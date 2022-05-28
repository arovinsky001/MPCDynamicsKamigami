import argparse
import pickle as pkl
from pdb import set_trace

import numpy as np
from matplotlib import pyplot as plt
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split

import torch
from torch import nn
from torch.nn import functional as F
from torch import tensor
from tqdm import trange, tqdm
from time import time

from sim.scripts.generate_data import *

# if torch.backends.mps.is_available:
#     device = torch.device("mps")
# else:
#     device = torch.device("cpu")
device = torch.device("cpu")

def to_device(*args):
    ret = []
    for arg in args:
        ret.append(arg.to(device))
    return ret

def to_tensor(*args):
    ret = []
    for arg in args:
        if type(arg) == np.ndarray:
            ret.append(tensor(arg.astype('float32'), requires_grad=True))
        else:
            ret.append(arg)
    return ret if len(ret) > 1 else ret[0]

def init_weights(m):
    if isinstance(m, nn.Linear):
        torch.nn.init.xavier_uniform_(m.weight)
        m.bias.data.fill_(0.01)

class DynamicsNetwork(nn.Module):
    def __init__(self, state_dim, action_dim, hidden_dim=512, lr=7e-4, std=0.0, delta=False):
        super(DynamicsNetwork, self).__init__()
        input_dim = state_dim + action_dim
        self.model = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Dropout(p=0.8),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, state_dim),
        )
        self.model.apply(init_weights)
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=lr)
        self.loss_fn = nn.MSELoss(reduction='none')
        self.delta = delta
        self.dist = (std > 0)
        self.std = std
        self.trained = False
        self.input_scaler = None
        self.output_scaler = None

    def forward(self, state, action):
        state, action = to_tensor(state, action)
        if len(state.shape) == 1:
            state = state[:, None]
        if len(action.shape) == 1:
            action = action[:, None]

        state_action = torch.cat([state, action], dim=-1).float()
        if self.dist:
            mean = self.model(state_action)
            std = torch.ones(mean.shape[-1]) * self.std
            return torch.distributions.normal.Normal(mean, std)
        else:
            pred = self.model(state_action)
            return pred
        

    def update(self, state, action, next_state, retain_graph=False):
        state, action, next_state = to_tensor(state, action, next_state)
        
        if self.dist:
            dist = self(state, action)
            prediction = dist.rsample()
            if self.delta:
                losses = self.loss_fn(state + prediction, next_state)
            else:
                losses = self.loss_fn(prediction, next_state)
        else:
            if self.delta:
                state_delta = self(state, action)
                losses = self.loss_fn(state + state_delta, next_state)
            else:
                pred_next_state = self(state, action)
                losses = self.loss_fn(pred_next_state, next_state)
        loss = losses.mean()

        self.optimizer.zero_grad()
        loss.backward(retain_graph=retain_graph)
        self.optimizer.step()
        return losses.detach()
    
    def set_scalers(self, states, actions, next_states):
        with torch.no_grad():
            self.input_scaler = StandardScaler().fit(np.append(states, actions, axis=-1))
            self.output_scaler = StandardScaler().fit(next_states)
    
    def get_scaled(self, *args):
        if len(args) == 2:
            states, actions = args
            states_actions = np.append(states, actions, axis=-1)
            states_actions_scaled = self.input_scaler.transform(states_actions)
            states_scaled = states_actions_scaled[:, :states.shape[-1]]
            actions_scaled = states_actions_scaled[:, states.shape[-1]:]
            return states_scaled, actions_scaled
        else:
            next_states = args[0]
            next_states_scaled = self.output_scaler.transform(next_states)
            return next_states_scaled

class MPCAgent:
    def __init__(self, state_dim, action_dim, seed=1, hidden_dim=512, lr=7e-4, std=0.0, delta=True):
        self.model = DynamicsNetwork(state_dim, action_dim, hidden_dim=hidden_dim, lr=lr, std=std, delta=delta)
        self.model.to(device)
        self.seed = seed
        self.action_dim = action_dim
        self.mse_loss = nn.MSELoss(reduction='none')
        self.neighbors = []
        self.state = None
        self.delta = delta
        self.time = 0

    def mpc_action(self, state, init, goal, state_range, action_range, n_steps=10, n_samples=1000,
                   swarm=False, swarm_weight=0.1, perp_weight=0.0, angle_weight=0.0, forward_weight=0.0):
        state, init, goal, state_range = to_tensor(state, init, goal, state_range)
        self.state = state
        all_actions = torch.empty(n_steps, n_samples, self.action_dim).uniform_(*action_range)
        states = torch.tile(state, (n_samples, 1))
        goals = torch.tile(goal, (n_samples, 1))
        states, all_actions, goals = to_tensor(states, all_actions, goals)
        x1, y1 = init
        x2, y2 = goal
        vec_to_goal = goal - init
        optimal_dot = vec_to_goal / vec_to_goal.norm()
        perp_denom = vec_to_goal.norm()
        all_losses = torch.empty(n_steps, n_samples)

        for i in range(n_steps):
            actions = all_actions[i]
            with torch.no_grad():
                states = self.get_prediction(states, actions)
            states = torch.clamp(states, *state_range)

            x0, y0 = states.T
            vecs_to_goal = goals - states
            actual_dot = vecs_to_goal.T / vecs_to_goal.norm(dim=-1)
            
            dist_loss = torch.norm(goals - states, dim=-1)
            perp_loss = torch.abs((x2 - x1) * (y1 - y0) - (x1 - x0) * (y2 - y1)) / perp_denom
            angle_loss = torch.arccos(torch.clamp(optimal_dot @ actual_dot, -1., 1.))
            forward_loss = torch.abs(optimal_dot @ vecs_to_goal.T).reshape(dist_loss.shape)
            swarm_loss = self.swarm_loss(states, goals) if swarm else 0

            perp_loss = perp_loss.reshape(dist_loss.shape)
            angle_loss = angle_loss.reshape(dist_loss.shape)
            forward_loss = forward_loss.reshape(dist_loss.shape)

            all_losses[i] = dist_loss + forward_weight * forward_loss + perp_weight * perp_loss \
                                + angle_weight * angle_loss + swarm_weight * swarm_loss
        
        best_idx = all_losses.sum(dim=0).argmin()
        return all_actions[0, best_idx]
    
    def get_prediction(self, states, actions, scale_input=True):
        if scale_input:
            states, actions = self.model.get_scaled(states, actions)
            states, actions = to_tensor(states, actions)
        states, actions = to_device(states, actions)
        model_output = self.model(states, actions)
        if self.model.dist:
            if self.delta:
                states_delta_scaled = model_output.loc
                next_states_scaled = states_delta_scaled + states
            else:
                next_states_scaled = model_output.loc
        else:
            if self.delta:
                states_delta_scaled = model_output
                next_states_scaled = states_delta_scaled + states
            else:
                next_states_scaled = model_output.detach()
        next_states_scaled = next_states_scaled.detach().cpu()
        next_states = self.model.output_scaler.inverse_transform(next_states_scaled)
        return to_tensor(next_states).float()

    def swarm_loss(self, states, goals):
        neighbor_dists = torch.empty(len(self.neighbors), states.shape[0])
        for i, neighbor in enumerate(self.neighbors):
            neighbor_states = torch.tile(neighbor.state, (states.shape[0], 1))
            distance = torch.norm(states - neighbor_states, dim=-1)
            neighbor_dists[i] = distance
        goal_term = torch.norm(goals - states, dim=-1)
        loss = neighbor_dists.mean(dim=0) * goal_term.mean() / goals.mean(dim=0).norm()
        return loss

    def train(self, states, actions, next_states, epochs=5, batch_size=256, correction_iters=0, n_tests=100):
        states, actions, next_states = to_tensor(states, actions, next_states)
        train_states, test_states, train_actions, test_actions, train_next_states, test_next_states \
            = train_test_split(states, actions, next_states, test_size=0.1, random_state=self.seed)

        training_losses = []
        test_losses = []
        test_idx = []
        idx = np.arange(len(train_states))

        n_batches = len(train_states) / batch_size
        if int(n_batches) != n_batches:
            n_batches += 1
        n_batches = int(n_batches)
        test_interval = epochs * n_batches // n_tests
        test_interval = 1 if test_interval == 0 else test_interval

        i = 0
        for _ in tqdm(range(epochs), desc="Epoch", position=0, leave=False):
            np.random.shuffle(idx)
            train_states, train_actions, train_next_states = train_states[idx], train_actions[idx], train_next_states[idx]
            
            for j in tqdm(range(n_batches), desc="Batch", position=1, leave=False):
                batch_states = torch.autograd.Variable(train_states[j*batch_size:(j+1)*batch_size])
                batch_actions = torch.autograd.Variable(train_actions[j*batch_size:(j+1)*batch_size])
                batch_next_states = torch.autograd.Variable(train_next_states[j*batch_size:(j+1)*batch_size])
                batch_states, batch_actions, batch_next_states = to_device(batch_states, batch_actions, batch_next_states)
                training_loss = self.model.update(batch_states, batch_actions, batch_next_states)
                if type(training_loss) != float:
                    while len(training_loss.shape) > 1:
                        training_loss = training_loss.sum(axis=-1)

                for _ in range(correction_iters):
                    training_loss = self.correct(batch_states, batch_actions, batch_next_states, training_loss)

                training_loss_mean = training_loss.mean().detach()
                training_losses.append(training_loss_mean)
                
                if i % test_interval == 0:
                    with torch.no_grad():
                        pred_next_states = self.get_prediction(test_states, test_actions, scale_input=False)
                    test_loss = self.mse_loss(pred_next_states, test_next_states)
                    test_loss_mean = test_loss.mean().detach()
                    test_losses.append(test_loss_mean)
                    test_idx.append(i)
                    tqdm.write(f"mean training loss: {training_loss_mean} | mean test loss: {test_loss_mean}")

                i += 1

        self.model.trained = True
        return training_losses, test_losses, test_idx

    def correct(self, states, actions, next_states, loss):
        batch_size = len(states)
        worst_idx = torch.topk(loss.squeeze(), int(batch_size / 10))[1].detach().numpy()
        worst_states, worst_actions, worst_next_states = states[worst_idx], actions[worst_idx], next_states[worst_idx]
        loss = self.model.update(worst_states, worst_actions, worst_next_states)
        
        if type(loss) != float:
            while len(loss.shape) > 1:
                loss = loss.sum(axis=-1)

        return loss

    def optimal_policy(self, state, goal, table, swarm=False, swarm_weight=0.3):
        if swarm:
            vec = goal - state
            states = tensor(state + table[:, 1, None])
            neighbor_dists = []
            for neighbor in self.neighbors:
                neighbor_states = torch.tile(neighbor.state, (states.shape[0], 1))
                distance = self.mse_loss(states, neighbor_states)
                neighbor_dists.append(distance.detach().numpy())
            neighbor_dists = np.array(neighbor_dists)
            mean_dists = neighbor_dists.mean(axis=0)
            goals = np.tile(goal, (len(states), 1))
            goal_dists = self.mse_loss(states, goals)
            costs = goal_dists + swarm_weight * mean_dists
        else:
            vec = goal - state
            diff = abs(vec - table[:, 1, None])
            min_idx = diff.argmin(axis=0)
        return table[min_idx, 0]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Train/load agent and do MPC.')
    parser.add_argument('-load_agent_path', type=str,
                        help='path/file to load old agent from')
    parser.add_argument('-save_agent_path', type=str,
                        help='path/file to save newly-trained agent to')
    parser.add_argument('-new_agent', '-n', action='store_true',
                        help='flag to train new agent')
    parser.add_argument('-hidden_dim', type=int, default=512,
                        help='hidden layers dimension')
    parser.add_argument('-epochs', type=int, default=10,
                        help='number of training epochs for new agent')
    parser.add_argument('-batch_size', type=int, default=128,
                        help='batch size for training new agent')
    parser.add_argument('-learning_rate', type=float, default=7e-4,
                        help='batch size for training new agent')
    parser.add_argument('-seed', type=int, default=1,
                        help='random seed for numpy and pytorch')
    parser.add_argument('-correction_iters', type=int, default=0,
                        help='number of times to retrain on mistaken data')
    parser.add_argument('-stochastic', action='store_true',
                        help='flag to use stochastic transition data')
    parser.add_argument('-std', type=float, default=0.0,
                        help='standard deviation for model distribution')
    parser.add_argument('-delta', action='store_true',
                        help='flag to output state delta')
    parser.add_argument('-real', action='store_true',
                        help='flag to use real data')

    args = parser.parse_args()
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    if args.real:
        agent_path = 'agents/real.pkl'
        data = np.load("sim/data/real_data_one.npz")
    else:
        agent_path = 'agents/'
        if args.stochastic:
            data = np.load("sim/data/data_stochastic.npz")
        else:
            data = np.load("sim/data/data_deterministic.npz")
    
    states = data['states']
    actions = data['actions']
    next_states = data['next_states']

    if args.real:
        bad_idx = np.linalg.norm(np.abs(states - next_states)[:, :2], axis=-1) > 0.1
        bad_idx = np.logical_or(bad_idx, np.linalg.norm(np.abs(states - next_states)[:, :2], axis=-1) < 0.0005)
        states = states[~bad_idx]
        actions = actions[~bad_idx]
        next_states = next_states[~bad_idx]

    print('\nDATA LOADED\n')

    if not args.real:
        agent_path += f"epochs{args.epochs}"
        agent_path += f"_dim{args.hidden_dim}"
        agent_path += f"_batch{args.batch_size}"
        agent_path += f"_lr{args.learning_rate}"
        if args.std > 0:
            agent_path += f"_std{args.std}"
        if args.stochastic:
            agent_path += "_stochastic"
        if args.delta:
            agent_path += "_delta"
        if args.correction_iters > 0:
            agent_path += f"_correction{args.correction_iters}"
        agent_path += ".pkl"

    if args.new_agent:
        agent = MPCAgent(states.shape[-1], actions.shape[-1], seed=args.seed, std=args.std,
                         delta=args.delta, hidden_dim=args.hidden_dim, lr=args.learning_rate)

        agent.model.set_scalers(states, actions, next_states)
        states_scaled, actions_scaled = agent.model.get_scaled(states, actions)
        next_states_scaled = agent.model.get_scaled(next_states)

        training_losses, test_losses, test_idx = agent.train(
                        states_scaled, actions_scaled, next_states_scaled,
                        epochs=args.epochs, batch_size=args.batch_size,
                        correction_iters=args.correction_iters, n_tests=100)

        training_losses = np.array(training_losses).squeeze()
        test_losses = np.array(test_losses).squeeze()
        plt.plot(np.arange(len(training_losses)), training_losses, label="Training Loss")
        plt.plot(test_idx, test_losses, label="Test Loss")
        # plt.yscale('log')
        plt.xlabel('Batch')
        plt.ylabel('Loss')
        plt.title('Dynamics Model Loss')
        plt.legend()
        plt.grid()
        plt.show()
        
        import pdb;pdb.set_trace()
        agent_path = args.save_agent_path if args.save_agent_path else agent_path
        with open(agent_path, "wb") as f:
            pkl.dump(agent, f)
    else:
        agent_path = args.load_agent_path if args.load_agent_path else agent_path
        with open(agent_path, "rb") as f:
            agent = pkl.load(f)

    for i in range(len(states)):
        pred = agent.get_prediction(states[None, i], actions[None, i]).detach()
        print(abs(pred - next_states[i]))
        set_trace()
    
    state_range = np.array([MIN_STATE, MAX_STATE])
    action_range = np.array([MIN_ACTION, MAX_ACTION])

    potential_actions = np.linspace(MIN_ACTION, MAX_ACTION, 10000)
    potential_deltas = FUNCTION(potential_actions)
    TABLE = np.block([potential_actions.reshape(-1, 1), potential_deltas.reshape(-1, 1)])

    # MPC parameters
    n_steps = 1         # length per sample trajectory
    n_samples = 100    # number of trajectories to sample
    forward_weight = 0.0
    perp_weight = 0.0
    angle_weight = 0.0

    # run trials testing the MPC policy against the optimal policy
    n_trials = 1000
    max_steps = 200
    success_threshold = 1.0
    plot = False

    # n_trials = 2
    # max_steps = 120
    # start = np.array([11.1, 18.7])
    # goal = np.array([90.3, 71.5])
    # optimal_losses = np.empty(max_steps)
    # actual_losses = np.empty((n_trials, max_steps))
    # noises = np.random.normal(loc=0.0, scale=NOISE_STD, size=(max_steps, 2))
    # state = start.copy()

    # i = 0
    # while not np.linalg.norm(goal - state) < 0.2:
    #     optimal_losses[i] = np.linalg.norm(goal - state)
    #     noise = noises[i]
    #     state += FUNCTION(agent.optimal_policy(state, goal, TABLE)) + noise
    #     state = np.clip(state, MIN_STATE, MAX_STATE)
    #     i += 1
    # optimal_losses[i:] = np.linalg.norm(goal - state)

    # for k in trange(n_trials):
    #     state = start.copy()
    #     i = 0
    #     while not np.linalg.norm(goal - state) < 0.2:
    #         actual_losses[k, i] = np.linalg.norm(goal - state)
    #         noise = noises[i]
    #         action = agent.mpc_action(state, goal, state_range, action_range,
    #                                 n_steps=n_steps, n_samples=n_samples).detach().numpy()
    #         state += FUNCTION(action) + noise
    #         state = np.clip(state, MIN_STATE, MAX_STATE)
    #         i += 1
    #     actual_losses[k, i:] = np.linalg.norm(goal - state)
    
    # plt.plot(np.arange(max_steps), optimal_losses, 'g-', label="Optimal Controller")
    # plt.plot(np.arange(max_steps), actual_losses.mean(axis=0), 'b-', label="MPC Controller")
    # plt.title("Optimal vs MPC Controller Performance")
    # plt.legend()
    # plt.xlabel("Step\n\nstart = [11.1, 18.7], goal = [90.3, 71.5]\nAveraged over 20 MPC runs")
    # plt.ylabel("Distance to Goal")
    # plt.grid()
    # # plt.text(0.5, 0.01, "start = [11.1, 18.7], goal = [90.3, 71.5]", wrap=True, ha='center', fontsize=12)
    # plt.show()
    # set_trace()



    # perp_weights = np.linspace(0, 5., 3)
    # angle_weights = np.linspace(0, 1., 3)
    # forward_weights = np.linspace(0, 1., 3)
    # init_min, init_max = 20., 22.
    # goal_min, goal_max = 70., 72.
    # init_states = np.random.rand(n_trials, 2) * (init_max - init_min) + init_min
    # goals = np.random.rand(n_trials, 2) * (goal_max - goal_min) + goal_min
    # noises = np.random.normal(loc=0.0, scale=NOISE_STD, size=(n_trials, max_steps, 2))
    # results = []

    # optimal_lengths = np.empty(n_trials)
    # for trial in trange(n_trials, leave=False):
    #     init_state = init_states[trial]
    #     goal = goals[trial]

    #     state = init_state.copy()
    #     i = 0
    #     while not np.linalg.norm(goal - state) < success_threshold:
    #         noise = noises[trial, i] if args.stochastic else 0.0
    #         state += FUNCTION(agent.optimal_policy(state, goal, TABLE)) + noise
    #         if LIMIT:
    #             state = np.clip(state, MIN_STATE, MAX_STATE)
    #         i += 1
    #     optimal_lengths[trial] = i

    # count = 0
    # while True:
    #     for p in perp_weights:
    #         for a in angle_weights:
    #             for f in forward_weights:
    #                 print(f"{count}: f{f}, p{p}, a{a}")
    #                 count += 1
    #                 actual_lengths = np.empty(n_trials)
    #                 optimal = 0
    #                 # all_states = []
    #                 # all_actions = []
    #                 # all_goals = []
    #                 for trial in trange(n_trials):
    #                     init_state = init_states[trial]
    #                     goal = goals[trial]
                        
    #                     state = init_state.copy()
    #                     i = 0
    #                     states, actions = [], []
    #                     while not np.linalg.norm(goal - state) < success_threshold:
    #                         states.append(state)
    #                         if i == max_steps:
    #                             break
    #                         action = agent.mpc_action(state, goal, state_range, action_range,
    #                                             n_steps=n_steps, n_samples=n_samples, perp_weight=p,
    #                                             angle_weight=a, forward_weight=f).detach().numpy()
    #                         noise = noises[trial, i] if args.stochastic else 0.0
    #                         state += FUNCTION(action) + noise
    #                         if LIMIT:
    #                             state = np.clip(state, MIN_STATE, MAX_STATE)
    #                         i += 1
    #                         actions.append(action)

    #                     # all_states.append(states)
    #                     # all_actions.append(actions)
    #                     # all_goals.append(goal)
    #                     actual_lengths[trial] = i

    #                     if i <= optimal_lengths[trial]:
    #                         optimal += 1
                    
    #                 actual_lengths = np.array(actual_lengths)
    #                 results.append(np.abs(optimal_lengths.mean() - actual_lengths.mean()) / optimal_lengths.mean())
        
    #     # print("\noptimal mean:", optimal_lengths.mean())
    #     # print("optimal std:", optimal_lengths.std(), "\n")
    #     # print("actual mean:", actual_lengths.mean())
    #     # print("actual std:", actual_lengths.std(), "\n")
    #     # print("mean error:", np.abs(optimal_lengths.mean() - actual_lengths.mean()) / optimal_lengths.mean())
    #     # print("optimality rate:", optimal / float(n_trials))
    #     # print("timeout rate:", (actual_lengths == max_steps).sum() / float(n_trials), "\n")
        
    #     # if plot:
    #     #     plt.hist(optimal_lengths)
    #     #     plt.plot(optimal_lengths, actual_lengths, 'bo')
    #     #     plt.xlabel("Optimal Steps to Reach Goal")
    #     #     plt.ylabel("Actual Steps to Reach Goal")
    #     #     plt.show()
    #     print(np.argsort(results))
    #     set_trace()



    init_min, init_max = 20., 21.
    goal_min, goal_max = 70., 71.
    init_states = np.random.rand(n_trials, 2) * (init_max - init_min) + init_min
    goals = np.random.rand(n_trials, 2) * (goal_max - goal_min) + goal_min
    noises = np.random.normal(loc=0.0, scale=NOISE_STD, size=(n_trials, max_steps, 2))

    optimal_lengths = np.empty(n_trials)
    for trial in trange(n_trials):
        init_state = init_states[trial]
        goal = goals[trial]

        state = init_state.copy()
        i = 0
        while not np.linalg.norm(goal - state) < success_threshold:
            noise = noises[trial, i] if args.stochastic else 0.0
            state += FUNCTION(agent.optimal_policy(state, goal, TABLE)) + noise
            if LIMIT:
                state = np.clip(state, MIN_STATE, MAX_STATE)
            i += 1
        optimal_lengths[trial] = i

    while True:
        actual_lengths = np.empty(n_trials)
        optimal = 0
        # all_states = []
        # all_actions = []
        # all_goals = []
        for trial in trange(n_trials, leave=False):
            init_state = init_states[trial]
            goal = goals[trial]
            
            state = init_state.copy()
            i = 0
            states, actions = [], []
            while not np.linalg.norm(goal - state) < success_threshold:
                states.append(state)
                if i == max_steps:
                    break
                action = agent.mpc_action(state, init_state, goal, state_range, action_range,
                                    n_steps=n_steps, n_samples=n_samples, perp_weight=perp_weight,
                                    angle_weight=angle_weight, forward_weight=forward_weight).detach().numpy()
                noise = noises[trial, i] if args.stochastic else 0.0
                state += FUNCTION(action) + noise
                if LIMIT:
                    state = np.clip(state, MIN_STATE, MAX_STATE)
                i += 1
                actions.append(action)

            # all_states.append(states)
            # all_actions.append(actions)
            # all_goals.append(goal)
            actual_lengths[trial] = i

            if i <= optimal_lengths[trial]:
                optimal += 1
        
        optimal_lengths, actual_lengths = np.array(optimal_lengths), np.array(actual_lengths)
        print("\n------------------------")
        print("optimal mean:", optimal_lengths.mean())
        print("optimal std:", optimal_lengths.std(), "\n")
        print("actual mean:", actual_lengths.mean())
        print("actual std:", actual_lengths.std(), "\n")
        print("mean error:", np.abs(optimal_lengths.mean() - actual_lengths.mean()) / optimal_lengths.mean())
        print("optimality rate:", optimal / float(n_trials))
        print("timeout rate:", (actual_lengths == max_steps).sum() / float(n_trials))
        print("------------------------\n")

        if plot:
            plt.hist(optimal_lengths)
            plt.plot(optimal_lengths, actual_lengths, 'bo')
            plt.xlabel("Optimal Steps to Reach Goal")
            plt.ylabel("Actual Steps to Reach Goal")
            plt.show()
        set_trace()
