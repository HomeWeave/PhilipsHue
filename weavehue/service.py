import time
import json
from pathlib import Path
from threading import Thread

from pyhuelights.manager import LightsManager
from pyhuelights.core import HueApp, Color
from pyhuelights.animations import SwitchOnEffect, SwitchOffEffect
from pyhuelights.animations import SetLightStateEffect

from pyantonlib.plugin import AntonPlugin
from pyantonlib.channel import DeviceHandlerBase, AppHandlerBase
from pyantonlib.channel import DefaultProtoChannel
from pyantonlib.utils import log_info
from anton.call_status_pb2 import CallStatus
from anton.plugin_pb2 import PipeType
from anton.device_pb2 import DEVICE_KIND_LIGHTS, DEVICE_STATUS_ONLINE
from anton.device_pb2 import DEVICE_STATUS_UNREGISTERED, DEVICE_STATUS_OFFLINE
from anton.device_pb2 import DeviceRegistrationEvent
from anton.state_pb2 import DeviceState
from anton.capabilities_pb2 import Capabilities, DeviceRegistrationCapabilities
from anton.power_pb2 import POWER_OFF, POWER_ON
from anton.color_pb2 import COLOR_MODEL_RGB, COLOR_MODEL_HUE_SAT
from anton.color_pb2 import COLOR_MODEL_TEMPERATURE
from anton.ui_pb2 import CustomMessage, DynamicAppRequestType
from anton.plugin_messages_pb2 import GenericPluginToPlatformMessage

from weavehue.registration import HueRegistrationController
from weavehue.settings import Settings


class Channel(DefaultProtoChannel):
    pass


class HueDevicesController(DeviceHandlerBase):

    def __init__(self, conn):
        super().__init__()
        self.lights_manager = LightsManager(conn)

        self.devices = {}

    def start(self):
        for _, light in self.lights_manager.get_all_lights().items():
            self.devices[light.unique_id] = light

            # First send an online event.
            event = GenericEvent(device_id=light.unique_id)
            event.device.friendly_name = light.name
            event.device.device_kind = DEVICE_KIND_LIGHTS
            event.device.device_status = DEVICE_STATUS_ONLINE

            capabilities = event.device.capabilities
            capabilities.power_state.supported_power_states[:] = [
                POWER_OFF, POWER_ON
            ]

            color_modes = light.capabilities.supported_color_modes()
            if "hs" in color_modes:
                capabilities.color.supported_color_models.append(
                    COLOR_MODEL_RGB)
                capabilities.color.supported_color_models.append(
                    COLOR_MODEL_HUE_SAT)
            if "ct" in color_modes:
                capabilities.color.supported_color_models.append(
                    COLOR_MODEL_TEMPERATURE)

            self.send_event(event)

            # Next, send power_state event.
            event = GenericEvent(device_id=light.unique_id)
            event.power_state.power_state = (POWER_ON
                                             if light.state.on else POWER_OFF)
            self.send_event(event)

    def on_power_state(self, instruction):
        light = self.devices.get(instruction.device_id, None)
        if not light:
            return CallStatus(msg="Bad device ID.")

        power_instruction = instruction.power_state

        if power_instruction == POWER_OFF:
            self.lights_manager.run_effect(light, SwitchOffEffect())
        elif power_instruction == POWER_ON:
            self.lights_manager.run_effect(light, SwitchOnEffect())

        event = GenericEvent(device_id=light.unique_id)
        event.power_state.power_state = power_instruction
        self.send_event(event)

    def on_color(self, instruction):
        light = self.devices.get(instruction.device_id, None)
        if not light:
            return CallStatus(msg="Bad device ID.")

        color_space = instruction.color.WhichOneof('ColorMode')

        color = None
        if color_space == 'rgb':
            color = Color.from_rgb(instruction.color.rgb.red,
                                   instruction.color.rgb.green,
                                   instruction.color.rgb.blue)
        elif color_space == 'temperature':
            color = Color.from_temperature(
                instruction.color.temperature.kelvin)
        elif color_space == 'hs':
            color = Color.from_hue_sat(instruction.color.hs.hue,
                                       instruction.color.hs.sat)

        brightness = instruction.color.brightness
        if brightness:
            brightness = int(brightness * 2.55)

        effect = SetLightStateEffect(on=True,
                                     color=color,
                                     brightness=brightness)
        self.lights_manager.run_effect(light, effect)

        event = GenericEvent(device_id=light.unique_id)
        event.color.CopyFrom(instruction.color)
        self.send_event(event)


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
            self.state.update(obj)
            self.app_handler.send_message(
                {
                    "type": "bridge",
                    "bridge": self.state
                }, requester_id=None)
            if obj["status"] == "connected":
                self.on_connected()

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
            self.discovery_thread = Thread(target=self.discover_bridges)
            self.discovery_thread.start()
        else:
            print("Not starting discovery: Not configured.")

    def on_stop(self):
        pass

    def on_response(self, call_status):
        print("Received response:", call_status)

    def on_hue_connect(self):
        self.device_handler = HueDevicesController(
            self.registration_controller.conn)
        self.channel.set_device_handler(self.device_handler)
