from __future__ import annotations

from unittest.mock import Mock

from bmo.config import ControlsConfig, DigitalInputHardware, HardwareConfig, JoystickHardware
from bmo.hardware.touch import GPIOControlsService


def _service() -> GPIOControlsService:
    return GPIOControlsService(Mock(), HardwareConfig(), ControlsConfig())


def test_active_high_touch_uses_pull_down_without_explicit_active_state() -> None:
    button_cls = Mock(return_value=object())
    cfg = DigitalInputHardware(gpio_pin=17, active_high=True, pull_up=None)

    _service()._make_button(button_cls, cfg)

    button_cls.assert_called_once_with(17, pull_up=False, bounce_time=0.035)


def test_explicit_pull_configuration_is_preserved() -> None:
    button_cls = Mock(return_value=object())
    cfg = DigitalInputHardware(gpio_pin=22, active_high=False, pull_up=True)

    _service()._make_button(button_cls, cfg)

    button_cls.assert_called_once_with(22, pull_up=True, bounce_time=0.035)


def test_active_high_joystick_uses_pull_down_without_explicit_active_state() -> None:
    button_cls = Mock(return_value=object())
    cfg = JoystickHardware(
        up_pin=5,
        down_pin=6,
        left_pin=13,
        right_pin=19,
        press_pin=26,
        active_high=True,
    )

    _service()._make_joystick_button(button_cls, cfg, cfg.up_pin)

    button_cls.assert_called_once_with(5, pull_up=False, bounce_time=0.035)
