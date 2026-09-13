"""Periodic Raspberry Pi health/performance metrics and internet reachability monitoring."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import psutil

from .config import HealthConfig
from .event_bus import EventBus
from .events import HealthSnapshot, NetworkLost, NetworkRestored

LOG = logging.getLogger(__name__)


class HealthService:
    """Measure system load without letting diagnostics interfere with realtime audio."""

    def __init__(
        self,
        bus: EventBus,
        config: HealthConfig,
        *,
        camera_fps: Callable[[], float] | None = None,
        vision_latency_ms: Callable[[], float] | None = None,
        audio_queue_depth: Callable[[], int] | None = None,
        tts_queue_depth: Callable[[], int] | None = None,
    ) -> None:
        self.bus = bus
        self.config = config
        self.camera_fps = camera_fps or (lambda: 0.0)
        self.vision_latency_ms = vision_latency_ms or (lambda: 0.0)
        self.audio_queue_depth = audio_queue_depth or (lambda: 0)
        self.tts_queue_depth = tts_queue_depth or (lambda: 0)
        self._task: asyncio.Task[None] | None = None
        self._internet_failures = 0
        self._internet_down = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="health-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        psutil.cpu_percent(interval=None)
        while True:
            await asyncio.sleep(self.config.interval_sec)
            reachable = await self._internet_ok()
            if reachable:
                self._internet_failures = 0
                if self._internet_down:
                    self._internet_down = False
                    await self.bus.publish(NetworkRestored(service="internet"))
            else:
                self._internet_failures += 1
                if self._internet_failures >= self.config.network_failure_threshold and not self._internet_down:
                    self._internet_down = True
                    await self.bus.publish(NetworkLost(service="internet", detail="connectivity probe failed"))

            snapshot = HealthSnapshot(
                cpu_percent=float(psutil.cpu_percent(interval=None)),
                memory_percent=float(psutil.virtual_memory().percent),
                cpu_temp_c=await asyncio.to_thread(read_cpu_temp_c),
                throttled_hex=await asyncio.to_thread(read_throttled),
                camera_fps=float(self.camera_fps()),
                vision_latency_ms=float(self.vision_latency_ms()),
                audio_queue_depth=int(self.audio_queue_depth()),
                tts_queue_depth=int(self.tts_queue_depth()),
            )
            LOG.info(
                "health cpu=%.1f%% mem=%.1f%% temp=%sC camera=%.1ffps vision=%.1fms audio_q=%d tts_q=%d throttled=%s",
                snapshot.cpu_percent,
                snapshot.memory_percent,
                f"{snapshot.cpu_temp_c:.1f}" if snapshot.cpu_temp_c is not None else "n/a",
                snapshot.camera_fps,
                snapshot.vision_latency_ms,
                snapshot.audio_queue_depth,
                snapshot.tts_queue_depth,
                snapshot.throttled_hex or "n/a",
            )
            await self.bus.publish(snapshot)

    async def _internet_ok(self) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.config.internet_host, self.config.internet_port),
                timeout=3.0,
            )
            del reader
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False


def read_cpu_temp_c() -> float | None:
    for path in (
        Path("/sys/class/thermal/thermal_zone0/temp"),
        Path("/sys/devices/virtual/thermal/thermal_zone0/temp"),
    ):
        try:
            return float(path.read_text(encoding="ascii").strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return None


def read_throttled() -> str | None:
    """Return vcgencmd get_throttled hex flags when Raspberry Pi firmware exposes it."""
    binary = shutil.which("vcgencmd")
    if not binary:
        return None
    try:
        result = subprocess.run(
            [binary, "get_throttled"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if "=" not in result.stdout:
        return None
    return result.stdout.strip().split("=", 1)[1]
