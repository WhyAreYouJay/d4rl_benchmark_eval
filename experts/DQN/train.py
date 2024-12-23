import torch
import numpy as np
import argparse
from utils import ReplayMemory, Transition
from experts.DQN.DQN import DQN
from deep_learning_rl_sm.environments.connect_four import ConnectFour


def agent_loop(agent, current_state, Replay_memory, environment, adv=False):
    # Select and perform an action
    current_state = torch.flatten(torch.tensor(current_state, dtype=torch.float32))
    action = agent.select_action(current_state, num_passes, environment.action_mask)
    action_mask = torch.tensor(environment.action_mask).unsqueeze(0)
    action = action.squeeze()

    # TODO change Connect-4 env to be 2-player
    # TODO fix nan problem in masking
    """print(environment.action_mask)
    print(action)"""
    player = 1 if adv is False else 2
    next_state, reward, done, time_restriction, _ = environment.step_2P(action, player=player)
    next_action_mask = torch.tensor(environment.action_mask).unsqueeze(0)

    # convert to tensors
    done_mask = torch.Tensor([done])
    reward = torch.tensor(reward, dtype=torch.float32)

    # Store the transition in memory
    Replay_memory.push(current_state.unsqueeze(0), action.unsqueeze(0), action_mask, done_mask.unsqueeze(0),
                       torch.flatten(torch.tensor(next_state, dtype=torch.float32)).unsqueeze(0), next_action_mask,
                       reward.unsqueeze(0))

    # Perform one step of the optimization (on the policy network/s)
    if len(Replay_memory) >= args.BATCH_SIZE:
        transitions = Replay_memory.sample(args.BATCH_SIZE)
        batch = Transition(*zip(*transitions))
        agent.update(batch)

    if num_passes % (2 * args.target_update) == 0:
        # hard update
        agent.target_net.load_state_dict(agent.policy_net.state_dict())

    return next_state, done


def simulate_env():
    # TODO for testing
    return None


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--BATCH_SIZE", default=64, type=int,
                        help="Size of the batch used in training to update the networks(default: 32)")
    parser.add_argument("--num_games", default=10000, type=int,
                        help="Num. of total timesteps of training (default: 3000)")
    parser.add_argument("--gamma", default=0.99,
                        help="Discount factor (default: 0.99)")
    parser.add_argument("--tau", default=0.001,
                        help="Update factor for the soft update of the target networks (default: 0.001)")
    parser.add_argument("--EVALUATE", default=50, type=int,
                        help="Number of episodes between testing cycles(default: 50)")
    parser.add_argument("--mem_size", default=10000, type=int,
                        help="Size of the Replay Buffer(default: 10000)")
    parser.add_argument("--noise_SD", default=1.0, type=float,
                        help="noise Standard deviation(default: 0.7)")
    parser.add_argument("--eps_start", default=1.0,
                        help="used for random actions in DQN")
    parser.add_argument("--eps_end", default=0.1,
                        help="used for random actions in DQN")
    parser.add_argument("--eps_decay", default=700,
                        help="used for random actions in DQN")
    parser.add_argument("--target_update", default=20, type=int,
                        help="number of iterations before dqn_target is receives hard update")
    args = parser.parse_args()
    # TODO if enough time:
    #  .replace hard update with soft update
    #  .double DQN

    memory_agent = ReplayMemory(args.mem_size)
    memory_adv = ReplayMemory(args.mem_size)

    env = ConnectFour()
    agent_dqn = DQN(args.BATCH_SIZE, args.gamma, args.eps_start, args.eps_end, args.eps_decay)
    adversary_dqn = DQN(args.BATCH_SIZE, args.gamma, args.eps_start, args.eps_end, args.eps_decay)

    episodeList = []
    averageRewardList = []
    i_episode = 0
    num_passes = 0
    max_average_reward = float("-inf")

    # TRAINING
    while i_episode < args.num_games:
        print("episode " + str(i_episode))
        state, _ = env.reset()
        final_state = False
        while not final_state:
            num_passes += 1
            if num_passes % 2 == 1:
                state, final_state = agent_loop(agent_dqn, state, memory_agent, env, adv=False)
            else:
                state, final_state = agent_loop(adversary_dqn, state, memory_adv, env, adv=True)

        if i_episode % args.EVALUATE == 0:
            if len(memory_agent) >= args.BATCH_SIZE:
                print("testing network...")

        i_episode += 1

    print('Completed training...')

    # torch.save(agent_dqn.policy_net.state_dict(), "net_configs/dqn.pth")
