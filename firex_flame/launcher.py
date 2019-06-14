import os
from socket import gethostname
import subprocess
import time
import urllib.parse

from firexapp.broker_manager.broker_factory import BrokerFactory
from firexapp.common import get_available_port
from firexapp.fileregistry import FileRegistry
from firexapp.submit.tracking_service import TrackingService
from firexapp.submit.console import setup_console_logging
from firexapp.submit.uid import Uid

from firex_flame.flame_helper import DEFAULT_FLAME_TIMEOUT, wait_until_web_request_ok

FLAME_LOG_REGISTRY_KEY = 'FLAME_OUTPUT_LOG_REGISTRY_KEY2'
FileRegistry().register_file(FLAME_LOG_REGISTRY_KEY, os.path.join(Uid.debug_dirname, 'flame2.stdout'))

logger = setup_console_logging(__name__)


def get_flame_url(port, hostname=gethostname()):
    return 'http://%s:%d' % (hostname, int(port))


def _wait_web_server_alive(flame_url):
    webserver_wait_timeout = 10
    webserver_alive = wait_until_web_request_ok(urllib.parse.urljoin(flame_url, '/alive'),
                                                timeout=webserver_wait_timeout)
    if not webserver_alive:
        raise Exception("Flame web server at %s not up after %s seconds." % (flame_url, webserver_wait_timeout))


class FlameLauncher(TrackingService):
    def __init__(self):
        self.sync = None
        self.port = -1

    def extra_cli_arguments(self, arg_parser):
        arg_parser.add_argument('--flame_timeout', help='How long the webserver should run for, in seconds.',
                                default=DEFAULT_FLAME_TIMEOUT)

    def start(self, args, port=None, uid=None, **kwargs)->{}:
        # store sync & port state for later
        self.sync = args.sync
        self.port = int(port) if port else get_available_port()
        rec_file = os.path.join(uid.logs_dir, 'flame2.rec')

        # assemble startup cmd
        cmd_args = {
            'port': self.port,
            'broker': BrokerFactory.get_broker_url(),
            'uid': str(uid),
            'logs_dir': uid.logs_dir,
            'chain': args.chain,
            'recording': rec_file,
            'central_server': kwargs.get('central_firex_server', None),
            'flame_timeout': args.flame_timeout
        }

        non_empty_args_strs = ['--%s %s' % (k, v) for k, v in cmd_args.items() if v]
        cmd = 'firex_flame %s &' % ' '.join(non_empty_args_strs)

        # start the flame service and return the port
        flame_stdout = FileRegistry().get_file(FLAME_LOG_REGISTRY_KEY, uid.logs_dir)
        with open(flame_stdout, 'wb') as out:
            subprocess.check_call(cmd, shell=True, stdout=out, stderr=subprocess.STDOUT)
        # TODO: also wait for celery event listener be up.
        flame_url = get_flame_url(self.port)
        _wait_web_server_alive(flame_url)

        logger.info('Flame: %s' % flame_url)

    # TODO: this mechanism is unreliable.
    def __del__(self):
        if not self.sync:
            print('See Flame to monitor the status of your run at: %s' % get_flame_url(self.port))
