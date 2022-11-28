import numpy as np
import matplotlib.pyplot as plt
import time
import scipy.io as scio
import os
import pygame
import signal
import argparse

import torch
from torch.utils.tensorboard import SummaryWriter   
writer = SummaryWriter('./algo/checkpoints/log')

from env_leftturn import LeftTurn
from utils import set_seed, signal_handler


def train_leftturn_task():
    
    set_seed(args.seed)
    
    # construct the DRL agent
    if args.algorithm == 0:
        from algo.TD3PHIL import DRL
        log_dir = 'algo/checkpoints/TD3jingda.pth'
    elif args.algorithm == 1:
        from algo.TD3IARL1 import DRL
        log_dir = 'algo/checkpoints/TD3IARL.pth'
    elif args.algorithm == 2:
        from algo.TD3 import DRL
        log_dir = 'algo/checkpoints/TD3HIRL.pth'    
    elif args.algorithm == 3:
        from algo.TD3 import DRL    
        log_dir = 'algo/checkpoints/TD3.pth'
    
    
    # if replay_mechansim == 'Proposed':
    #     pass
    # elif replay_mechansim == 'TD'
    #     from algo.TD3PHILTD import DRL    
    #     log_dir = 'algo/checkpoints/TD3PHILTD.pth'
    # elif algorithm == 6:
    #     from algo.TD3DDPHIL import DRL   
    #     log_dir = 'algo/checkpoints/TD3DDPHIL.pth'
    # else:
    #     from algo.TD3DD import DRL   
    #     log_dir = 'algo/checkpoints/TD3DD.pth'

    
    env = LeftTurn(joystick_enabled = args.joystick_enabled, conservative_surrounding = args.simulator_conservative_surrounding, 
                   frame=args.simulator_render_frequency, port=args.simulator_port)

    s_dim = [env.observation_size_width, env.observation_size_height]
    a_dim = env.action_size
    
    DRL = DRL(a_dim, s_dim)
    
    exploration_rate = args.initial_exploration_rate 
    
    if args.resume and os.path.exists(log_dir):
        checkpoint = torch.load(log_dir)
        DRL.load(log_dir)
        start_epoch = checkpoint['epoch'] + 1
    else:
        start_epoch = 0
    
    
    if args.human_model:
        from algo.SL import SL
        SL = SL(a_dim, s_dim)
        SL.load_model('./algo/models_leftturn/SL.pkl')
    
    
    # initialize global variables
    total_step = 0
    a_loss,c_loss = 0,0
    count_sl = 0
    
    loss_critic, loss_actor = [], []
    episode_total_reward_list, episode_mean_reward_list = [], []
    global_reward_list, episode_duration_list = [], []
    
    drl_action = [[] for i in range(args.maximum_episode)] # drl_action: output by RL policy
    adopted_action = [[] for i in range(args.maximum_episode)]  # adopted_action: eventual action which may include human guidance action
     
    reward_i_record = [[] for i in range(args.maximum_episode)]  # reward_i: virtual reward (added shaping term);
    reward_e_record = [[] for i in range(args.maximum_episode)]  # reward_e: real reward
    
    
    start_time = time.perf_counter()
    
    for i in range(start_epoch, args.maximum_episode):
        
        # initial episode variables
        ep_reward = 0      
        step = 0
        step_intervened = 0
        done = False
        human_model_activated = False

        # initial environment
        observation = env.reset()
        state = np.repeat(np.expand_dims(observation,2), 2, axis=2)
        state_ = state.copy()
    
        while not done:
            ## Section DRL's actting ##
            action = DRL.choose_action(state)
            action = np.clip( np.random.normal(action,  exploration_rate), -1, 1)
            
    
            drl_action[i].append(action)
            ## End of Section DRL's actting
    
            
            ## Section human model's actting ##
            # use a pre-trained model to output human-like policy which can be taken as human guidance
            # this model is only activated when the ego agent is approaching risk
            if args.human_model:
                if (env.risk is not None ) and (abs(env.risk) <1) and (env.v_low < env.ego_vehicle.get_velocity().y) and (env.index_obs_concerned is not None) and (i % 3 == 1):
                    action = SL.choose_action(observation)
                    env.intervention = True
                    human_model_activated = True
                    env.show_human_model_mode()
                else:
                    human_model_activated = False
            ## End of Section human model's actting
            
        
            ## Section environment update ##
            observation_, action_fdbk, reward_e, _, done, scope = env.step(action)
            
            state_[:,:,0] = state_[:,:,1].copy()
            state_[:,:,1] = observation_.copy()
        
            ## End of Section environment update ##
    
    
            ## Section reward shaping ##
            # intervention penalty-based shaping
            if args.reward_shaping == 1:
                # only the 1st intervened time step is penalized
                if (action_fdbk is not None) or (human_model_activated is True):
                    if step_intervened == 0:
                        reward_i = -10
                        step_intervened += 1
                    else:
                        reward_i = 0
                else:
                    reward_i = 0
                    step_intervened = 0
            # no shaping
            else:
                reward_i = 0
            reward = reward_e + reward_i
            ## End of Section reward shaping ##


            ## Section DRL store ##
            # real human intervention event occurs
            if action_fdbk is not None:
                
                action = action_fdbk
                intervention = 1
                DRL.store_transition(state, action, action_fdbk, intervention, reward, state_)
                
                if args.human_model_update:
                    SL.store_transition(observation, action_fdbk)
                    count_sl += 1

            # human model intervention event occurs
            elif human_model_activated is True:
                intervention = 1
                action_fdbk = action
                DRL.store_transition(state, action, action_fdbk, intervention, reward, state_)

            # no intervention occurs
            else:
                intervention = 0
                DRL.store_transition(state, action, action_fdbk, intervention, reward, state_)
            ## End of DRL store ##
    
    
            ## Section DRL update ##
            learn_threshold = args.warmup_threshold if args.warmup else 256
            if total_step > learn_threshold:
                c_loss, a_loss = DRL.learn(batch_size = 16, epoch=i)
                loss_critic.append(np.average(c_loss))
                loss_actor.append(np.average(a_loss))
                # Decrease the exploration rate
                exploration_rate = exploration_rate * args.exploration_decay_rate if exploration_rate>args.cutoff_exploration_rate else args.cutoff_exploration_rate
            ## End of Section DRL update ##
            
            
            if args.human_model and args.human_model_update and count_sl > 100 and step % 10 == 0:
                SL.learn(batch_size = 32, epoch = i)
    
            ep_reward += reward
            global_reward_list.append([reward_e,reward_i])
            reward_e_record[i].append(reward_e)
            reward_i_record[i].append(reward_i)
            
            adopted_action[i].append(action)
    
            

            observation = observation_.copy()
            state = state_.copy()
            
            
            dura = env.terminate_position
            total_step += 1
            step += 1

    
        # plt.subplot(211)
        # plt.plot(reward_e_record[i])
        # plt.plot(reward_i_record[i])
        # plt.subplot(212)
        # plt.plot(adopted_action[i])
        # plt.show()
        
        mean_reward =  ep_reward / step  
        episode_total_reward_list.append(ep_reward)
        episode_mean_reward_list.append(mean_reward)
        episode_duration_list.append(dura)

        print('\n episode is:',i)
        print('explore_rate:',round(exploration_rate,4))
        print('c_loss:',round(np.average(c_loss),4))
        print('a_loss',round(np.average(a_loss),4))
        print('total_step:',total_step)
        print('episode_step:',step)
        print('episode_cumu_reward',round(ep_reward,4))
        
        writer.add_scalar('reward/reward_episode', ep_reward, i)
        writer.add_scalar('reward/reward_episode_noshaping', np.mean(reward_e_record[i]), i)
        writer.add_scalar('reward/duration_episode', step, i)
        writer.add_scalar('rate_exploration', round(exploration_rate,4), i)
        writer.add_scalar('loss/loss_critic', round(np.average(c_loss),4), i)
        writer.add_scalar('loss/loss_actor', round(np.average(a_loss),4), i)
        
    
    signal.signal(signal.SIGINT, signal_handler)
    
    print('total time:',time.perf_counter()-start_time)        
    
    timeend = round(time.time())
    DRL.save_actor('./algo/models_leftturn', timeend)
    
    pygame.display.quit()
    pygame.quit()
    
    action_drl = drl_action[0:i]
    action_final = adopted_action[0:i]
    scio.savemat('dataleftturn_{}-{}-{}.mat'.format(args.seed,args.algorithm,timeend), mdict={'action_drl': action_drl,'action_final': action_final,
                                                    'stepreward':global_reward_list,'mreward':episode_mean_reward_list,
                                                    'step':episode_duration_list,'reward':episode_total_reward_list,
                                                    'r_i':reward_i_record,'r_e':reward_e_record})
    
    


if __name__ == "__main__":
    # Arguments
    parser = argparse.ArgumentParser(description='Training')
    parser.add_argument('--algorithm', type=int, help='RL algorithm (0 for Proposed, 1 for IARL, 2 for HIRL, 3 for Vanilla TD3) (default: 0)', default=0)
    parser.add_argument('--human_model', action="store_true", help='whehther to use human behavior model (default: True)', default=True)
    parser.add_argument('--human_model_update', action="store_true", help='whehther to update human behavior model (default: False)', default=False)
    parser.add_argument('--maximum_episode', type=float, help='maximum training episode number (default:400)', default=400)
    parser.add_argument('--seed', type=int, help='fix random seed', default=2)
    parser.add_argument("--initial_exploration_rate", type=float, help="initial explore policy variance (default: 0.5)", default=0.5)
    parser.add_argument("--cutoff_exploration_rate", type=float, help="minimum explore policy variance (default: 0.05)", default=0.05)
    parser.add_argument("--exploration_decay_rate", type=float, help="decay factor of explore policy variance (default: 0.99988)", default=0.99988)
    parser.add_argument('--resume', action="store_true", help='whether to resume trained agents (default: False)', default=False)
    parser.add_argument('--warmup', action="store_true", help='whether to start training until collecting enough data (default: False)', default=False)
    parser.add_argument('--warmup_threshold', type=int, help='warmup length by step (default: 1e4)', default=1e4)
    parser.add_argument('--pid_controller_guidance', action="store_true", help='whether to use PID controller providing guidance action (default: False)', default=False)
    parser.add_argument('--reward_shaping', type=int, help='reward shaping scheme (0: none; 1:proposed) (default: 1)', default=1)
    parser.add_argument('--device', type=str, help='run on which device (default: cuda)', default='cuda')
    parser.add_argument('--simulator_port', type=int, help='Carla port value which needs specifize when using multiple CARLA clients (default: 2000)', default=2000)
    parser.add_argument('--simulator_render_frequency', type=int, help='Carla rendering frequenze, Hz (default: 12)', default=12)
    parser.add_argument('--simulator_conservative_surrounding', action="store_true", help='surrounding vehicles are conservative or not (default: False)', default=False)
    parser.add_argument('--joystick_enabled', action="store_true", help='whether use Logitech G29 joystick for human guidance (default: False)', default=False)
    args = parser.parse_args()

    # Run
    train_leftturn_task()
