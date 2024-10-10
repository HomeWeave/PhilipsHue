from threading import Thread

from pyhuelights.manager import LightsManager
from pyhuelights.core import Color
from pyhuelights.animations import SwitchOnEffect, SwitchOffEffect
from pyhuelights.animations import SetLightStateEffect

from pyantonlib.channel import DeviceHandlerBase
from pyantonlib.exceptions import ResourceNotFound, AntonInternalError
from pyantonlib.utils import log_warn
from anton.call_status_pb2 import CallStatus, Status
from anton.state_pb2 import DeviceState
from anton.capabilities_pb2 import Capabilities
from anton.power_pb2 import POWER_OFF, POWER_ON, PowerInstruction
from anton.color_pb2 import COLOR_MODEL_RGB, COLOR_MODEL_HUE_SAT
from anton.color_pb2 import COLOR_MODEL_TEMPERATURE
from anton.device_pb2 import DEVICE_KIND_LIGHTS, DEVICE_STATUS_ONLINE
from anton.device_pb2 import DEVICE_STATUS_OFFLINE


def to_device_status(light):
    return DEVICE_STATUS_ONLINE if light.state.reachable else DEVICE_STATUS_OFFLINE


def to_device_power_state(light):
    return POWER_ON if light.state.on else POWER_OFF


def handle_power_instruction(device_handler, lm, device, power_instruction):
    if power_instruction == PowerInstruction.POWER_OFF:
        lm.run_effect(device, SwitchOffEffect())
    elif power_instruction == PowerInstruction.POWER_ON:
        lm.run_effect(device, SwitchOnEffect())

    device_handler.devices[device.id] = lm.get_resource(device)


class HueLightsStateRefresher:

    def __init__(self, conn, on_event):
        self.staging = []
        self.conn = conn

    def push(self, device):
        pass


class HueDevicesController(DeviceHandlerBase):

    def __init__(self, conn):
        super().__init__()
        self.lights_manager = LightsManager(conn)
        self.watcher_thread = Thread(target=self.watch)

        self.devices = {}

    def start(self):
        for _, light in self.lights_manager.get_all_lights().items():
            self.devices[light.unique_id] = light

            # First send an online event.
            state = DeviceState(device_id=light.unique_id,
                                friendly_name=light.name,
                                kind=DEVICE_KIND_LIGHTS)

            self.populate_capabilities(light, state)
            self.populate_device_state(light, state)

            self.send_device_state_updated(state)

        self.watcher_thread.start()

    def watch(self):
        for device in self.lights_manager.iter_events():
            self.devices[device.id] = device

            state = DeviceState(device_id=device.unique_id)
            self.populate_device_state(device, state)
            self.send_device_state_updated(state)

    def handle_instruction(self, msg, responder):
        log_warn("Handling instruction: " + str(msg))

        device = self.devices.get(msg.device_id)
        if device is None:
            raise ResourceNotFound(msg.device_id)

        if msg.power_instruction != PowerInstruction.UNKNOWN:
            handle_power_instruction(self, self.lights_manager, device,
                                     msg.power_instruction)
            responder(CallStatus(code=Status.STATUS_OK))
            return

        raise AntonInternalError("Unknown instruction.")

    def populate_capabilities(self, device, state):
        capabilities = state.capabilities
        capabilities.power_state.supported_power_instructions[:] = [
            POWER_OFF, POWER_ON
        ]

        color_modes = device.capabilities.supported_color_modes()
        if "hs" in color_modes:
            capabilities.color.supported_color_models.append(COLOR_MODEL_RGB)
            capabilities.color.supported_color_models.append(
                COLOR_MODEL_HUE_SAT)
        if "ct" in color_modes:
            capabilities.color.supported_color_models.append(
                COLOR_MODEL_TEMPERATURE)

        return capabilities

    def populate_device_state(self, light, state):
        state.device_status = to_device_status(light)
        state.power_state = to_device_power_state(light)

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


class UnregisteredHueController(DeviceHandlerBase):

    def stop(self):
        pass
