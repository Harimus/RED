'''
Disclaimer: The trpo part highly rely on trpo_mpi at @openai/baselines
'''

import time
import os
from contextlib import contextmanager
from mpi4py import MPI
from collections import deque

import tensorflow as tf
import numpy as np

import baselines.common.tf_util as U
from baselines.common import explained_variance, zipsame, dataset, fmt_row
from baselines import logger
from baselines.common.mpi_adam import MpiAdam
from baselines.common.cg import cg
from baselines.gail.statistics import stats
from baselines.common.dataset_plus import iterbatches

import matplotlib.pyplot as plt
import seaborn as sns

def lineplot(x, y, y2=None, filename='', xaxis='Steps', yaxis='Return', title=''):
  y = np.array(y).squeeze()                                                              
  y_mean, y_std = y.mean(axis=1), y.std(axis=1)                                 
  sns.lineplot(x=x, y=y_mean, color='coral')                                    
  plt.fill_between(x, y_mean - y_std, y_mean + y_std, color='coral', alpha=0.3) 
  if y2:                                                                        
    y2 = np.array(y2).squeeze()                                                      
    y2_mean, y2_std = y2.mean(axis=1), y2.std(axis=1)                           
    sns.lineplot(x=x, y=y2_mean, color='b')                                     
    plt.fill_between(x, y2_mean - y2_std, y2_mean + y2_std, color='b', alpha=0.3)
                                                                                
  plt.xlim(left=0, right=x[-1])                                                 
  plt.xlabel(xaxis)                                                             
  plt.ylabel(yaxis)                                                             
  plt.title(title)                                                              
  plt.savefig(f'{filename}.png')                                                
  plt.close()          

def evaluate_policy(metrics, step, pi, env, reward_giver, expert_dataset, dir, num_episodes=20):
  exp_data_size = expert_dataset[0].shape[0]
  ob = env.reset()
  i, ep_lens,  episode_rewards,  = 0,[], []
  j, num_pred_compare, pred_reward, pred_exp_reward = 0, 500, [], []
  first = True
  first_trajectory_policy_reward, first_trajectory_expert_reward = []
  for i in range(num_episodes):
    sum_reward = 0
    done = False
    print(f"Evaluatng agent... {i}")
    while not done:
      ac, _ = pi.act(False, ob)
      ob, rew, done, _ = env.step(ac)
      rndi = np.random.randint(exp_data_size)
      red_reward = reward_giver.get_reward(ob, ac)
      if j < num_pred_compare:
        pred_reward.append(red_reward)
        pred_exp_reward.append(reward_giver.get_reward(expert_dataset[0][rndi], expert_dataset[1][rndi]))
      sum_reward+=rew
      j+=1
      if first: first_trajectory_policy_reward.append(red_reward)
    first = False
    episode_rewards.append(sum_reward)
    ep_lens.append(j)
    ob = env.reset()
  metrics['test_step'].append(step)
  metrics['test_returns'].append(episode_rewards)
  metrics['policy_reward'].append(pred_reward)
  metrics['expert_reward'].append(pred_exp_reward)
  tstfn = os.path.join(dir, 'test_return')
  predfn = os.path.join(dir, 'predicted_return')
  if len(metrics['test_step']) > 1:
    lineplot(metrics['test_step'], metrics['test_returns'], filename=tstfn, title='Returns')
    lineplot(metrics['test_step'], metrics['policy_reward'], metrics['expert_reward'], filename=predfn, title='predicted rewards')
  traj_len = len(first_trajectory_policy_reward)
  indx = min(rndi, len(expert_dataset[0])- traj_len)
  first_trajectory_expert_reward = reward_giver.get_reward(expert_dataset[0][indx:indx+traj_len], expert_dataset[1][indx:indx+traj_len])
  fig, ax1, ax2  = plt.subplots(2)
  ax1.bar([*range(len(first_trajectory_policy_reward))], first_trajectory_policy_reward)
  ax1.set_title('Policy trajectory reward')
  ax2.bar([*range(len(first_trajectory_expert_reward))], first_trajectory_expert_reward)
  ax2.set_title('Expert (sampled) same length trajectory reward')
  fig.savefig(f"Trajectory_Rewards{step}.png")
  plt.close(fig)






def traj_segment_generator(pi, env, reward_giver, horizon, stochastic):

    # Initialize state variables
    t = 0
    ac = env.action_space.sample()
    new = True
    rew = 0.0
    true_rew = 0.0
    ob = env.reset()

    cur_ep_ret = 0
    cur_ep_len = 0
    cur_ep_true_ret = 0
    ep_true_rets = []
    ep_rets = []
    ep_lens = []

    # Initialize history arrays
    obs = np.array([ob for _ in range(horizon)])
    true_rews = np.zeros(horizon, 'float32')
    rews = np.zeros(horizon, 'float32')
    vpreds = np.zeros(horizon, 'float32')
    news = np.zeros(horizon, 'int32')
    acs = np.array([ac for _ in range(horizon)])
    prevacs = acs.copy()

    while True:
        prevac = ac
        ac, vpred = pi.act(stochastic, ob)
        # Slight weirdness here because we need value function at time T
        # before returning segment [0, T-1] so we get the correct
        # terminal value
        if t > 0 and t % horizon == 0:
            yield {"ob": obs, "rew": rews, "vpred": vpreds, "new": news,
                   "ac": acs, "prevac": prevacs, "nextvpred": vpred * (1 - new),
                   "ep_rets": ep_rets, "ep_lens": ep_lens, "ep_true_rets": ep_true_rets}
            _, vpred = pi.act(stochastic, ob)
            # Be careful!!! if you change the downstream algorithm to aggregate
            # several of these batches, then be sure to do a deepcopy
            ep_rets = []
            ep_true_rets = []
            ep_lens = []
        i = t % horizon
        obs[i] = ob
        vpreds[i] = vpred
        news[i] = new
        acs[i] = ac
        prevacs[i] = prevac

        rew = reward_giver.get_reward(ob, ac)
        ob, true_rew, new, _ = env.step(ac)
        rews[i] = rew
        true_rews[i] = true_rew

        cur_ep_ret += rew
        cur_ep_true_ret += true_rew
        cur_ep_len += 1
        if new:
            ep_rets.append(cur_ep_ret)
            ep_true_rets.append(cur_ep_true_ret)
            ep_lens.append(cur_ep_len)
            cur_ep_ret = 0
            cur_ep_true_ret = 0
            cur_ep_len = 0
            ob = env.reset()
        t += 1


def add_vtarg_and_adv(seg, gamma, lam):
    new = np.append(seg["new"], 0)  # last element is only used for last vtarg, but we already zeroed it if last new = 1
    vpred = np.append(seg["vpred"], seg["nextvpred"])
    T = len(seg["rew"])
    seg["adv"] = gaelam = np.empty(T, 'float32')
    rew = seg["rew"]
    lastgaelam = 0
    for t in reversed(range(T)):
        nonterminal = 1-new[t+1]
        delta = rew[t] + gamma * vpred[t+1] * nonterminal - vpred[t]
        gaelam[t] = lastgaelam = delta + gamma * lam * nonterminal * lastgaelam
    seg["tdlamret"] = seg["adv"] + seg["vpred"]


def learn(env, policy_func, reward_giver, expert_dataset, rank,
          pretrained, pretrained_weight, *,
          g_step, d_step, entcoeff,
          ckpt_dir, timesteps_per_batch, task_name,
          gamma, lam,
          max_kl, cg_iters, cg_damping=1e-2,
          vf_stepsize=3e-4, d_stepsize=3e-4, vf_iters=3,
          max_timesteps=0, max_episodes=0, max_iters=0, rnd_iter=200,
          callback=None, dyn_norm=False, mmd=False):

    nworkers = MPI.COMM_WORLD.Get_size()
    rank = MPI.COMM_WORLD.Get_rank()
    np.set_printoptions(precision=3)
    # Setup losses and stuff
    # ----------------------------------------
    ob_space = env.observation_space
    ac_space = env.action_space
    pi = policy_func("pi", ob_space, ac_space)
    oldpi = policy_func("oldpi", ob_space, ac_space)
    atarg = tf.placeholder(dtype=tf.float32, shape=[None])  # Target advantage function (if applicable)

    ob = U.get_placeholder_cached(name="ob")
    ac = pi.pdtype.sample_placeholder([None])

    kloldnew = oldpi.pd.kl(pi.pd)
    ent = pi.pd.entropy()
    meankl = tf.reduce_mean(kloldnew)
    meanent = tf.reduce_mean(ent)
    entbonus = entcoeff * meanent

    ratio = tf.exp(pi.pd.logp(ac) - oldpi.pd.logp(ac))  # advantage * pnew / pold
    surrgain = tf.reduce_mean(ratio * atarg)

    optimgain = surrgain + entbonus
    losses = [optimgain, meankl, entbonus, surrgain, meanent]
    loss_names = ["optimgain", "meankl", "entloss", "surrgain", "entropy"]

    dist = meankl

    all_var_list = pi.get_trainable_variables()
    var_list = [v for v in all_var_list if v.name.startswith("pi/pol") or v.name.startswith("pi/logstd")]
    vf_var_list = [v for v in all_var_list if v.name.startswith("pi/vff")]
    vfadam = MpiAdam(vf_var_list)

    get_flat = U.GetFlat(var_list)
    set_from_flat = U.SetFromFlat(var_list)
    klgrads = tf.gradients(dist, var_list)
    flat_tangent = tf.placeholder(dtype=tf.float32, shape=[None], name="flat_tan")
    shapes = [var.get_shape().as_list() for var in var_list]
    start = 0
    tangents = []
    for shape in shapes:
        sz = U.intprod(shape)
        tangents.append(tf.reshape(flat_tangent[start:start+sz], shape))
        start += sz
    gvp = tf.add_n([tf.reduce_sum(g*tangent) for (g, tangent) in zipsame(klgrads, tangents)])  # pylint: disable=E1111
    fvp = U.flatgrad(gvp, var_list)

    assign_old_eq_new = U.function([], [], updates=[tf.assign(oldv, newv)
                                                    for (oldv, newv) in zipsame(oldpi.get_variables(), pi.get_variables())])
    compute_losses = U.function([ob, ac, atarg], losses)
    compute_lossandgrad = U.function([ob, ac, atarg], losses + [U.flatgrad(optimgain, var_list)])
    compute_fvp = U.function([flat_tangent, ob, ac, atarg], fvp)
    compute_vflossandgrad = pi.vlossandgrad


    def allmean(x):
        assert isinstance(x, np.ndarray)
        out = np.empty_like(x)
        MPI.COMM_WORLD.Allreduce(x, out, op=MPI.SUM)
        out /= nworkers
        return out

    U.initialize()
    th_init = get_flat()
    MPI.COMM_WORLD.Bcast(th_init, root=0)
    set_from_flat(th_init)
    vfadam.sync()
    if rank == 0:
        print("Init param sum", th_init.sum(), flush=True)

    # Prepare for rollouts
    # ----------------------------------------
    seg_gen = traj_segment_generator(pi, env, reward_giver, timesteps_per_batch, stochastic=True)

    episodes_so_far = 0
    timesteps_so_far = 0
    iters_so_far = 0
    tstart = time.time()
    lenbuffer = deque(maxlen=40)  # rolling buffer for episode lengths
    rewbuffer = deque(maxlen=40)  # rolling buffer for episode rewards
    true_rewbuffer = deque(maxlen=40)

    assert sum([max_iters > 0, max_timesteps > 0, max_episodes > 0]) == 1

    ep_stats = stats(["True_rewards", "Rewards", "Episode_length"])
    # if provide pretrained weight
    if pretrained_weight is not None:
        U.load_variables(pretrained_weight, variables=pi.get_variables())
    else:
        if not dyn_norm:
            pi.ob_rms.update(expert_dataset[0])


    if not mmd:
        reward_giver.train(*expert_dataset, iter=rnd_iter)
        #inspect the reward learned
        # for batch in iterbatches(expert_dataset, batch_size=32):
        #     print(reward_giver.get_reward(*batch))

    best = -2000
    save_ind = 0
    max_save = 3
    metrics = dict(test_step=[], test_returns=[], policy_reward=[], expert_reward=[])
    eval_count = 0
    while True:
        if callback: callback(locals(), globals())
        if max_timesteps and timesteps_so_far >= max_timesteps:
            break
        elif max_episodes and episodes_so_far >= max_episodes:
            break
        elif max_iters and iters_so_far >= max_iters:
            break


        logger.log("********** Iteration %i ************" % iters_so_far)

        def fisher_vector_product(p):
            return allmean(compute_fvp(p, *fvpargs)) + cg_damping * p
        # ------------------ Update G ------------------
        # logger.log("Optimizing Policy...")
        for _ in range(g_step):
            seg = seg_gen.__next__()

            #mmd reward
            if mmd:
                reward_giver.set_b2(seg["ob"], seg["ac"])
                seg["rew"] = reward_giver.get_reward(seg["ob"], seg["ac"])

            #report stats and save policy if any good
            lrlocal = (seg["ep_lens"], seg["ep_rets"], seg["ep_true_rets"])  # local values
            listoflrpairs = MPI.COMM_WORLD.allgather(lrlocal)  # list of tuples
            lens, rews, true_rets = map(flatten_lists, zip(*listoflrpairs))
            true_rewbuffer.extend(true_rets)
            lenbuffer.extend(lens)
            rewbuffer.extend(rews)

            true_rew_avg = np.mean(true_rewbuffer)
            logger.record_tabular("EpLenMean", np.mean(lenbuffer))
            logger.record_tabular("EpRewMean", np.mean(rewbuffer))
            logger.record_tabular("EpTrueRewMean", true_rew_avg)
            logger.record_tabular("EpThisIter", len(lens))
            episodes_so_far += len(lens)
            timesteps_so_far += sum(lens)
            iters_so_far += 1

            logger.record_tabular("EpisodesSoFar", episodes_so_far)
            logger.record_tabular("TimestepsSoFar", timesteps_so_far)
            logger.record_tabular("TimeElapsed", time.time() - tstart)
            logger.record_tabular("Best so far", best)

            # Save model
            if ckpt_dir is not None and true_rew_avg >= best:
                best = true_rew_avg
                fname = os.path.join(ckpt_dir, task_name)
                os.makedirs(os.path.dirname(fname), exist_ok=True)
                pi.save_policy(fname+"_"+str(save_ind))
                save_ind = (save_ind+1) % max_save


            #compute gradient towards next policy
            add_vtarg_and_adv(seg, gamma, lam)
            ob, ac, atarg, tdlamret = seg["ob"], seg["ac"], seg["adv"], seg["tdlamret"]
            vpredbefore = seg["vpred"]  # predicted value function before udpate
            atarg = (atarg - atarg.mean()) / atarg.std()  # standardized advantage function estimate

            if hasattr(pi, "ob_rms") and dyn_norm: pi.ob_rms.update(ob)  # update running mean/std for policy

            args = seg["ob"], seg["ac"], atarg
            fvpargs = [arr[::5] for arr in args]

            assign_old_eq_new()  # set old parameter values to new parameter values
            *lossbefore, g = compute_lossandgrad(*args)
            lossbefore = allmean(np.array(lossbefore))
            g = allmean(g)
            if np.allclose(g, 0):
                logger.log("Got zero gradient. not updating")
            else:
                stepdir = cg(fisher_vector_product, g, cg_iters=cg_iters, verbose=False)
                assert np.isfinite(stepdir).all()
                shs = .5*stepdir.dot(fisher_vector_product(stepdir))
                lm = np.sqrt(shs / max_kl)
                fullstep = stepdir / lm
                expectedimprove = g.dot(fullstep)
                surrbefore = lossbefore[0]
                stepsize = 1.0
                thbefore = get_flat()
                for _ in range(10):
                    thnew = thbefore + fullstep * stepsize
                    set_from_flat(thnew)
                    meanlosses = surr, kl, *_ = allmean(np.array(compute_losses(*args)))
                    improve = surr - surrbefore
                    logger.log("Expected: %.3f Actual: %.3f" % (expectedimprove, improve))
                    if not np.isfinite(meanlosses).all():
                        logger.log("Got non-finite value of losses -- bad!")
                    elif kl > max_kl * 1.5:
                        logger.log("violated KL constraint. shrinking step.")
                    elif improve < 0:
                        logger.log("surrogate didn't improve. shrinking step.")
                    else:
                        logger.log("Stepsize OK!")
                        break
                    stepsize *= .5
                else:
                    logger.log("couldn't compute a good step")
                    set_from_flat(thbefore)
                if nworkers > 1 and iters_so_far % 20 == 0:
                    paramsums = MPI.COMM_WORLD.allgather((thnew.sum(), vfadam.getflat().sum()))  # list of tuples
                    assert all(np.allclose(ps, paramsums[0]) for ps in paramsums[1:])
            if pi.use_popart:
                pi.update_popart(tdlamret)
            for _ in range(vf_iters):
                for (mbob, mbret) in dataset.iterbatches((seg["ob"], seg["tdlamret"]),
                                                         include_final_partial_batch=False, batch_size=128):
                    if hasattr(pi, "ob_rms") and dyn_norm:
                        pi.ob_rms.update(mbob)  # update running mean/std for policy
                    vfadam.update(allmean(compute_vflossandgrad(mbob, mbret)), vf_stepsize)

        g_losses = meanlosses
        for (lossname, lossval) in zip(loss_names, meanlosses):
            logger.record_tabular(lossname, lossval)
        logger.record_tabular("ev_tdlam_before", explained_variance(vpredbefore, tdlamret))
        if rank == 0:
            logger.dump_tabular()
            if timesteps_so_far > eval_count *(int(max_timesteps/100)):
              evaluate_policy(metrics, timesteps_so_far, pi, env, reward_giver, expert_dataset, ckpt_dir)
              eval_count  += 1


def flatten_lists(listoflists):
    return [el for list_ in listoflists for el in list_]
