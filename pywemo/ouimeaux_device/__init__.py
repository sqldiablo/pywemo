"""
Base WeMo Device class
"""

import logging
import time

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

import requests
#from requests import ConnectTimeout
#from requests import ConnectionError
#from requests import Timeout

from .api.service import Service
from .api.xsd import device as deviceParser

LOG = logging.getLogger(__name__)

# Start with the most commonly used port
PROBE_PORTS = (49153, 49152, 49154, 49151, 49155, 49156, 49157, 49158, 49159)


def probe_wemo(host, ports=PROBE_PORTS, probe_timeout=10):
    """Probe a host for the current port.

    This probes a host for known-to-be-possible ports and
    returns the one currently in use. If no port is discovered
    then it returns None.
    """
    for port in ports:
        try:
            r = requests.get('http://%s:%i/setup.xml' % (host, port),
                             timeout=probe_timeout)
            if ('WeMo' in r.text) or ('Belkin' in r.text):
                return port
        except requests.ConnectTimeout:
            # If we timed out connecting, then the wemo is gone,
            # no point in trying further.
            LOG.debug('Timed out connecting to %s on port %i, '
                      'wemo is offline', host, port)
            break
        except requests.Timeout:
            # Apparently sometimes wemos get into a wedged state where
            # they still accept connections on an old port, but do not
            # respond. If that happens, we should keep searching.
            LOG.debug('No response from %s on port %i, continuing',
                      host, port)
            continue
        except requests.ConnectionError:
            pass
    return None


def probe_device(device):
    """Probe a device for available port.

    This is an extension for probe_wemo, also probing current port.
    """
    ports = list(PROBE_PORTS)
    if device.port in ports:
        ports.remove(device.port)
    ports.insert(0, device.port)

    return probe_wemo(device.host, ports)


class UnknownService(Exception):
    pass


class Device(object):
    def __init__(self, url, mac):
        self._state = None
        self.basic_state_params = {}
        base_url = url.rsplit('/', 1)[0]
        parsed_url = urlparse(url)
        self.host = parsed_url.hostname
        self.port = parsed_url.port
        self.retrying = False
        self.mac = mac
        xml = requests.get(url, timeout=10)
        self._config = deviceParser.parseString(xml.content).device
        sl = self._config.serviceList
        self.services = {}
        for svc in sl.service:
            svcname = svc.get_serviceType().split(':')[-2]
            service = Service(self, svc, base_url)
            service.eventSubURL = base_url + svc.get_eventSubURL()
            self.services[svcname] = service
            setattr(self, svcname, service)

    def _reconnect_with_device_by_discovery(self):
        """
        Wemos tend to change their port number from time to time.
        Whenever requests throws an error, we will try to find the device again
        on the network and update this device. """

        # Put here to avoid circular dependency
        from ..discovery import discover_devices

        LOG.info("Trying to reconnect with %s", self.name)
        # We will try to find it 5 times, each time we wait a bigger interval
        try_no = 0

        while True:
            found = discover_devices(st=None, max_devices=1,
                                     match_mac=self.mac,
                                     match_serial=self.serialnumber)

            if found:
                LOG.info("Found %s again, updating local values", self.name)

                self.__dict__ = found[0].__dict__
                self.retrying = False
                return

            wait_time = try_no * 5

            LOG.info(
                "%s Not found in try %i. Trying again in %i seconds",
                self.name, try_no, wait_time)

            if try_no == 5:
                LOG.error(
                    "Unable to reconnect with {} in 5 tries. Stopping.".
                    format(self.name))
                self.retrying = False
                return

            time.sleep(wait_time)

            try_no += 1

    def _reconnect_with_device_by_probing(self):
        port = probe_device(self)
        if port is None:
            LOG.error('Unable to re-probe wemo at {}'.format(self.host))
            return False
        LOG.info('Reconnected to wemo at {} on port {}'.format(
            self.host, port))
        self.port = port
        url = 'http://{}:{}/setup.xml'.format(self.host, self.port)
        self.__dict__ = self.__class__(url, None).__dict__
        return True

    def reconnect_with_device(self):
        ret_val = None

        if self.rediscovery_enabled and not self.rediscovery_pending:
            try:
                self.rediscovery_pending = True

                LOG.debug("Attempting to rediscover wemo at %s by probing for a new port...",
                          self.host)
                device = self._reconnect_with_device_by_probing()

                if not device and (self.mac or self.serialnumber):
                    LOG.debug("Attempting to rediscover wemo at %s by ssdp discovery...",
                              self.host)
                    device = self._reconnect_with_device_by_discovery()

                if not device:
                    self.update_config(device)
#                    ret_val = device.url
            except Exception:
                LOG.error('Error while rediscovering wemo at %s: %s',
                          self.url, traceback.format_exc())
            finally:
                self.rediscovery_pending = False

        return ret_val

    def parse_basic_state(self, params):
        # BinaryState
        # 1|1492338954|0|922|14195|1209600|0|940670|15213709|227088884
        (
            state,  # 0 if off, 1 if on,
            _x1,
            _x2,
            _x3,
            _x4,
            _x5,
            _x6,
            _x7,
            _x8,
            _x9
        ) = params.split('|')
        return {'state': state}

    def update_binary_state(self):
        self.basic_state_params = self.basicevent.GetBinaryState()

    def subscription_update(self, _type, _params):
        LOG.debug("subscription_update %s %s", _type, _params)
        if _type == "BinaryState":
            try:
                self._state = int(self.parse_basic_state(_params).get("state"))
            except ValueError:
                self._state = 0
            return True
        return False

    def get_state(self, force_update=False):
        """
        Returns 0 if off and 1 if on.
        """
        if force_update or self._state is None:
            state = self.basicevent.GetBinaryState() or {}

            try:
                self._state = int(state.get('BinaryState', 0))
            except ValueError:
                self._state = 0

        return self._state

    def get_service(self, name):
        try:
            return self.services[name]
        except KeyError:
            raise UnknownService(name)

    def list_services(self):
        return list(self.services.keys())

    def explain(self):
        for name, svc in self.services.items():
            print(name)
            print('-' * len(name))
            for aname, action in svc.actions.items():
                print("  %s(%s)" % (aname, ', '.join(action.args)))
            print()

    @property
    def model(self):
        return self._config.get_modelDescription()

    @property
    def model_name(self):
        return self._config.get_modelName()

    @property
    def name(self):
        return self._config.get_friendlyName()

    @property
    def serialnumber(self):
        return self._config.get_serialNumber()
