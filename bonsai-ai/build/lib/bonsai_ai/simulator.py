# Copyright (C) 2018 Bonsai, Inc.

import abc
import time
import datetime
from collections import deque

from tornado.ioloop import IOLoop

from bonsai_ai.exceptions import BonsaiClientError, SimStateError, \
    BonsaiServerError
from bonsai_ai.logger import Logger
from bonsai_ai.simulator_ws import Simulator_WS

log = Logger()

_CONNECT_TIMEOUT_SECS = 60


class _RateCounter(object):
    """
    A utility class for counting events and reporting their rate
    in `updates/sec`.
    """
    def __init__(self):
        self.reset()

    def update(self):
        """
        Call this at intervals to update the counter and rate.
        Uses an exponetial moving average.
        """
        self.count += 1
        delta = (time.time() - self.start)
        if delta is not 0:
            average = self.count / delta
            DECAY_RATE = 0.9
            self.rate = average * DECAY_RATE + self.rate * (1.0 - DECAY_RATE)

    def reset(self):
        """ resets the counters """
        self.count = 0
        self.rate = 0
        self.start = time.time()


class Simulator(object):
    """
    This class is used to interface with the server while training or
    running predictions against a BRAIN. It is an abstract base class,
    and to use it a developer must create a subclass.

    The `Simulator` class is closely related to the Inkling file that
    is associated with the BRAIN. The name used to construct `Simulator`
    must match the name of the simulator in the Inkling file.

    There are two main methods that you must override, `episode_start`
    and `simulate`. At the start of a session, `episode_start` is called
    once, then `simulate` is called repeatedly until the `terminal` flag is
    returned as `True` or the next `episode_start` interrupts the simulation.

    Attributes:
        brain:          The BRAIN to connect to.
        name:           The name of this Simulator. Must match simulator
                        in Inkling.
        objective_name: The name of the current objective for an episode.
        episode_reward: Cumulative reward for this episode so far.
        episode_count:  Number of completed episodes since sim launch.
        episode_rate:   Episodes per second. R/O
        iteration_count: Number of iterations for this episode.
        iteration_rate: Iterations per second

    Example Inkling:
        simulator my_simulator(Config)
            action (Action)
            state (State)
        end

    Example Code:
        class MySimulator(bonsai_ai.Simulator):
            def __init__(brain, name):
                super().__init__(brain, name)
                # your sim init code goes here.

            def episode_start(self, parameters=None):
                # your reset/init code goes here.
                return my_state

            def simulate(self, action):
                # your simulation stepping code goes here.
                return (my_state, my_reward, is_terminal)

            def episode_finish(self):
                print('Episode reward', self.episode_reward)

        ...

        config = bonsai_ai.Config(sys.argv)
        brain = bonsai_ai.Brain(config)
        sim = MySimulator(brain, "my_simulator")

        while sim.run():
            continue

    """
    __metaclass__ = abc.ABCMeta

    def __init__(self, brain, name):
        """
        Constructs the Simulator class.

        Arguments:
            brain: The BRAIN you wish to train or predict against.
            name:  The Simulator name. Must match the name in Inkling.
        """
        self.name = name
        self.brain = brain
        self._ioloop = IOLoop.current()
        self._impl = Simulator_WS(brain, self, name)

        # statistics
        self.episode_reward = 0
        self.episode_count = 0
        self._episode_rate = _RateCounter()
        self.iteration_count = 0
        # NOTE: _iteration_rate is accumulative, not per episode
        self._iteration_rate = _RateCounter()

    def __repr__(self):
        """ Return a JSON representation of the Simulator. """
        return '{{'\
            'name: {self.name!r}, ' \
            'objective_name: {self._impl.objective_name!r}, ' \
            'predict: {self.predict!r}, ' \
            'brain: {self.brain!r}, ' \
            'episode_reward: {self.episode_reward!r}, ' \
            'episode_count: {self.episode_count!r}, ' \
            'episode_rate: {self.episode_rate!r}, ' \
            'iteration_count: {self.iteration_count!r}, ' \
            'iteration_rate: {self.iteration_rate!r}' \
            '}}'.format(self=self)

    @property
    def predict(self):
        """ True if simulation is configured for prediction. """
        return self.brain.config.predict

    @property
    def objective_name(self):
        """ Current episode objective name. """
        return self._impl.objective_name

    @property
    def episode_rate(self):
        """ Episodes per second. """
        return int(self._episode_rate.rate)

    @property
    def iteration_rate(self):
        """ Iterations per second. """
        return int(self._iteration_rate.rate)

    @abc.abstractmethod
    def episode_start(self, episode_config):
        """
        Called at the start of each new episode. This callback passes in a
        set of initial parameters and expects an initial state in return for
        the simulator. Before this callback is called, the property
        `objective_name` will be updated to reflect the current objective
        for this episode.

        This call is where a simulation should be reset for the next round.

        Arguments:
            episode_config: A dict of config paramters defined in Inkling.

        Returns:
            A dictionary of the initial state of the simulation as defined
            in inkling.

        Example Inkling:
            schema Config
                UInt8 start_angle
            end

            schema State
                Float32 angle,
                Float32 velocity
            end

        Example Code:
            def episode_start(self, params):
                # training params are only passed in during training
                if self.predict == False:
                    print(self.objective_name)
                    self.angle = params.start_angle

                initial = {
                    "angle": self.angle,
                    "velocity": self.velocity,
                }
                return initial
        """
        return {}  # initial_state

    @abc.abstractmethod
    def simulate(self, action):
        """
        This callback steps the simulation forward by a single step.
        It passes in the `action` to be taken, and expects the resulting
        `state`, `reward` for the current `objective`, and a `terminal` flag
        used to signal the end of an episode. Note that an episode may be
        reset prematurely by the backend during training.

        For a multi-lesson curriculum, the `objective_name` will change from
        episode to episode. In this case ensure that the simulator is
        returning the correct reward for the different lessons.

        Returning `True` for the `terminal` flag signals the start of a
        new episode.

        Arguments:
            action:     A dict as defined in Inkling of the action to take.

        Returns:
            A tuple of (state, reward, terminal).
            state:    A dict as defined in Inkling of the sim state.
            reward:   A real number describing the reward for this step.
            terminal: True if the simulation has ended or terminated. False
                      if it should continue.

        Example Inkling:
            schema Action
                Int8{0, 1} delta
            end

        Example Code:
            def simulate(self, action):
                velocity = velocity - action.delta;
                terminal = (velocity <= 0.0)

                # reward is only needed during training
                if self.predict == False:
                    reward = reward_for_objective(self.objective_name)

                state = {
                    "velocity": self.velocity,
                    "angle": self.angle,
                }
                return (state, reward, terminal)
        """
        return {}  # state, reward, terminal

    def episode_finish(self):
        """
        This callback is called at the end of every episode before the next
        episode_start(). You can use it to do post episode cleanup
        or statistics reporting.
        """
        pass

    def standby(self, reason):
        log.info(reason)

    def _on_episode_start(self, episode_config):
        """ Callback hook for episode_start, called by event dispatcher """
        # update counters
        self.iteration_count = 0
        self.episode_reward = 0

        return self.episode_start(episode_config)

    def _on_simulate(self, action):
        """ Callback hook for simulate, called by event dispatcher. """
        # update counters
        self._iteration_rate.update()
        self.iteration_count += 1

        # step
        state, reward, terminal = self.simulate(action)
        self.episode_reward += reward
        return state, reward, terminal

    def _on_episode_finish(self):
        """ Callback hook for end of episode, called by event dispatcher. """
        # update counters
        self._episode_rate.update()
        self.episode_count += 1

        # userland callback
        self.episode_finish()

    def run(self):
        """
        Main loop call for driving the simulation. Returns `False` when the
        simulation has finished or halted.

        The client should call this method in a `while` loop until it
        returns `False`. To run for prediction, `brain.config.predict`
        must return `True`.

        Example:
            sim = MySimulator(brain)

            if sim.predict:
                print("Predicting against ", brain.name,
                      " version ", brain.version)
            else:
                print("Training ", brain.name)

            while sim.run():
                continue
        """
        try:
            success = self._ioloop.run_sync(self._impl.run, 1000)
        except KeyboardInterrupt:
            success = False
        except BonsaiClientError as e:
            log.error(e)
            raise e.original_exception
        except BonsaiServerError as e:
            log.error(e)
            success = False
        except SimStateError as e:
            log.error(e)
            raise e

        return success
