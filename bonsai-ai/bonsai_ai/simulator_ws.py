# Copyright (C) 2018 Bonsai, Inc.

# tornado
from tornado import gen
from tornado.websocket import websocket_connect, WebSocketClosedError, \
    StreamClosedError
from tornado.httpclient import HTTPRequest

# protobuf
from google.protobuf.json_format import MessageToJson

# inkling
from bonsai_ai.proto.generator_simulator_api_pb2 import ServerToSimulator
from bonsai_ai.proto.generator_simulator_api_pb2 import SimulatorToServer

# bonsai
from bonsai_ai.common.state_to_proto import convert_state_to_proto
from bonsai_ai.exceptions import SimulateError, EpisodeStartError, \
    BonsaiServerError

from bonsai_ai.inkling_factory import InklingMessageFactory
from bonsai_ai.logger import Logger


_CONNECT_TIMEOUT_SECS = 60

log = Logger()


class Simulator_WS(object):
    class SimStep(object):
        """
        Internal class used for keeping track of batch-processed
        round trips through the simulator. Packed into a protobuf
        message at the end and sent over the wire.

        """
        def __init__(self):
            self.prediction = None
            self.state = None
            self.reward = 0
            self.terminal = False

    def __init__(self, brain, sim, simulator_name):
        self.brain = brain
        self.name = simulator_name
        self.objective_name = None
        self._sim = sim

        self._ws = None
        self._prev_message_type = ServerToSimulator.UNKNOWN

        # acknowledge_register
        # schemas are of type DescriptorProto
        self._properties_schema = None
        self._output_schema = None
        self._prediction_schema = None
        self._sim_id = -1

        # set_properties
        self._init_properties = {}
        # TODO(oren.leiman): Pretty sure this is vestigial.
        # self._initial_prediction_schema = None

        # current batch of simulation steps
        self._sim_steps = []

        # Caching actions for predictor
        self._predictor_action = None

        # protobuf discriptor cache
        self._inkling = InklingMessageFactory()

        self._dispatch_send = {
            ServerToSimulator.UNKNOWN:
                '_send_registration',
            ServerToSimulator.ACKNOWLEDGE_REGISTER:
                '_send_initial_state' if self._sim.predict else '_send_ready',
            ServerToSimulator.SET_PROPERTIES:
                '_send_initial_state' if self._sim.predict else '_send_ready',
            ServerToSimulator.START:
                '_send_initial_state',
            ServerToSimulator.PREDICTION:
                '_send_state',
            ServerToSimulator.RESET:
                '_send_initial_state' if self._sim.predict else '_send_ready',
            ServerToSimulator.STOP:
                '_send_initial_state' if self._sim.predict else '_send_ready',
        }

        self._dispatch_recv = {
            ServerToSimulator.ACKNOWLEDGE_REGISTER:
                '_on_acknowledge_register',
            ServerToSimulator.SET_PROPERTIES:
                '_on_set_properties',
            ServerToSimulator.START:
                '_on_start',
            ServerToSimulator.PREDICTION:
                '_on_prediction',
            ServerToSimulator.RESET:
                '_on_reset',
            ServerToSimulator.STOP:
                '_on_stop',
            ServerToSimulator.FINISHED:
                '_on_finished'
        }

    def _new_state_message(self):
        """
        Generate an InklingMessage for holding simulator state
        :return: state message
        """
        return self._inkling.new_message_from_proto(self._output_schema)

    def _dict_for_message(self, message):
        """
        Unpack a protobuf message into a Python dictionary
        :return: dictionary
        """
        result = {}
        # If the message is bogus, return an empty dictionary rather
        # than crashing.
        if message is not None:
            for field in message.DESCRIPTOR.fields:
                result[field.name] = getattr(message, field.name)
        return result

    def _send_registration(self, to_server):
        log.simulator_ws('Sending Registration')
        to_server.message_type = SimulatorToServer.REGISTER
        to_server.register_data.simulator_name = self.name

    def _send_ready(self, to_server):
        log.simulator_ws('Sending Ready')
        to_server.message_type = SimulatorToServer.READY
        to_server.sim_id = self._sim_id

    def _send_initial_state(self, to_server):
        log.simulator_ws('Sending initial State')
        to_server.message_type = SimulatorToServer.STATE
        to_server.sim_id = self._sim_id
        state = to_server.state_data.add()

        try:
            initial_state = self._sim._on_episode_start(self._init_properties)
        except Exception as e:
            raise EpisodeStartError(e)

        state_message = self._new_state_message()
        convert_state_to_proto(state_message, initial_state)
        state.state = state_message.SerializeToString()
        state.reward = 0.0
        state.terminal = False
        # state.action_taken = ... # no-op for init state

    def _send_state(self, to_server):
        log.simulator_ws('Sending State')
        to_server.message_type = SimulatorToServer.STATE
        to_server.sim_id = self._sim_id
        for step in self._sim_steps:
            if step.state:
                state = to_server.state_data.add()
                state.state = step.state.SerializeToString()
                state.reward = step.reward
                state.terminal = step.terminal
                state.action_taken = step.prediction
            else:
                log.simulator("WARNING: Missing step in send_state")
        self._sim_steps = []

    def _on_acknowledge_register(self, from_server):
        log.simulator_ws('Acknowledging Registration')
        data = from_server.acknowledge_register_data
        self._properties_schema = data.properties_schema
        self._output_schema = data.output_schema
        self._prediction_schema = data.prediction_schema
        self._sim_id = data.sim_id

    def _on_set_properties(self, from_server):
        log.simulator_ws('Setting properties')
        data = from_server.set_properties_data
        self._prediction_schema = data.prediction_schema
        self.objective_name = data.reward_name
        dynamic_properties = data.dynamic_properties
        properties_message = self._inkling.message_for_dynamic_message(
            dynamic_properties, self._properties_schema)
        self._init_properties = self._dict_for_message(properties_message)

    def _on_start(self, from_server):
        pass

    def _on_prediction(self, from_server):
        log.simulator_ws('On Prediction')
        for p_data in from_server.prediction_data:
            step = self.SimStep()
            step.prediction = p_data.dynamic_prediction
            self._sim_steps.append(step)

            # Convert server msg to action dict and saves it for predictor
            self._cache_action_for_predictor(step.prediction)

    def _on_reset(self, from_server):
        pass

    def _on_stop(self, from_server):
        self._sim._on_episode_finish()

    def _on_finished(self, from_server):
        pass

    def _dump_message(self, message, fname):
        '''Helper function for dumping protobuf message contents'''
        with open(fname, 'wb') as f:
            f.write(message.SerializeToString())

    def _on_send(self, to_server):
        ''' message handler for sending messages to the server '''
        method_name = self._dispatch_send.get(
            self._prev_message_type, 'default')
        method = getattr(self, method_name, lambda: log.simulator("Finished"))
        method(to_server)

    def _on_recv(self, from_server):
        ''' message handler for server messages '''
        def _raise(msg):
            raise BonsaiServerError(
                "Received unknown message ({}) from server".format(
                    msg.message_type))

        method_name = self._dispatch_recv.get(
            from_server.message_type, 'default')
        method = getattr(self, method_name, _raise)
        method(from_server)
        self._prev_message_type = from_server.message_type

    def _cache_action_for_predictor(self, prediction):
        """ Converts a server prediction into an action dictionary and saves it
            for the predictor class """
        action_message = self._inkling.message_for_dynamic_message(
            prediction, self._prediction_schema)
        self._predictor_action = self._dict_for_message(action_message)

    @gen.coroutine
    def _connect(self):
        """
        Fire up a websocket connection.
        """
        try:
            if self._sim.predict is True:
                url = self.brain._prediction_url()
            else:
                url = self.brain._simulation_url()

            log.info("trying to connect: {}".format(url))
            req = HTTPRequest(
                url,
                connect_timeout=_CONNECT_TIMEOUT_SECS,
                request_timeout=_CONNECT_TIMEOUT_SECS)
            req.headers['Authorization'] = self.brain.config.accesskey
            req.headers['User-Agent'] = self.brain._user_info

            self._ws = yield websocket_connect(req)
        except Exception as e:
            raise gen.Return(repr(e))
        else:
            raise gen.Return(None)

    def _advance(self, step):
        """ Helper function to advance the simulator and process the resulting
        state for transmission.
        """
        log.simulator_ws('Advancing')
        action_message = self._inkling.message_for_dynamic_message(
            step.prediction, self._prediction_schema)
        action = self._dict_for_message(action_message)

        try:
            state, reward, terminal = self._sim._on_simulate(action)
        except Exception as e:
            raise SimulateError(e)

        state_message = self._new_state_message()
        convert_state_to_proto(state_message, state)
        log.simulator("{}".format(MessageToJson(state_message)))
        step.state = state_message
        step.reward = reward
        step.terminal = terminal
        if terminal:
            try:
                self._sim._on_episode_finish()
                self._sim._on_episode_start(self._init_properties)
            except Exception as e:
                raise EpisodeStartError(e)

    @gen.coroutine
    def close_connection(self):
        """ Close websocket connection """
        yield self._ws.close()

    @gen.coroutine
    def run(self):
        """ Run loop called from Simulator. Encapsulates one round trip
        to the backend, which might include a simulation loop.
        """
        # Grab a web socket connection if needed
        if self._ws is None:
            message = yield self._connect()
            # If the connection failed, report
            if message is not None:
                raise BonsaiServerError(
                    "Error while connecting to websocket: {}".format(message))

        # If there is a batch of predictions cued up, step through it
        if self._prev_message_type == ServerToSimulator.PREDICTION:
            for step in self._sim_steps:
                self._advance(step)

        # send message to server
        to_server = SimulatorToServer()

        self._on_send(to_server)

        if (to_server.message_type):
            out_bytes = to_server.SerializeToString()
            try:
                yield self._ws.write_message(out_bytes, binary=True)
            except (StreamClosedError, WebSocketClosedError) as e:
                raise BonsaiServerError(
                    "Websocket connection closed. Code: {}, Reason: {}".format(
                        self._ws.close_code, self._ws.close_reason))

        # read response from server
        in_bytes = yield self._ws.read_message()
        if in_bytes is None:
            raise BonsaiServerError(
                "Websocket connection closed. Code: {}, Reason: {}".format(
                    self._ws.close_code, self._ws.close_reason))

        from_server = ServerToSimulator()
        from_server.ParseFromString(in_bytes)
        self._on_recv(from_server)

        if self._prev_message_type == ServerToSimulator.FINISHED:
            yield self._ws.close()
            raise gen.Return(False)

        # You've come this far, celebrate!
        raise gen.Return(True)
