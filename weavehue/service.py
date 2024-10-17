import time
import json
from pathlib import Path

from pyantonlib.plugin import AntonPlugin
from pyantonlib.channel import AppHandlerBase, DeviceHandlerBase
from pyantonlib.channel import DefaultProtoChannel
from pyantonlib.utils import log_info
from anton.call_status_pb2 import CallStatus
from anton.plugin_pb2 import PipeType
from anton.ui_pb2 import CustomMessage, DynamicAppRequestType
from anton.plugin_messages_pb2 import GenericPluginToPlatformMessage

from weavehue.registration import HueRegistrationController
from weavehue.settings import Settings
from weavehue.devices_controller import HueDevicesController


class Channel(DefaultProtoChannel):
    pass


class AppHandler(AppHandlerBase):

    def __init__(self, plugin_startup_info):
        super().__init__(plugin_startup_info, incoming_message_key='action')

    def get_ui_path(self, app_type):
        if app_type == DynamicAppRequestType.SETTINGS:
            return "ui/settings_ui.pbtxt"


class RegistrationStateHelper:

    def __init__(self, app_handler, on_connected):
        self.app_handler = app_handler
        self.on_connected = on_connected
        self.state = {}

    def get_listener(self):

        def listener(obj):
            if obj["status"] == "connected":
                conn = obj.pop("conn")
                self.on_connected(conn)

            self.state.update(obj)
            self.app_handler.send_message(
                {
                    "type": "bridge",
                    "bridge": self.state
                }, requester_id=None)

        return listener

    def get_state(self):
        return {"bridge": self.state}


class SettingsStateHelper:

    def __init__(self, app_handler, data_dir):
        self.app_handler = app_handler
        self.settings = Settings(data_dir, self.get_listener())
        self.state = {}

    def transform_state(self, obj):
        return {k: v for k, v in obj.items() if k != "username"}

    def get_listener(self):

        def listener(obj):
            self.app_handler.send_message(
                {
                    "type": "settings",
                    "settings": self.transform_state(obj)
                },
                requester_id=None)

        return listener

    def get_state(self):
        return {"settings": self.transform_state(self.settings.props)}


def send_states(send_fn, *args):
    for obj in args:
        res = obj.get_state()
        res["type"] = list(res.keys())[0]
        send_fn(res)


class HuePlugin(AntonPlugin):

    def setup(self, plugin_startup_info):
        self.app_handler = AppHandler(plugin_startup_info)
        self.registration_state_helper = RegistrationStateHelper(
            self.app_handler, self.on_hue_connect)

        self.settings_state_helper = SettingsStateHelper(
            self.app_handler, plugin_startup_info.data_dir)
        self.settings = self.settings_state_helper.settings

        self.device_handler = DeviceHandlerBase()

        self.channel = Channel(self.device_handler, self.app_handler)

        registry = self.channel_registrar()
        registry.register_controller(PipeType.DEFAULT, self.channel)

        self.registration_controller = HueRegistrationController(
            self, self.settings, self.registration_state_helper.get_listener())

        # All actions from DynamicApp.
        self.app_handler.register_action(
            'get_plugin_state', lambda requester_id, _: send_states(
                lambda x: self.app_handler.send_message(
                    x, requester_id=requester_id), self.
                registration_state_helper, self.settings_state_helper))
        self.app_handler.register_action(
            'discover',
            lambda _, __: self.registration_controller.start_discovery())
        self.app_handler.register_action(
            'register',
            lambda _, __: self.registration_controller.register_bridge())

    def on_start(self):
        if self.settings.get_prop('username'):
            self.registration_controller.start_discovery()
        else:
            print("Not starting discovery: Not configured.")

    def on_stop(self):
        pass

    def on_hue_connect(self, conn):
        self.device_handler = HueDevicesController(conn)
        self.channel.set_device_handler(self.device_handler)
        self.device_handler.start()
