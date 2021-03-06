# Copyright (C) 2018 Bonsai, Inc.

import sys
from configparser import RawConfigParser
from os.path import expanduser, join
from os import environ
from argparse import ArgumentParser
import json

try:
    # Try python 3 import
    from urllib.parse import urljoin, urlparse, urlunparse
except ImportError:
    from urlparse import urljoin, urlparse, urlunparse

from bonsai_ai.logger import Logger


log = Logger()

# proxy environment variables on Unix systems
_ALL_PROXY = 'all_proxy'
_HTTP_PROXY = 'http_proxy'
_HTTPS_PROXY = 'https_proxy'

# .bonsai config file keys
_DEFAULT = 'DEFAULT'
_ACCESSKEY = 'accesskey'
_USERNAME = 'username'
_URL = 'url'
_PROXY = 'proxy'
_PROFILE = 'profile'

# Default bonsai api url
_DEFAULT_URL = 'https://api.bons.ai'

# env variables, used in hosted containers
_BONSAI_HEADLESS = 'BONSAI_HEADLESS'
_BONSAI_TRAIN_BRAIN = 'BONSAI_TRAIN_BRAIN'

# file names
_DOT_BONSAI = '.bonsai'
_DOT_BRAINS = '.brains'

# CLI help strings
_ACCESS_KEY_HELP = \
            'The access key to use when connecting to the BRAIN server. If ' \
            'specified, it will be used instead of any access key' \
            'information stored in a bonsai config file.'
_USERNAME_HELP = 'Bonsai username.'
_URL_HELP = \
    'Bonsai server URL. The URL should be of the form ' \
    '"https://api.bons.ai"'
_PROXY_HELP = 'Proxy server address and port. Example: localhost:3128'
_BRAIN_HELP = \
    """
    The name of the BRAIN to connect to. Unless a version is specified
    the BRAIN will connect for training.
    """
_PREDICT_HELP = \
    """
    If set, the BRAIN will connect for prediction with the specified
    version. May be a positive integer number or 'latest' for the most
    recent version.
        For example: --predict=latest or --predict=3
    """
_VERBOSE_HELP = "Enables logging. Alias for --log=all"
_PERFORMANCE_HELP = \
    "Enables time delta logging. Alias for --log=perf.all"
_LOG_HELP = \
    """
    Enable logging. Parameters are a list of log domains.
    Using --log=all will enable all domains.
    Using --log=none will disable logging.
    """

# legacy help strings
_TRAIN_BRAIN_HELP = "The name of the BRAIN to connect to for training."
_PREDICT_BRAIN_HELP = \
    """
    The name of the BRAIN to connect to for predictions. If you
    use this flag, you must also specify the --predict-version flag.
    """
_PREDICT_VERSION_HELP = \
    """
    The version of the BRAIN to connect to for predictions. This flag
    must be specified when --predict-brain is used. This flag will
    be ignored if it is specified along with --train-brain or
    --brain-url.
    """
_RECORDING_FILE_HELP = 'Unsupported.'


class Config(object):
    """
    Manages Bonsai configuration environments.

    Configuration information is pulled from different locations. This class
    helps keep it organized. Configuration information comes from environment
    variables, the user `~./.bonsai` file, a local `./.bonsai` file, the
    `./.brains` file, command line arguments, and finally, parameters
    overridden in code.

    An optional `profile` key can be used to switch between different
    profiles stored in the `~/.bonsai` configuration file. The users active
    profile is selected if none is specified.

    Attributes:
        accesskey:     Users access key from the web.
                        (Example: 00000000-1111-2222-3333-000000000001)
        username:      Users login name.
        url:           URL of the server to connect to.
                        (Example: "https://api.bons.ai")
        brain:         Name of the BRAIN to use.
        predict:       True is predicting against a BRAIN, False for training.
        brain_version: Version number of the brain to use for prediction.
        proxy:         Server name and port number of proxy to connect through.
                        (Example: "localhost:9000")

    Example Usage:
        import sys, bonsai_ai
        config = bonsai_ai.Config(sys.argv)
        print(config)
        if config.predict:
            ...

    """
    def __init__(self, argv=sys.argv, profile=None):
        """
        Construct Config object with program arguments.
        Pass in sys.argv for command-line arguments and an
        optional profile name to select a specific profile.

        Arguments:
            argv:    A list of argument strings.
            profile: The name of a profile to select. (optional)
        """
        self.accesskey = None
        self.username = None
        self.url = None

        self.brain = None

        self.predict = False
        self.brain_version = 0
        self._proxy = None

        self.verbose = False
        self._config = self._read_config()
        self.profile = profile

        self._parse_env()
        self._parse_config(_DEFAULT)
        self._parse_config(profile)
        self._parse_brains()
        self._parse_args(argv)

        # parse args works differently in 2.7
        if sys.version_info >= (3, 0):
            self._parse_legacy(argv)

    def __repr__(self):
        """ Prints out a JSON formatted string of the Config state. """
        return '{{'\
            'profile: {self.profile!r} ' \
            'accesskey: {self.accesskey!r}, ' \
            'username: {self.username!r}, ' \
            'brain: {self.brain!r}, ' \
            'url: {self.url!r}, ' \
            'predict: {self.predict!r}, ' \
            'brain_version: {self.brain_version!r}, ' \
            'proxy: {self.proxy!r}' \
            '}}'.format(self=self)

    @property
    def proxy(self):
        if self._proxy is not None:
            return self._proxy

        proxy = environ.get(_ALL_PROXY, None)

        http_proxy = environ.get(_HTTP_PROXY, None)
        if http_proxy is not None:
            proxy = http_proxy

        if self.url is not None:
            uri = urlparse(self.url)
            if uri.scheme == 'https':
                https_proxy = environ.get(_HTTPS_PROXY, None)
                if https_proxy is not None:
                    proxy = https_proxy

        return proxy

    @proxy.setter
    def proxy(self, proxy):
        uri = urlparse(proxy)
        uri.port
        self._proxy = proxy

    def _parse_env(self):
        ''' parse out environment variables used in hosted containers '''
        self.brain = environ.get(_BONSAI_TRAIN_BRAIN, None)
        headless = environ.get(_BONSAI_HEADLESS, None)
        if headless == 'True':
            self.headless = True

    def _parse_config(self, profile):
        ''' parse both the '~/.bonsai' and './.bonsai' config files. '''
        config_parser = self._read_config()

        # read the values
        def assign_key(key):
            if config_parser.has_option(section, key):
                self.__dict__[key] = config_parser.get(section, key)

        # get the profile
        section = _DEFAULT
        if profile is None:
            if config_parser.has_option(_DEFAULT, _PROFILE):
                section = config_parser.get(_DEFAULT, _PROFILE)
                self.profile = section
        else:
            section = profile

        # if url is none set it to default bonsai api url
        if self.url is None:
            config_parser.set(self.profile, _URL, _DEFAULT_URL)

        assign_key(_ACCESSKEY)
        assign_key(_USERNAME)
        assign_key(_URL)
        assign_key(_PROXY)

    def _parse_brains(self):
        ''' parse the './.brains' config file
            Example:
                {"brains": [{"default": true, "name": "test"}]}
        '''
        data = {}
        try:
            with open(_DOT_BRAINS) as file:
                data = json.load(file)

                # parse file now
                for brain in data['brains']:
                    if brain['default'] is True:
                        self.brain = brain['name']
                        return

        # except FileNotFoundError: python3
        except IOError as e:
            return

    def _parse_legacy(self, argv):
        ''' print support for legacy CLI arguments '''
        if sys.version_info >= (3, 0):
            optional = ArgumentParser(
                description="",
                allow_abbrev=False,
                add_help=False)
        else:
            optional = ArgumentParser(
                description="",
                add_help=False)

        optional.add_argument(
            '--legacy',
            action='store_true',
            help='Legacy command line options')
        optional.add_argument('--train-brain', help=_TRAIN_BRAIN_HELP)
        optional.add_argument('--predict-brain', help=_PREDICT_BRAIN_HELP)
        optional.add_argument('--predict-version', help=_PREDICT_VERSION_HELP)
        optional.add_argument('--recording-file', help=_RECORDING_FILE_HELP)
        args, remainder = optional.parse_known_args(argv)

        if args.train_brain is not None:
            self.brain = args.train_brain
            self.predict = False

        if args.predict_version is not None:
            self.predict = True
            if args.predict_version == "latest":
                self.brain_version = 0
            else:
                self.brain_version = int(args.predict_version)

    def _parse_args(self, argv):
        ''' parser command line arguments '''
        if sys.version_info >= (3, 0):
            parser = ArgumentParser(allow_abbrev=False)
        else:
            parser = ArgumentParser()

        parser.add_argument('--accesskey', help=_ACCESS_KEY_HELP)
        parser.add_argument('--username', help=_USERNAME_HELP)
        parser.add_argument('--url', help=_URL_HELP)
        parser.add_argument('--proxy', help=_PROXY_HELP)
        parser.add_argument('--brain', help=_BRAIN_HELP)
        parser.add_argument(
            '--predict',
            help=_PREDICT_HELP,
            nargs='?',
            const='latest',
            default=None)
        parser.add_argument('--verbose', action='store_true',
                            help=_VERBOSE_HELP)
        parser.add_argument('--performance', action='store_true',
                            help=_PERFORMANCE_HELP)
        parser.add_argument('--log', nargs='+', help=_LOG_HELP)

        args, remainder = parser.parse_known_args(argv[1:])

        if args.accesskey is not None:
            self.accesskey = args.accesskey

        if args.username is not None:
            self.username = args.username

        if args.url is not None:
            self.url = args.url

        if args.proxy is not None:
            self.proxy = args.proxy

        if args.brain is not None:
            self.brain = args.brain

        if args.verbose:
            self.verbose = args.verbose
            log.set_enable_all(args.verbose)

        if args.performance:
            # logging::log().set_enabled(true);
            # logging::log().set_enable_all_perf(true);
            pass

        if args.log is not None:
            for domain in args.log:
                log.set_enabled(domain)

        brain_version = None
        if args.predict is not None:
            if args.predict == "latest":
                brain_version = 0
            else:
                brain_version = args.predict
            self.predict = True

        # update brain_version after all args have been processed
        if brain_version is not None:
            brain_version = int(brain_version)
            if brain_version < 0:
                raise ValueError(
                    'BRAIN version number must be'
                    'positive integer or "latest".')
            self.brain_version = brain_version

    def _make_section(self, key):
        if (not self._config.has_section(key) and key != _DEFAULT):
            self._config.add_section(key)

    def _read_config(self):
        config_path = join(expanduser('~'), _DOT_BONSAI)
        config = RawConfigParser(allow_no_value=True)
        config.read([config_path, _DOT_BONSAI])
        return config

    def _set_profile(self, section):
        self._make_section(section)
        self.profile = section
        if section == _DEFAULT:
            self._config.remove_option(_DEFAULT, _PROFILE)
        else:
            self._config.set(_DEFAULT, _PROFILE, str(section))

    def _write(self):
        config_path = join(expanduser('~'), _DOT_BONSAI)
        with open(config_path, 'w') as f:
            self._config.write(f)

    def _websocket_url(self):
        """ Converts api url to websocket url """
        api_url = self.url
        parsed_api_url = urlparse(api_url)

        if parsed_api_url.scheme == 'http':
            parsed_ws_url = parsed_api_url._replace(scheme='ws')
        elif parsed_api_url.scheme == 'https':
            parsed_ws_url = parsed_api_url._replace(scheme='wss')
        else:
            return None
        ws_url = urlunparse(parsed_ws_url)
        return ws_url

    def _has_section(self, section):
        """Checks the configuration to see if section exists."""
        if section == _DEFAULT:
            return True
        return self._config.has_section(section)

    def _section_list(self):
        """ Returns a list of sections in config """
        return self._config.sections()

    def _section_items(self, section):
        """ Returns a dictionary of items in a section """
        return self._config.items(section)

    def _update(self, **kwargs):
        """Updates the configuration with the Key/value pairs in kwargs."""
        if not kwargs:
            return
        for key, value in kwargs.items():
            if key.lower() == _PROFILE.lower():
                self._set_profile(value)
            else:
                self._config.set(self.profile, key, str(value))
        self._write()
        self._parse_config(self.profile)
