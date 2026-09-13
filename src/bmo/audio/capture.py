"""Continuous 20-40 ms microphone capture with bounded buffering and fixtures."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import wave
from pathlib import Path
from typing import Any

from ..config import AudioConfig, AudioHardware
from ..event_bus import EventBus
from ..events import MicAudioChunk, PeripheralFailed

LOG = logging.getLogger(__name__)


class AudioCaptureService:
    """Capture raw PCM continuously and reopen the device after disconnects."""

    def __init__(self, bus: EventBus, hardware: AudioHardware, config: AudioConfig) -> None:
        self.bus = bus
        self.hardware = hardware
        self.config = config
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=config.queue_max_chunks)
        self._stream: Any = None
        self._sd: Any = None
        self._tasks: list[asyncio.Task[None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._resolved_device: str | int | None = hardware.device

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("sounddevice is required for microphone capture; run scripts/install.sh") from exc
        self._sd = sd
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._tasks = [
            asyncio.create_task(self._pump(), name="microphone-pump"),
            asyncio.create_task(self._device_supervisor(), name="microphone-supervisor"),
        ]

    async def _device_supervisor(self) -> None:
        backoff = 0.5
        while not self._stopping:
            active = bool(self._stream is not None and getattr(self._stream, "active", False))
            if active:
                await asyncio.sleep(0.5)
                continue
            await self._close_stream()
            try:
                self._open_stream()
                backoff = 0.5
                LOG.info("microphone streaming at %d Hz, %d ms chunks", self.hardware.sample_rate, self.config.chunk_ms)
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("microphone unavailable/disconnected: %s", exc)
                await self.bus.publish(PeripheralFailed(peripheral="microphone", detail=str(exc)))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 10.0)

    def _open_stream(self) -> None:
        frames = self.hardware.sample_rate * self.config.chunk_ms // 1000
        if self.hardware.device is None and self.config.aec_mode == "pipewire":
            self._resolved_device = self._find_device(self.config.echo_cancel_source_hint, input_device=True)
        else:
            self._resolved_device = self.hardware.device

        def callback(indata: bytes, _frames: int, _time: Any, status: Any) -> None:
            if status:
                LOG.warning("microphone status: %s", status)
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._offer, bytes(indata))

        self._stream = self._sd.RawInputStream(
            samplerate=self.hardware.sample_rate,
            blocksize=frames,
            device=self._resolved_device,
            channels=self.hardware.channels,
            dtype="int16",
            callback=callback,
        )
        self._stream.start()


    def _find_device(self, hint: str, *, input_device: bool) -> int | None:
        def normalized(value: object) -> str:
            return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()

        wanted = normalized(hint)
        if not wanted:
            return None
        try:
            devices = self._sd.query_devices()
        except Exception:
            return None
        channel_key = "max_input_channels" if input_device else "max_output_channels"
        for index, device in enumerate(devices):
            name = normalized(device.get("name", ""))
            if wanted in name and int(device.get(channel_key, 0)) > 0:
                LOG.info("selected PipeWire AEC input device index=%d name=%s", index, device.get("name", ""))
                return index
        LOG.warning("PipeWire AEC source hint %r not found; using system default input", hint)
        return None

    def _offer(self, pcm: bytes) -> None:
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(pcm)

    async def _pump(self) -> None:
        while True:
            pcm = await self._queue.get()
            await self.bus.publish(
                MicAudioChunk(pcm=pcm, sample_rate=self.hardware.sample_rate, channels=self.hardware.channels)
            )

    async def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.stop)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.close)

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._close_stream()


class WavFixtureCaptureService:
    """Development capture source that streams a WAV fixture in realtime chunks."""

    def __init__(self, bus: EventBus, path: Path, chunk_ms: int = 30, loop_audio: bool = False) -> None:
        self.bus = bus
        self.path = path
        self.chunk_ms = chunk_ms
        self.loop_audio = loop_audio
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="fixture-microphone")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            with wave.open(str(self.path), "rb") as wav:
                if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
                    raise ValueError("fixture WAV must be mono signed 16-bit PCM")
                rate = wav.getframerate()
                frames = rate * self.chunk_ms // 1000
                while chunk := wav.readframes(frames):
                    await self.bus.publish(MicAudioChunk(pcm=chunk, sample_rate=rate, channels=1))
                    await asyncio.sleep(self.chunk_ms / 1000)
            if not self.loop_audio:
                return
