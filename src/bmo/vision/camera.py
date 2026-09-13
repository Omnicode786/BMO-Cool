"""Picamera2 low-resolution stream plus on-demand high-quality snapshot capture."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from ..config import CameraHardware, PerformanceProfile
from ..event_bus import EventBus
from ..events import CameraFrame, PeripheralFailed
from .frame_buffer import FrameBuffer, FrameRecord

LOG = logging.getLogger(__name__)


class CameraService:
    """Capture low-resolution RGB frames without accumulating a backlog."""

    def __init__(
        self,
        bus: EventBus,
        hardware: CameraHardware,
        profile: PerformanceProfile,
        frame_buffer: FrameBuffer,
    ) -> None:
        self.bus = bus
        self.hardware = hardware
        self.profile = profile
        self.frame_buffer = frame_buffer
        self.available = False
        self._camera: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._capture_lock = asyncio.Lock()
        self._frames = 0
        self._fps_window_start = time.monotonic()
        self._measured_fps = 0.0

    @property
    def measured_fps(self) -> float:
        return self._measured_fps

    async def start(self) -> None:
        if not self.hardware.enabled:
            LOG.info("camera disabled in hardware config")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="camera-capture")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._close_camera()

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._open_camera()
                backoff = 1.0
                period = 1.0 / max(1, self.profile.camera_fps)
                while not self._stop.is_set():
                    started = time.monotonic()
                    async with self._capture_lock:
                        frame = await asyncio.to_thread(self._camera.capture_array, "main")
                    # Picamera2 RGB888 can be padded/ordered by platform; normalize shape only.
                    record = self.frame_buffer.add(frame)
                    await self.bus.publish(
                        CameraFrame(
                            frame_id=record.frame_id,
                            frame=record.frame,
                            width=int(record.frame.shape[1]),
                            height=int(record.frame.shape[0]),
                            sharpness=record.sharpness,
                            brightness=record.brightness,
                        )
                    )
                    self._tick_fps()
                    await asyncio.sleep(max(0.0, period - (time.monotonic() - started)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.available = False
                LOG.warning("camera unavailable/disconnected: %s", exc)
                await self.bus.publish(PeripheralFailed(peripheral="camera", detail=str(exc)))
                await self._close_camera()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 15.0)

    async def _open_camera(self) -> None:
        if self._camera is not None:
            return
        try:
            from picamera2 import Picamera2
        except ImportError as exc:
            raise RuntimeError("Picamera2 is unavailable; install Raspberry Pi OS python3-picamera2") from exc
        camera = Picamera2(self.hardware.device)
        size = tuple(self.profile.camera_resolution)
        config = camera.create_video_configuration(
            main={"size": size, "format": "RGB888"},
            controls={"FrameRate": float(self.profile.camera_fps)},
            buffer_count=3,
        )
        camera.configure(config)
        camera.start()
        await asyncio.sleep(0.35)
        self._camera = camera
        self.available = True
        LOG.info("camera started at %sx%s @ %s FPS", size[0], size[1], self.profile.camera_fps)

    async def _close_camera(self) -> None:
        camera, self._camera = self._camera, None
        if camera is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.to_thread(camera.stop)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(camera.close)
        self.available = False

    async def capture_snapshot(self, reason: str) -> FrameRecord:
        """Capture one high-quality still and place it only in the bounded RAM ring."""
        del reason  # retained for auditability at the call site; it is never sent to hardware.
        if not self.available or self._camera is None:
            raise RuntimeError("camera unavailable")
        async with self._capture_lock:
            try:
                still = self._camera.create_still_configuration(
                    main={"size": tuple(self.hardware.snapshot_resolution), "format": "RGB888"}
                )
                frame = await asyncio.to_thread(self._camera.switch_mode_and_capture_array, still, "main")
                # switch_mode_and_capture_array returns the camera to its previous configuration.
            except Exception:
                # A current low-resolution frame is preferable to failing an explicit user request.
                latest = self.frame_buffer.latest()
                if latest is None:
                    raise
                return latest
        return self.frame_buffer.add(frame)

    def _tick_fps(self) -> None:
        self._frames += 1
        now = time.monotonic()
        elapsed = now - self._fps_window_start
        if elapsed >= 2.0:
            self._measured_fps = self._frames / elapsed
            self._frames = 0
            self._fps_window_start = now


class FixtureCameraService:
    """Development camera that loops image fixtures without Raspberry Pi hardware."""

    def __init__(self, bus: EventBus, frame_buffer: FrameBuffer, fixture_dir: Path, fps: int = 5) -> None:
        self.bus = bus
        self.frame_buffer = frame_buffer
        self.fixture_dir = fixture_dir
        self.fps = fps
        self.available = False
        self.measured_fps = float(fps)
        self._task: asyncio.Task[None] | None = None
        self._paths: list[Path] = []

    async def start(self) -> None:
        self._paths = sorted(
            p for p in self.fixture_dir.glob("*") if p.suffix.lower() in {".png", ".jpg", ".jpeg"}
        )
        if not self._paths:
            LOG.warning("no camera fixtures found in %s", self.fixture_dir)
            return
        self.available = True
        self._task = asyncio.create_task(self._run(), name="fixture-camera")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self.available = False

    async def _run(self) -> None:
        index = 0
        while True:
            frame = await asyncio.to_thread(self._load, self._paths[index % len(self._paths)])
            index += 1
            record = self.frame_buffer.add(frame)
            await self.bus.publish(
                CameraFrame(
                    frame_id=record.frame_id,
                    frame=record.frame,
                    width=int(record.frame.shape[1]),
                    height=int(record.frame.shape[0]),
                    sharpness=record.sharpness,
                    brightness=record.brightness,
                )
            )
            await asyncio.sleep(1.0 / max(1, self.fps))

    async def capture_snapshot(self, reason: str) -> FrameRecord:
        del reason
        latest = self.frame_buffer.latest()
        if latest is None:
            frame = await asyncio.to_thread(self._load, self._paths[0])
            return self.frame_buffer.add(frame)
        return latest

    @staticmethod
    def _load(path: Path) -> Any:
        import numpy as np
        from PIL import Image

        return np.asarray(Image.open(path).convert("RGB"))
