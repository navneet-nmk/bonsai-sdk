import gym
import argparse
import logging
import os
from time import sleep, time
from bonsai_ai import Simulator, Brain, Config
import sys

# log = logging.getLogger(__name__)
log = logging.getLogger('gym_simulator')


class GymSimulator(Simulator):
    """ GymSimulator class

        End users should subclass GymSimulator to interface OpenAI Gym
        environments to the Bonsai platform. A minimal subclass must
        implement `gym_to_state()` and `action_to_gym()`, as well as
        specify the `simulator_name` and `environment_name`.

        To start the simulation for training, call `run_gym()`.

        In your Inkling config schema you can optionally add
        an `iteration_limit` Integer which will be used to set the
        maximum number of iterations per episode.
    """
    simulator_name = None    # name of the simulation in the inkling file
    environment_name = None  # name of the OpenAI Gym environment

    def __init__(self, brain, iteration_limit=0):
        """ initialize the GymSimulator with a bonsai.Config,
            the class variables will be used to setup the environment
            and simulator name as specified in inkling
        """
        super(GymSimulator, self).__init__(brain, self.simulator_name)

        # create the gym environment
        self._env = gym.make(self.environment_name)

        # parse optional command line arguments
        cli_args = self._parse_arguments()
        if cli_args is None:
            return

        # optional parameters for controlling the simulation
        self._headless = cli_args.headless
        self._iteration_limit = iteration_limit    # default is no limit

        # book keeping for rate status
        self._log_interval = 10.0  # seconds
        self._last_status = time()

    # convert openai gym observation to our state schema
    def gym_to_state(self, observation):
        """Convert a gym observation into an Inkling state

        Example:
            state = {'position': observation[0],
                     'velocity': observation[1],
                     'angle':    observation[2],
                     'rotation': observation[3]}
            return state

        :param observation: gym observation, see specific gym
            environment for details.
        :return A dictionary matching the Inkling state schema.
        """
        return None

    # convert our action schema into openai gym action
    def action_to_gym(self, action):
        """Convert an Inkling action schema into a gym action.

        Example:
            return action['command']

        :param action: A dictionary as defined in the Inkling schema.
        :return A gym action as defined in the gym environment
        """
        return action['command']

    def gym_episode_start(self, parameters):
        """
        called during episode_start() to return the initial observation
        after reseting the gym environment. clients can override this
        to provide additional initialization.
        """
        observation = self._env.reset()
        return observation

    def episode_start(self, parameters):
        """ called at the start of each new episode
        """

        # optional configuration arguments for open-ai-gym
        if "iteration_limit" in parameters:
            self._iteration_limit = parameters["iteration_limit"]

        # initial observation
        observation = self.gym_episode_start(parameters)
        state = self.gym_to_state(observation)
        return state

    def gym_simulate(self, gym_action):
        """
        called during simulate to single step the gym environment
        and return (observation, reward, done, info).
        clients can override this method to provide additional
        reward shaping.
        """
        observation, reward, done, info = self._env.step(gym_action)
        return observation, reward, done, info

    def simulate(self, action):
        """ step the simulation, optionally rendering the results
        """
        # simulate
        gym_action = self.action_to_gym(action)
        observation, reward, done, info = self.gym_simulate(gym_action)

        # episode limits
        if (self._iteration_limit > 0):
            if (self.iteration_count >= self._iteration_limit):
                done = True

        # render if not headless
        if not self._headless:
            if 'human' in self._env.metadata['render.modes']:
                self._env.render()

        # print a periodic status of iterations and episodes
        self._periodic_status_update()

        # convert state and return to the server
        state = self.gym_to_state(observation)
        return state, reward, done

    def episode_finish(self):
        # log how this episode went
        log.info("Episode %s reward is %s",
                 self.episode_count, self.episode_reward)
        self._last_status = time()

    def standby(self, reason):
        """ report standby messages from the server
        """
        log.info('standby: %s', reason)
        sleep(1)
        return True

    def run_gym(self):
        """ runs the simulation until cancelled or finished
        """
        # update brain to make sure we're in sync
        self.brain.update()

        # train
        while self.run():
            continue

        # success
        log.info('Finished running %s', self.name)

    def _periodic_status_update(self):
        """ print a periodic status update showing iterations/sec
        """
        if time() - self._last_status > self._log_interval:
            log.info("Episode %s is still running, reward so far is %s",
                     self.episode_count, self.episode_reward)
            self._last_status = time()

    def _parse_arguments(self):
        """ parses command line arguments and returns them as a list
        """
        headless_help = (
            "The simulator can be run with or without the graphical "
            "environment. By default the graphical environment is shown. "
            "Using --headless will run the simulator without graphical "
            "output. This may be set as BONSAI_HEADLESS in the environment.")
        parser = argparse.ArgumentParser()
        parser.add_argument('--headless',
                            help=headless_help,
                            action='store_true',
                            default=os.environ.get('BONSAI_HEADLESS', False))
        try:
            args, unknown = parser.parse_known_args()
        except SystemExit:
            # --help specified by user. Continue, so as to print rest of help
            # from (brain_server_connection).
            print('')
            return None
        return args
