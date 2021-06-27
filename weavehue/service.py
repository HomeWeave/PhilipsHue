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
from pyhuelights.core import HueApp, Color
from pyhuelights.animations import SwitchOnEffect, SwitchOffEffect
from pyhuelights.animations import SetLightStateEffect

from pyantonlib.plugin import AntonPlugin
from pyantonlib.channel import GenericInstructionController
from pyantonlib.channel import GenericEventController
from pyantonlib.utils import log_info
from anton.plugin_pb2 import IOT_INSTRUCTION, IOT_EVENTS
from anton.call_status_pb2 import CallStatus
from anton.device_pb2 import DEVICE_KIND_LIGHTS, DEVICE_STATUS_ONLINE
from anton.device_pb2 import DEVICE_STATUS_UNREGISTERED, DEVICE_STATUS_OFFLINE
from anton.device_pb2 import DeviceRegistrationEvent
from anton.events_pb2 import GenericEvent
from anton.capabilities_pb2 import Capabilities, DeviceRegistrationCapabilities
from anton.power_pb2 import POWER_OFF, POWER_ON
from anton.color_pb2 import COLOR_MODEL_RGB, COLOR_MODEL_HUE_SAT
from anton.color_pb2 import COLOR_MODEL_TEMPERATURE


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

        self.devices = {}

    def start(self):
        for _, light in self.lights_manager.get_all_lights().items():
            self.devices[light.unique_id] = light

            event = GenericEvent(device_id=light.unique_id)
            event.device.friendly_name = light.name
            event.device.device_kind = DEVICE_KIND_LIGHTS
            event.device.device_status = DEVICE_STATUS_ONLINE

            capabilities = event.device.capabilities
            capabilities.power_state.supported_power_states[:] = [
                    POWER_OFF, POWER_ON]

            color_modes = light.capabilities.supported_color_modes()
            if "hs" in color_modes:
                capabilities.color.supported_color_models.append(
                        COLOR_MODEL_RGB)
                capabilities.color.supported_color_models.append(
                        COLOR_MODEL_HUE_SAT)
            if "ct" in color_modes:
                capabilities.color.supported_color_models.append(COLOR_MODEL_TEMPERATURE)

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

    def on_color(self, instruction):
        light = self.devices.get(instruction.device_id, None)
        if not light:
            return CallStatus(msg="Bad device ID.")

        color_space = instruction.color.WhichOneof('ColorMode')

        if color_space == 'rgb':
            color = Color.from_rgb(instruction.color_instruction.rgb.red,
                                   instruction.color_instruction.rgb.green,
                                   instruction.color_instruction.rgb.blue)
        elif color_space == 'temperature':
            color = Color.from_temperature(
                    instruction.color_instruction.temperature.kelvin)
        elif color_space == 'hs':
            color = Color.from_hue_sat(instruction.color_instruction.hs.hue,
                                       instruction.color_instruction.hs.sat)

        effect = SetLightStateEffect(on=True, color=color)
        self.lights_manager.run_effect(light, effect)


class RegistrationController:
    APP = HueApp("PyHueLights", "Anton")

    def __init__(self, conn, send_event, config, success_callback):
        self.conn = conn
        self.config = config
        self.send_event = send_event
        self.success_callback = success_callback
        self.watcher = None
        self.device_id = "HueRegistration-" + str(id(self))

        event = GenericEvent(device_id=self.device_id)
        event.device.friendly_name = "Hue Bridge"
        event.device.device_kind = DEVICE_KIND_LIGHTS
        event.device.device_status = DEVICE_STATUS_UNREGISTERED

        capabilities = event.device.capabilities
        # Send online event for the Bridge first, to enable registration.
        capabilities.device_registration_capabilities.greeting_text = (
                "Hue Bridge discovered. Register?")

        self.send_event(event)


    def on_instruction(self, instruction):
        if (instruction.device.device_registration_instruction.execute_step == 1
            and self.watcher is None):
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
        elif self.watcher.status == REGISTRATION_SUCCEEDED:
            self.config["username"] = self.watcher.username
            self.send_registration_event(
                    "Registration successful!", DeviceRegistrationEvent.SUCCESS)
            offline_event = GenericEvent(device_id=self.device_id)
            offline_event.device.device_status = DEVICE_STATUS_OFFLINE

            conn = AuthenticatedHueConnection(self.conn.host,
                                              self.watcher.username)
            self.success_callback(conn)

        self.watcher = None

    def send_registration_event(self, msg, event_type):
        event = GenericEvent(device_id=self.device_id)
        event.device.registration.event_type = event_type
        event.device.registration.event_message = msg
        self.send_event(event)


class HuePlugin(AntonPlugin):
    APIS = {}

    def setup(self, plugin_startup_info):
        self.instruction_controller = GenericInstructionController(self.APIS)
        event_controller = GenericEventController(lambda call_status: 0)
        event_controller.create_client(0, self.on_response)
        self.send_event = event_controller.create_client(0, self.on_response)
        self.config = Config(Path(plugin_startup_info.data_dir) / "config.json")

        registry = self.channel_registrar()
        registry.register_controller(IOT_INSTRUCTION,
                                     self.instruction_controller)
        registry.register_controller(IOT_EVENTS, event_controller)

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

        username = self.config.get("username")
        if username:
            conn = AuthenticatedHueConnection(conn.host, username)
            self.on_successful_registration(conn)
            return

        controller = RegistrationController(conn, self.send_event, self.config,
                                            self.on_successful_registration)
        self.instruction_controller.register_api("device",
                                                 controller.on_instruction)

    def on_successful_registration(self, conn):
        self.instruction_controller.unregister_api("device")

        controller = HueDevicesController(conn, self.send_event)
        self.instruction_controller.register_api(
                "power_state", controller.on_power_state)
        self.instruction_controller.register_api("color", controller.on_color)


        controller.start()

