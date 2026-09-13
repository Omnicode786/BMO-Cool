"""Replaceable SSD1306/SH1106 OLED output with a console development fallback."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from ..config import OLEDHardware

LOG = logging.getLogger(__name__)


class OLEDDevice(Protocol):
    width: int
    height: int

    def display(self, image: Any) -> None: ...

    def close(self) -> None: ...


class LumaOLEDDevice:
    """luma.oled-backed I2C display for SSD1306 or SH1106 modules."""

    def __init__(self, config: OLEDHardware) -> None:
        try:
            from luma.core.interface.serial import i2c
            from luma.oled.device import sh1106, ssd1306
        except ImportError as exc:
            raise RuntimeError("luma.oled is not installed; run scripts/install.sh") from exc
        serial = i2c(port=config.i2c_bus, address=config.i2c_address)
        cls = ssd1306 if config.driver == "ssd1306" else sh1106
        self._device = cls(serial, width=config.width, height=config.height)
        self.width = config.width
        self.height = config.height

    def display(self, image: Any) -> None:
        self._device.display(image)

    def close(self) -> None:
        self._device.cleanup()


class ConsoleOLEDDevice:
    """Development OLED emulator that logs the current logical frame label."""

    def __init__(self, config: OLEDHardware) -> None:
        self.width = config.width
        self.height = config.height
        self.last_image: Any = None
        self.last_label = ""

    def display(self, image: Any) -> None:
        self.last_image = image.copy() if hasattr(image, "copy") else image
        label = str(getattr(image, "info", {}).get("label", "frame"))
        if label != self.last_label:
            LOG.info("OLED(console): %s", label)
            self.last_label = label

    def close(self) -> None:
        self.last_image = None


def create_oled_device(config: OLEDHardware) -> OLEDDevice:
    if config.driver == "console":
        return ConsoleOLEDDevice(config)
    return LumaOLEDDevice(config)
