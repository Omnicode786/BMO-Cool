"""GPIO Zero controls for touch, two buttons, and a five-way joystick plus keyboard dev input."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

from ..config import ControlsConfig, DigitalInputHardware, HardwareConfig, JoystickHardware
from ..event_bus import EventBus
from ..events import ControlAction, ControlEvent, PushToTalkEnded, PushToTalkStarted

LOG = logging.getLogger(__name__)


class GPIOControlsService:
    """Own all GPIO input access so model/higher layers never manipulate pins directly."""

    def __init__(self, bus: EventBus, hardware: HardwareConfig, controls: ControlsConfig) -> None:
        self.bus = bus
        self.hardware = hardware
        self.controls = controls
        self._loop: asyncio.AbstractEventLoop | None = None
        self._devices: list[Any] = []
        self._touch_press_at = 0.0
        self._pending_tap: asyncio.Task[None] | None = None

    async def start(self) -> None:
        try:
            from gpiozero import Button
        except ImportError as exc:
            raise RuntimeError("gpiozero is not installed") from exc
        self._loop = asyncio.get_running_loop()
        if self.hardware.touch.enabled:
            touch = self._make_button(Button, self.hardware.touch)
            touch.when_pressed = self._on_touch_press
            touch.when_released = self._on_touch_release
            self._devices.append(touch)
        for cfg, action in (
            (self.hardware.button_a, ControlAction.BUTTON_A),
            (self.hardware.button_b, ControlAction.BUTTON_B),
        ):
            if cfg.enabled:
                button = self._make_button(Button, cfg)
                button.when_pressed = lambda action=action: self._thread_publish(ControlEvent(action=action))
                self._devices.append(button)
        if self.hardware.joystick.enabled:
            joy = self.hardware.joystick
            for pin, action in (
                (joy.up_pin, ControlAction.JOY_UP),
                (joy.down_pin, ControlAction.JOY_DOWN),
                (joy.left_pin, ControlAction.JOY_LEFT),
                (joy.right_pin, ControlAction.JOY_RIGHT),
                (joy.press_pin, ControlAction.JOY_PRESS),
            ):
                button = self._make_joystick_button(Button, joy, pin)
                button.when_pressed = lambda action=action: self._thread_publish(ControlEvent(action=action))
                self._devices.append(button)
        LOG.info("GPIO controls initialized (%s inputs)", len(self._devices))

    async def stop(self) -> None:
        if self._pending_tap:
            self._pending_tap.cancel()
        for device in self._devices:
            try:
                device.close()
            except Exception:
                LOG.debug("GPIO device close failed", exc_info=True)
        self._devices.clear()

    def _make_button(self, button_cls: Any, cfg: DigitalInputHardware) -> Any:
        bounce = self.controls.timing.debounce_ms / 1000.0
        if cfg.pull_up is not None:
            return button_cls(cfg.gpio_pin, pull_up=cfg.pull_up, bounce_time=bounce)
        if cfg.active_high:
            return button_cls(cfg.gpio_pin, pull_up=False, active_state=True, bounce_time=bounce)
        return button_cls(cfg.gpio_pin, pull_up=True, bounce_time=bounce)

    def _make_joystick_button(self, button_cls: Any, cfg: JoystickHardware, pin: int) -> Any:
        bounce = self.controls.timing.debounce_ms / 1000.0
        if cfg.active_high:
            return button_cls(pin, pull_up=False, active_state=True, bounce_time=bounce)
        return button_cls(pin, pull_up=True, bounce_time=bounce)

    def _thread_publish(self, event: Any) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self.bus.publish(event)))

    def _on_touch_press(self) -> None:
        self._touch_press_at = time.monotonic()
        self._thread_publish(PushToTalkStarted(source="touch"))

    def _on_touch_release(self) -> None:
        now = time.monotonic()
        duration_ms = int((now - self._touch_press_at) * 1000)
        self._thread_publish(PushToTalkEnded(source="touch"))
        if duration_ms >= self.controls.timing.long_press_ms:
            if self._pending_tap:
                self._pending_tap.cancel()
                self._pending_tap = None
            self._thread_publish(ControlEvent(action=ControlAction.TOUCH_LONG, duration_ms=duration_ms))
            return
        if self._loop is None:
            return
        if self._pending_tap and not self._pending_tap.done():
            self._pending_tap.cancel()
            self._pending_tap = None
            self._thread_publish(ControlEvent(action=ControlAction.TOUCH_DOUBLE, duration_ms=duration_ms))
        else:
            self._loop.call_soon_threadsafe(self._schedule_single_tap, duration_ms)

    def _schedule_single_tap(self, duration_ms: int) -> None:
        async def delayed() -> None:
            await asyncio.sleep(self.controls.timing.double_tap_ms / 1000.0)
            await self.bus.publish(ControlEvent(action=ControlAction.TOUCH_TAP, duration_ms=duration_ms))

        self._pending_tap = asyncio.create_task(delayed(), name="touch-single-tap")


class KeyboardControlsService:
    """Line-oriented keyboard controls for hardware-free development."""

    KEYMAP = {
        "a": ControlAction.BUTTON_A,
        "b": ControlAction.BUTTON_B,
        "w": ControlAction.JOY_UP,
        "s": ControlAction.JOY_DOWN,
        "j": ControlAction.JOY_LEFT,
        "l": ControlAction.JOY_RIGHT,
        "p": ControlAction.JOY_PRESS,
        "t": ControlAction.TOUCH_TAP,
        "d": ControlAction.TOUCH_DOUBLE,
        "h": ControlAction.TOUCH_LONG,
    }

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        LOG.info("keyboard controls: a/b, w/s/j/l/p joystick, t tap, d double, h long; press Enter")
        self._task = asyncio.create_task(self._run(), name="keyboard-controls")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                await asyncio.sleep(0.2)
                continue
            for char in line.strip().lower():
                action = self.KEYMAP.get(char)
                if action:
                    await self.bus.publish(ControlEvent(action=action))
