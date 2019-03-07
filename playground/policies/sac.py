"""
WARNING

WIP; NOT READY!
"""

import logging
import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp

from gym.spaces import Box

from playground.policies.base import BaseModelMixin, BaseTrainConfig, Policy
from playground.policies.memory import ReplayMemory, Transition
from playground.utils.misc import plot_learning_curve
from playground.utils.tf_ops import dense_nn


class SACPolicy(Policy, BaseModelMixin):

    def __init__(self, env, name, training=True, gamma=0.99, layer_sizes=None, clip_norm=None, **kwargs):
        Policy.__init__(self, env, name, training=training, gamma=gamma, **kwargs)
        BaseModelMixin.__init__(self, name)

        assert isinstance(self.env.action_space, Box), \
            "Current SACPolicy implementation only works for discrete action space."

        self.layer_sizes = [64, 64] if layer_sizes is None else layer_sizes
        self.clip_norm = clip_norm
        self._entropy_threshold = -self.act_dim[0]

    def act(self, state, eps=0.0):
        # add random gaussian noise for action exploration.
        action = self.sess.run(self.mu, {self.s: [state]})[0]
        action += eps * np.random.randn(*self.act_dim)
        action = np.clip(action, self.env.action_space.low, self.env.action_space.high)
        return action

    def _construct_gaussian_policy_network(self, input_state, reuse=False):
        """NOTE: to distinguish from stochastic policy `pi`, I use `mu` as a symbol for
        deterministic policy, `mu` does not refer to mean of a distribution here.
        """
        EPS = 1e-6

        with tf.variable_scope('mu', reuse=reuse):
            mean = dense_nn(input_state, self.layer_sizes + self.act_dim, name='mu_mean')
            logstd = dense_nn(input_state, self.layer_sizes + self.act_dim, name='mu_logstd')  # log() - so that it can be any number.
            logstd = tf.clip_by_value(logstd, -20.0, 2.0)
            std = tf.exp(logstd)

            x = mean + tf.random_normal(tf.shape(mean)) * std
            logp = tf.reduce_sum(
                -0.5 * (((x - mean) / (tf.exp(logstd) + EPS)) ** 2 + 2 * logstd + np.log(2 * np.pi)),
                axis=1
            )

            # dist = tfp.distributions.Normal(loc=mean, scale=std)
            # x = dist.sample()
            # logp = dist.log_prob(x)

            # apply squashing function
            mu = tf.tanh(mean)  # deterministic action
            pi = tf.tanh(x)  # stochastic action
            logp -= tf.reduce_sum(tf.log(1 - pi ** 2 + EPS), axis=1)

        return pi, logp

    def _build_Q_networks(self, name):
        arch = self.layer_sizes + [1]
        # Q(s_t, a_t)
        Q = dense_nn(tf.concat([self.s, self.a], axis=1), arch, name=name)
        mu_Q = dense_nn(tf.concat([self.s, self.mu], axis=1), arch, name=name, reuse=True)
        Q_vars = self.scope_vars(name)
        return Q, mu_Q, Q_vars

    def _build_networks(self):
        # Define input placeholders
        self.s = tf.placeholder(tf.float32, shape=[None] + self.state_dim, name='state')
        self.a = tf.placeholder(tf.float32, shape=[None] + self.act_dim, name='action')
        self.s_next = tf.placeholder(tf.float32, shape=[None] + self.state_dim, name='next_state')
        self.r = tf.placeholder(tf.float32, shape=(None,), name='reward')
        self.done = tf.placeholder(tf.float32, shape=(None,), name='done_flag')

        # Deterministic policy: predict the action vector a_t ~ mu(.|s_t)
        # mu_next - the action for the next state: a_{t+1} ~ mu(.|s_{t+1})
        # Construct policy network:
        self.mu, self.mu_logp = self._construct_gaussian_policy_network(self.s)
        self.mu_next, self.mu_logp_next = self._construct_gaussian_policy_network(self.s_next, reuse=True)
        self.mu_vars = self.scope_vars('mu')

        # construct V function
        self.V = dense_nn(self.s, self.layer_sizes + [1], name='V_main')
        self.V_target_next = dense_nn(self.s_next, self.layer_sizes + [1], name='V_target')
        self.V_vars = self.scope_vars('V_main')
        self.V_target_vars = self.scope_vars('V_target')
        assert len(self.V_vars) == len(self.V_target_vars)

        # Q function: we would like to learn two soft Q functions independently to
        # mitigate bias introduced during the policy improvement.
        self.Q1, self.mu_Q1, self.Q1_vars = self._build_Q_networks('Q1')
        self.Q2, self.mu_Q2, self.Q2_vars = self._build_Q_networks('Q2')

    def update_target_networks(self, mode='soft', tau=0.01):
        if mode == 'hard':
            self.sess.run([v_t.assign(v) for v_t, v in zip(self.V_target_vars, self.V_vars)])
        elif mode == 'soft':
            self.sess.run([v_t.assign(tau * v + (1.0 - tau) * v_t)
                           for v_t, v in zip(self.V_target_vars, self.V_vars)])
        else:
            raise ValueError(f"unknown update target network mode: '{mode}'")

    def _build_train_ops(self):
        self.alpha = tf.placeholder(tf.float32, shape=None, name='alpha')
        self.lr = tf.placeholder(tf.float32, shape=None, name='learning_rate')

        with tf.variable_scope('training_mu'):
            # The policy is trained to minimize KL divergence
            reg_mu = tf.reduce_mean([tf.nn.l2_loss(x) for x in self.mu_vars])

            # using reparametrization gradient; TODO why this does not work?
            # loss_mu = tf.reduce_mean(
            #      self.alpha * self.mu_logp - tf.minimum(self.mu_Q1, self.mu_Q2)
            # ) + 0.0001 * reg_mu

            # # using REINFORCE gradient; Psi_t = advantage value with an entropy reward
            loss_mu = - tf.reduce_mean(self.mu_logp * tf.stop_gradient(
                self.mu_Q1 - self.V - self.alpha * self.mu_logp)) + 0.0001 * reg_mu

        with tf.variable_scope('training_Q'):
            # Compute the regression target.
            y_q = self.r + self.gamma * self.V_target_next * (1.0 - self.done)
            # loss_Q1 = tf.reduce_mean(tf.square(self.Q1 - tf.stop_gradient(y_q)))
            # loss_Q2 = tf.reduce_mean(tf.square(self.Q2 - tf.stop_gradient(y_q)))
            loss_Q1 = tf.losses.huber_loss(tf.stop_gradient(y_q), self.Q1)
            loss_Q2 = tf.losses.huber_loss(tf.stop_gradient(y_q), self.Q2)

        with tf.variable_scope('training_V'):
            y_v = tf.minimum(self.mu_Q1, self.mu_Q2) - self.alpha * self.mu_logp
            loss_V = tf.losses.mean_squared_error(tf.stop_gradient(y_v), self.V)

        train_mu_op = tf.train.AdamOptimizer(self.lr).minimize(loss_mu)
        # train_Q1_op = tf.train.AdamOptimizer(self.lr).minimize(loss_Q1)
        # train_Q2_op = tf.train.AdamOptimizer(self.lr).minimize(loss_Q2)
        # train_V_op = tf.train.AdamOptimizer(self.lr).minimize(loss_V)
        train_values_op = tf.train.AdamOptimizer(self.lr).minimize(loss_Q1 + loss_Q2 + loss_V)

        self.losses = [loss_Q1, loss_Q2, loss_V, loss_mu]
        self.train_ops = [train_values_op, train_mu_op]

        with tf.variable_scope('summary'):
            self.ep_reward = tf.placeholder(tf.float32, name='episode_reward')
            self.summary = [
                tf.summary.scalar('loss_Q1', loss_Q1),
                tf.summary.scalar('loss_Q2', loss_Q2),
                tf.summary.scalar('loss_V', loss_V),
                tf.summary.scalar('loss_mu', loss_mu),
                tf.summary.scalar('Q1', tf.reduce_mean(self.Q1)),
                tf.summary.scalar('Q2', tf.reduce_mean(self.Q2)),
                tf.summary.scalar('V', tf.reduce_mean(self.V)),
                tf.summary.scalar('V_target', tf.reduce_mean(self.V_target_next)),
                tf.summary.scalar('mu_0', tf.reduce_mean(self.mu[..., 0])),
                tf.summary.scalar('mu_logp', tf.reduce_mean(self.mu_logp)),
                tf.summary.scalar('learning_rate', self.lr),
                tf.summary.scalar('avg_temperature_alpha', self.alpha),
                tf.summary.scalar('episode_reward', self.ep_reward)
            ]

            self.merged_summary = tf.summary.merge_all(key=tf.GraphKeys.SUMMARIES)

        self.sess.run(tf.global_variables_initializer())
        self.update_target_networks(mode='hard')  # initialization

    def build(self):
        self._build_networks()
        self._build_train_ops()

    class TrainConfig(BaseTrainConfig):
        alpha = 0.1
        tau = 0.01
        lr = 0.003
        lr_decay_steps = 1000
        lr_decay = 0.9
        batch_size = 64
        n_steps = 1e6
        buffer_capacity = 1e5
        log_interval = 100
        train_steps_per_loop = 1
        epsilon = 0.35
        epsilon_final = 0.0

    def train(self, config: BaseTrainConfig):
        self.buffer = ReplayMemory(capacity=config.buffer_capacity, tuple_class=Transition)
        self.n_episode = 0
        self.episode_reward = 0.
        self.reward_history = []
        self.reward_averaged = []

        lr = config.lr
        step = 0  # training step.

        eps = config.epsilon
        eps_drop_per_step = (eps - config.epsilon_final) / config.warmup_steps
        print("decrease `epsilon` per step:", eps_drop_per_step)

        while step < config.n_steps:

            rew = 0.
            ob = self.env.reset()
            done = False

            while not done:
                a = self.act(ob, eps)
                ob_next, r, done, info = self.env.step(a)
                rew += r
                record = Transition(self.obs_to_inputs(ob), a, r, self.obs_to_inputs(ob_next), done)
                self.buffer.add(record)
                ob = ob_next

                if eps > config.epsilon_final:
                    eps = max(config.epsilon_final, eps - eps_drop_per_step)

                if self.buffer.size >= config.batch_size and self.reward_history:
                    for _ in range(config.train_steps_per_loop):
                        batch = self.buffer.sample(config.batch_size)
                        feed_dict = {
                            self.lr: lr,
                            self.s: batch['s'],
                            self.a: batch['a'],
                            self.r: batch['r'],
                            self.s_next: batch['s_next'],
                            self.done: batch['done'],
                            self.alpha: config.alpha,
                            self.ep_reward: self.reward_history[-1],
                        }

                        _, summ_str = self.sess.run([self.train_ops, self.merged_summary], feed_dict=feed_dict)

                        self.writer.add_summary(summ_str, step)
                        self.update_target_networks(mode='soft', tau=config.tau)

                        step += 1

                        if step % config.lr_decay_steps == 0:
                            lr *= config.lr_decay

                        if step % config.log_interval == 0:
                            max_rew = np.max(self.reward_history)
                            avg10_rew = np.mean(self.reward_history[-10:])

                            # Report the performance every `every_step` steps
                            logging.info(
                                f"[episodes:{self.n_episode}/step:{step}], best:{max_rew:.2f}, avg:{avg10_rew:.2f}, "
                                f"buffer:{self.buffer.size}, lr:{lr:.4f}, exploring: {step < config.explore_steps}")
                            # self.save_checkpoint(step=step)

            # One trajectory is complete!
            self.reward_history.append(rew)
            self.reward_averaged.append(np.mean(self.reward_history[-10:]))
            self.n_episode += 1

        self.save_checkpoint(step=step)
        logging.info(f"[FINAL] episodes: {len(self.reward_history)}, Max reward: {np.max(self.reward_history)}, "
                     f"Average reward: {np.mean(self.reward_history)}")

        data_dict = {
            'reward': self.reward_history,
            'reward_smooth10': self.reward_averaged,
        }
        plot_learning_curve(self.model_name, data_dict, xlabel='episode')
