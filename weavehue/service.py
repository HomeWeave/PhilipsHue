import time
import json
from collections.abc import MutableMapping
from pathlib import Path
from threading import Thread

from pyhuelights.registration import RegistrationWatcher
from pyhuelights.registration import AuthenticatedHueConnection
from pyhuelights.registration import REGISTRATION_FAILED, REGISTRATION_SUCCEEDED
from pyhuelights.manager import LightsManager
from pyhuelights.discovery import DefaultDiscovery, DiscoveryFailed
from pyhuelights.core import HueApp

from pyantonlib.plugin import AntonPlugin
from pyantonlib.channel import GenericInstructionController
from pyantonlib.channel import GenericEventController
from pyantonlib.utils import log_info
from anton.plugin_pb2 import PipeType
from anton.events_pb2 import GenericEvent, DeviceOnlineEvent, DeviceOfflineEvent
from anton.capabilities_pb2 import Capabilities, NotificationCapabilities
from anton.capabilities_pb2 import DeviceRegistrationCapabilities


class Config(MutableMapping):
    def __init__(self, path):
        self.path = path
        self.reload_config()

    def reload_config(self):
        try:
            with open(self.path) as f:
                self.config = json.load(f)
        except IOError:
            self.config = {}
            self.write_config()

    def write_config(self):
        with open(self.path, 'w') as f:
            json.dump(self.config, f)

    def __getitem__(self, key):
        return self.config[key]

    def __setitem__(self, key, value):
        self.config[key] = value
        self.write_config()

    def __delitem__(self, key):
        del self.config[key]
        self.write_config()

    def __iter__(self):
        return iter(self.config)

    def __len__(self):
        return len(self.config)


class HueDevicesController:
    def __init__(self, conn, send_event):
        self.conn = conn
        self.send_event = send_event
        self.lights_manager = LightsManager(conn)

    def start(self):
        for light in self.lights_manager.get_all_lights():

            device_online = DeviceOnlineEvent(
                    friendly_name=light.name, capabilities=capabilities)
            event = GenericEvent(device_id=light.unique_id,
                                 device_online=device_online)

    def on_power_state(self, instruction):
        pass


class RegistrationController:
    APP = HueApp("PyHueLights", "Anton")

    def __init__(self, conn, send_event, config, success_callback):
        self.conn = conn
        self.config = config
        self.send_event = send_event
        self.success_callback = success_callback
        self.watcher = None
        self.device_id = "HueRegistration-" + str(id(self))

        # Send online event for the Bridge first, to enable registration.
        device_reg_capabilities = DeviceRegistrationCapabilities(
                greeting_text="Hue Bridge discovered. Register?",
                icon_url="")
        capabilities = Capabilities(
                device_registration_capabilities=device_reg_capabilities)
        online_event = DeviceOnlineEvent(friendly_name="Hue Bridge",
                                         capabilities=capabilities)
        event = GenericEvent(device_id=self.device_id,
                             device_online=online_event)
        self.send_event(event)


    def on_instruction(self, instruction):
        if (instruction.device_registration_instruction.execute_step == 1 and
            self.watcher is None):
            self.watcher = RegistrationWatcher(self.conn.host,
                                               "PyHueLights#Anton",
                                               30.0, self.registration_callback)
            self.watcher.start()

            self.send_registration_event(
                    "Please press the button on the hub",
                    DeviceRegistrationEvent.INFO)

    def registration_callback(self):
        if self.watcher.status == REGISTRATION_FAILED:
            self.send_registration_event(
                    "Registration Failed.", DeviceRegistrationEvent.FAILURE)
        elif self.watcher.status == REGISTRATION_SUCEEDED:
            self.send_registration_event(
                    "Registration successful!", DeviceRegistrationEvent.SUCCESS)
            offline_event = GenericEvent(device_id=self.device_id,
                                         device_offline=DeviceOfflineEvent())

            conn = AuthenticatedHueConnection(self.conn.host,
                                              self.watcher.username)
            self.success_callback(conn)

        self.watcher = None

    def send_registration_event(self, msg, event_type):
        device_registration = DeviceRegistrationEvent(event_type=event_type,
                                                      event_message=msg)
        event = GenericEvent(device_registration=device_registration,
                             device_id=self.device_id)
        self.send_event(event)


class HuePlugin(AntonPlugin):
    APIS = {}

    def setup(self, plugin_startup_info):
        instruction_controller = GenericInstructionController(self.APIS)
        event_controller = GenericEventController(lambda call_status: 0)
        event_controller.create_client(0, self.on_response)
        self.send_event = event_controller.create_client(0, self.on_response)
        self.config = Config(Path(plugin_startup_info.data_dir) / "config.json")

        registry = self.channel_registrar()
        registry.register_controller(PipeType.IOT_INSTRUCTION,
                                     instruction_controller)
        registry.register_controller(PipeType.IOT_EVENTS, event_controller)

    def on_start(self):
        self.discovery_thread = Thread(target=self.discover_bridges)
        self.discovery_thread.start()

    def on_stop(self):
        pass

    def on_response(self, call_status):
        print("Received response:", call_status)

    def discover_bridges(self):
        discovery = DefaultDiscovery()
        while True:
            try:
                log_info("Discovering Hue Bridges..")
                conn = discovery.discover()
                break
            except DiscoveryFailed:
                time.sleep(30)
                continue

        controller = RegistrationController(conn, self.send_event, self.config,
                                            self.on_successful_registration)
        self.APIS["device_registration_instruction"] = controller.on_instruction

    def on_successful_registration(self, conn):
        del self.APIS["device_registration_instruction"]



