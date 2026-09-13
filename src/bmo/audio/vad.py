"""Fast local energy VAD used for barge-in and hybrid STT finalization."""

from __future__ import annotations

import asyncio
import time

from ..config import AudioConfig
from ..event_bus import EventBus
from ..events import (
    ListeningModeChanged,
    ProcessedAudioChunk,
    PushToTalkEnded,
    PushToTalkStarted,
    SpeechEnded,
    SpeechStarted,
)
from ..latency import LatencyTracker
from ..state import StateMachine, TurnManager


class EnergyVAD:
    """Small adaptive VAD with explicit start/end frame hysteresis."""

    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        self.speaking = False
        self.noise_floor = max(config.local_vad_threshold * 0.45, 0.003)
        self.active_frames = 0
        self.silent_frames = 0
        self.started_at = 0.0

    def update(self, rms: float, *, assistant_speaking: bool) -> str | None:
        threshold = max(self.config.local_vad_threshold, self.noise_floor * 2.4)
        if assistant_speaking:
            threshold *= self.config.vad_threshold_while_speaking_multiplier

        is_voice = rms >= threshold
        if not self.speaking and not is_voice:
            # Slowly follow ambient noise, but never let a sudden loud frame redefine it.
            clipped = min(rms, self.config.local_vad_threshold * 0.9)
            self.noise_floor = 0.985 * self.noise_floor + 0.015 * clipped

        if is_voice:
            self.active_frames += 1
            self.silent_frames = 0
            if not self.speaking and self.active_frames >= self.config.vad_start_frames:
                self.speaking = True
                self.started_at = time.monotonic()
                return "start"
            return None

        self.active_frames = 0
        if self.speaking:
            self.silent_frames += 1
            if self.silent_frames >= self.config.vad_end_frames:
                self.speaking = False
                self.silent_frames = 0
                return "end"
        return None


class VADService:
    """Publish typed speech boundaries and assign each user utterance a turn id."""

    def __init__(
        self,
        bus: EventBus,
        state: StateMachine,
        turns: TurnManager,
        config: AudioConfig,
        latency: LatencyTracker,
    ) -> None:
        self.bus = bus
        self.state = state
        self.turns = turns
        self.detector = EnergyVAD(config)
        self.latency = latency
        self.config = config
        self._tasks: list[asyncio.Task[None]] = []
        self._active_turn: int | None = None
        self._mode = config.listening_mode
        self._ptt_started_at = 0.0

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._run(), name="local-vad"),
            asyncio.create_task(self._mode_loop(), name="vad-mode"),
            asyncio.create_task(self._ptt_loop(), name="vad-ptt"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run(self) -> None:
        sub = await self.bus.subscribe(ProcessedAudioChunk, maxsize=self.config.queue_max_chunks, name="local-vad")
        async with sub:
            while True:
                event = await sub.get()
                if self._mode == "touch_to_talk":
                    continue
                edge = self.detector.update(event.rms, assistant_speaking=self.state.speaking)
                if edge == "start":
                    self._active_turn = await self.turns.new_turn()
                    self.latency.mark(self._active_turn, "speech_start")
                    await self.bus.publish(SpeechStarted(turn_id=self._active_turn, rms=event.rms))
                elif edge == "end" and self._active_turn is not None:
                    duration = int((time.monotonic() - self.detector.started_at) * 1000)
                    self.latency.mark(self._active_turn, "speech_end")
                    await self.bus.publish(SpeechEnded(turn_id=self._active_turn, duration_ms=duration))

    async def _mode_loop(self) -> None:
        sub = await self.bus.subscribe(ListeningModeChanged, maxsize=8, name="vad-mode")
        async with sub:
            while True:
                event = await sub.get()
                self._mode = event.mode
                if event.mode == "touch_to_talk":
                    self.detector.speaking = False
                    self.detector.active_frames = 0
                    self.detector.silent_frames = 0

    async def _ptt_loop(self) -> None:
        start_sub = await self.bus.subscribe(PushToTalkStarted, maxsize=8, name="vad-ptt-start")
        end_sub = await self.bus.subscribe(PushToTalkEnded, maxsize=8, name="vad-ptt-end")
        async with start_sub, end_sub:
            while True:
                start_task = asyncio.create_task(start_sub.get())
                end_task = asyncio.create_task(end_sub.get())
                done, pending = await asyncio.wait({start_task, end_task}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                event = next(iter(done)).result()
                if self._mode != "touch_to_talk":
                    continue
                if isinstance(event, PushToTalkStarted):
                    self._active_turn = await self.turns.new_turn()
                    self._ptt_started_at = time.monotonic()
                    self.latency.mark(self._active_turn, "speech_start")
                    await self.bus.publish(SpeechStarted(turn_id=self._active_turn, rms=0.0))
                elif isinstance(event, PushToTalkEnded) and self._active_turn is not None:
                    duration = int((time.monotonic() - self._ptt_started_at) * 1000)
                    self.latency.mark(self._active_turn, "speech_end")
                    await self.bus.publish(SpeechEnded(turn_id=self._active_turn, duration_ms=duration))
                    self._active_turn = None

