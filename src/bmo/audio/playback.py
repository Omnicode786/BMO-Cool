"""Low-latency PCM playback that rejects stale turn audio after interruption."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Any

from ..config import AudioHardware, TTSConfig
from ..event_bus import EventBus
from ..events import BargeIn, PeripheralFailed, PlaybackStarted, PlaybackStopped, TTSAudioChunk, TTSFinished
from ..latency import LatencyTracker
from ..state import TurnManager
from .preprocessing import scale_pcm16

LOG = logging.getLogger(__name__)


class AudioPlaybackService:
    """Slice TTS PCM into short writes so barge-in stops audible output quickly."""

    def __init__(
        self,
        bus: EventBus,
        hardware: AudioHardware,
        config: TTSConfig,
        turns: TurnManager,
        latency: LatencyTracker,
        *,
        fallback_duck_gain: float = 1.0,
        aec_device_hint: str | None = None,
    ) -> None:
        self.bus = bus
        self.hardware = hardware
        self.config = config
        self.turns = turns
        self.latency = latency
        self.fallback_duck_gain = fallback_duck_gain
        self.aec_device_hint = aec_device_hint
        self._resolved_device: str | int | None = hardware.device
        self._queue: asyncio.Queue[TTSAudioChunk] = asyncio.Queue(maxsize=config.queue_max_chunks)
        self._stream: Any = None
        self._sd: Any = None
        self._tasks: list[asyncio.Task[None]] = []
        self._first_audio_turns: set[int] = set()
        self._finished_turns: set[int] = set()
        self._playing_turn: int | None = None
        self._finish_lock = asyncio.Lock()

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("sounddevice is required for speaker playback; run scripts/install.sh") from exc
        self._sd = sd
        try:
            await self._ensure_stream()
        except Exception as exc:
            LOG.warning("speaker unavailable at startup; will retry on audio: %s", exc)
            await self.bus.publish(PeripheralFailed(peripheral="speaker", detail=str(exc)))
        self._tasks = [
            asyncio.create_task(self._ingest(), name="playback-ingest"),
            asyncio.create_task(self._writer(), name="playback-writer"),
            asyncio.create_task(self._finish_loop(), name="playback-finish"),
            asyncio.create_task(self._barge_loop(), name="playback-barge"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.clear(interrupted=False)
        await self._close_stream()

    async def _ingest(self) -> None:
        sub = await self.bus.subscribe(TTSAudioChunk, maxsize=64, name="playback-ingest")
        async with sub:
            while True:
                event = await sub.get()
                if not self.turns.is_current(event.turn_id):
                    continue
                for piece in self._split(event):
                    if self._queue.full():
                        # Playback audio is not disposable like camera frames. If the queue
                        # fills, back-pressure TTS reception briefly rather than losing words.
                        await self._queue.put(piece)
                    else:
                        self._queue.put_nowait(piece)

    def _split(self, event: TTSAudioChunk) -> list[TTSAudioChunk]:
        bytes_per_sample = 2 * self.hardware.channels
        frames = max(1, event.sample_rate * self.config.playback_slice_ms // 1000)
        size = frames * bytes_per_sample
        return [
            TTSAudioChunk(
                turn_id=event.turn_id,
                event_id=event.event_id,
                timestamp=event.timestamp,
                pcm=event.pcm[offset : offset + size],
                sample_rate=event.sample_rate,
                context_id=event.context_id,
            )
            for offset in range(0, len(event.pcm), size)
            if event.pcm[offset : offset + size]
        ]

    async def _writer(self) -> None:
        while True:
            event = await self._queue.get()
            if not self.turns.is_current(event.turn_id):
                continue
            turn_id = int(event.turn_id)
            if self._playing_turn != turn_id:
                self._playing_turn = turn_id
                await self.bus.publish(PlaybackStarted(turn_id=turn_id, sample_rate=event.sample_rate))
            pcm = scale_pcm16(event.pcm, self.fallback_duck_gain)
            try:
                await self._ensure_stream()
                await asyncio.to_thread(self._stream.write, pcm)
            except Exception as exc:
                LOG.warning("speaker write failed; dropping current slice and retrying later: %s", exc)
                await self.bus.publish(PeripheralFailed(peripheral="speaker", detail=str(exc)))
                await self.bus.publish(PlaybackStopped(turn_id=turn_id, interrupted=False))
                await self._close_stream()
                await asyncio.sleep(0.2)
                continue
            if turn_id not in self._first_audio_turns:
                self._first_audio_turns.add(turn_id)
                self.latency.mark(turn_id, "speaker_first_audio")
            await self._maybe_finish(turn_id)

    async def _finish_loop(self) -> None:
        sub = await self.bus.subscribe(TTSFinished, maxsize=16, name="playback-finish")
        async with sub:
            while True:
                event = await sub.get()
                if event.turn_id is None:
                    continue
                self._finished_turns.add(int(event.turn_id))
                await self._maybe_finish(int(event.turn_id))

    async def _maybe_finish(self, turn_id: int) -> None:
        async with self._finish_lock:
            if turn_id not in self._finished_turns or self._playing_turn != turn_id:
                return
            # A single current turn owns playback. TTSFinished is received after all
            # audio messages for its context, so an empty writer queue means audible
            # playback has reached the normal turn boundary.
            if not self._queue.empty():
                return
            self._finished_turns.discard(turn_id)
            self._playing_turn = None
            await self.bus.publish(PlaybackStopped(turn_id=turn_id, interrupted=False))

    async def _ensure_stream(self) -> None:
        if self._stream is not None and getattr(self._stream, "active", False):
            return
        await self._close_stream()
        if self.hardware.device is None and self.aec_device_hint:
            self._resolved_device = self._find_device(self.aec_device_hint)
        else:
            self._resolved_device = self.hardware.device
        self._stream = self._sd.RawOutputStream(
            samplerate=self.hardware.sample_rate,
            blocksize=0,
            device=self._resolved_device,
            channels=self.hardware.channels,
            dtype="int16",
            latency="low",
        )
        self._stream.start()


    def _find_device(self, hint: str) -> int | None:
        def normalized(value: object) -> str:
            return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()

        wanted = normalized(hint)
        try:
            devices = self._sd.query_devices()
        except Exception:
            return None
        for index, device in enumerate(devices):
            name = normalized(device.get("name", ""))
            if wanted in name and int(device.get("max_output_channels", 0)) > 0:
                LOG.info("selected PipeWire AEC output device index=%d name=%s", index, device.get("name", ""))
                return index
        LOG.warning("PipeWire AEC sink hint %r not found; using system default output", hint)
        return None

    async def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.stop)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.close)

    async def _barge_loop(self) -> None:
        sub = await self.bus.subscribe(BargeIn, maxsize=8, name="playback-barge")
        async with sub:
            while True:
                event = await sub.get()
                await self.clear(turn_id=event.interrupted_turn_id, interrupted=True)

    async def clear(self, turn_id: int | None = None, *, interrupted: bool = True) -> None:
        while True:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                continue
            break
        if self._stream is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._stream.abort)
                await asyncio.to_thread(self._stream.start)
        old = self._playing_turn if turn_id is None else turn_id
        if old is not None:
            self._finished_turns.discard(old)
            await self.bus.publish(PlaybackStopped(turn_id=old, interrupted=interrupted))
        self._playing_turn = None


class MemoryPlaybackService:
    """Development playback sink that preserves PCM chunks for assertions."""

    def __init__(self, bus: EventBus, turns: TurnManager) -> None:
        self.bus = bus
        self.turns = turns
        self.played: list[tuple[int, bytes]] = []
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="memory-playback")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        sub = await self.bus.subscribe(TTSAudioChunk, name="memory-playback")
        async with sub:
            while True:
                event = await sub.get()
                if self.turns.is_current(event.turn_id):
                    self.played.append((int(event.turn_id), event.pcm))
