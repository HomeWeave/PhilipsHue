import asyncio

from pyhuelights.core import RGB, Temperature, HueSat, LightsManager
from pyhuelights.animations import SwitchOnEffect, SwitchOffEffect
from pyhuelights.animations import SetLightStateEffect

from pyantonlib.channel import DeviceHandlerBase
from pyantonlib.exceptions import ResourceNotFound, AntonInternalError
from pyantonlib.utils import log_warn
from anton.call_status_pb2 import CallStatus, Status
from anton.state_pb2 import DeviceState
from anton.capabilities_pb2 import Capabilities
from anton.power_pb2 import PowerState
from anton.color_pb2 import COLOR_MODEL_RGB, COLOR_MODEL_HUE_SAT
from anton.color_pb2 import COLOR_MODEL_TEMPERATURE
from anton.color_pb2 import Color as AntonColor
from anton.device_pb2 import DEVICE_KIND_LIGHTS, DEVICE_STATUS_ONLINE
from anton.device_pb2 import DEVICE_STATUS_OFFLINE


def to_device_status(light):
    return DEVICE_STATUS_ONLINE if light.reachable else DEVICE_STATUS_OFFLINE


def to_device_power_state(light):
    return PowerState.POWER_STATE_ON if light.on else PowerState.POWER_STATE_OFF


def to_device_brightness(light):
    return int((light.brightness or 0) / 255.0 * 100)


def to_device_color_state(light):
    res = AntonColor()
    color = light.color
    if isinstance(color, Temperature):
        res.temperature.kelvin = color.value
    elif isinstance(color, HueSat):
        res.hs.hue = color.hue
        res.hs.sat = color.saturation
    elif isinstance(color, RGB):
        res.rgb.red = color.r
        res.rgb.green = color.g
        res.rgb.blue = color.b

    return res


async def handle_power_state_request(device_handler, lm, device, power_state):
    if power_state == PowerState.POWER_STATE_OFF:
        await lm.run_effect(device, SwitchOffEffect())
    elif power_state == PowerState.POWER_STATE_ON:
        await lm.run_effect(device, SwitchOnEffect())


async def handle_color_state_request(device_handler, lm, device, color):
    if not color:
        return

    if color.WhichOneof('ColorMode') == 'rgb':
        lib_color = RGB(color.rgb.red, color.rgb.green, color.rgb.blue)
    elif color.WhichOneof('ColorMode') == 'temperature':
        lib_color = Temperature(color.temperature.kelvin)
    elif color.WhichOneof('ColorMode') == 'hs':
        lib_color = HueSat(color.hs.hue, color.hs.sat)
    else:
        return

    await lm.run_effect(device, SetLightStateEffect(on=True, color=lib_color))


async def handle_brightness_request(device_handler, lm, device, brightness):
    if brightness < 0 or brightness > 100:
        log_warn("Brightness needs to be in the range [0, 100]")
        return

    value = int(brightness / 100.0 * 255.0)
    await lm.run_effect(device,
                        SetLightStateEffect(on=(value > 0), brightness=value))


class HueDevicesController(DeviceHandlerBase):

    def __init__(self, conn, loop):
        super().__init__()
        self.lights_manager = LightsManager(conn)
        self.devices = {}
        self.watcher_task = None
        self.loop = loop

    def start(self):

        def _create_task():
            self.watcher_task = self.loop.create_task(self._start_async())

        self.loop.call_soon_threadsafe(_create_task)

    def stop(self):
        if self.watcher_task:
            self.loop.call_soon_threadsafe(self.watcher_task.cancel)

    async def _start_async(self):
        lights = await self.lights_manager.get_all_lights()
        for _, light in lights.items():
            self.devices[light.id] = light

            state = DeviceState(device_id=light.id,
                                friendly_name=light.metadata.name,
                                kind=DEVICE_KIND_LIGHTS)

            self.populate_capabilities(light, state)
            self.populate_device_state(light, state)

            self.send_device_state_updated(state)

        await self.watch()

    async def watch(self):
        async for device in self.lights_manager.iter_events():
            self.devices[device.id] = device

            state = DeviceState(device_id=device.id)
            self.populate_device_state(device, state)
            self.send_device_state_updated(state)

    def handle_set_device_state(self, msg, responder):
        log_warn("Handling set_device_state: " + str(msg))

        device = self.devices.get(msg.device_id)
        if device is None:
            raise ResourceNotFound(msg.device_id)

        self.loop.call_soon_threadsafe(lambda: self.loop.create_task(
            self._handle_set_device_state_async(msg, responder, device)))

    async def _handle_set_device_state_async(self, msg, responder, device):
        try:
            if msg.power_state != PowerState.POWER_STATE_UNKNOWN:
                await handle_power_state_request(self, self.lights_manager,
                                                 device, msg.power_state)

            if msg.HasField('color_state'):
                await handle_color_state_request(self, self.lights_manager,
                                                 device, msg.color_state)

            if msg.brightness > 0:
                await handle_brightness_request(self, self.lights_manager,
                                                device, msg.brightness)

            responder(CallStatus(code=Status.STATUS_OK))
        except Exception as e:
            log_warn(f"Error handling device state: {e}")
            responder(
                CallStatus(code=Status.STATUS_INTERNAL_ERROR, message=str(e)))

    def populate_capabilities(self, device, state):
        capabilities = state.capabilities
        capabilities.power_state.supported_power_states[:] = [
            PowerState.POWER_STATE_OFF, PowerState.POWER_STATE_ON
        ]
        capabilities.color.supports_brightness = True

        models = device.capabilities.supported_color_models
        if RGB in models:
            capabilities.color.supported_color_models.append(COLOR_MODEL_RGB)
        if HueSat in models:
            capabilities.color.supported_color_models.append(
                COLOR_MODEL_HUE_SAT)
        if Temperature in models:
            capabilities.color.supported_color_models.append(
                COLOR_MODEL_TEMPERATURE)

        return capabilities

    def populate_device_state(self, light, state):
        state.device_status = to_device_status(light)
        state.power_state = to_device_power_state(light)
        state.color_state.CopyFrom(to_device_color_state(light))
        state.brightness = to_device_brightness(light)


class UnregisteredHueController(DeviceHandlerBase):

    def stop(self):
        pass
