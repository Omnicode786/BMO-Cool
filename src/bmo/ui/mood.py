"""Lightweight fictional mood engine that drives expression without claiming real emotions."""

from __future__ import annotations

import asyncio
import time

from ..config import PersonalityConfig
from ..event_bus import EventBus
from ..events import ControlAction, ControlEvent, MoodChanged, PeripheralFailed, TranscriptCommitted, VisualEvent


class MoodEngine:
    """Maintain bounded valence/arousal from interactions and decay toward baseline."""

    def __init__(self, bus: EventBus, config: PersonalityConfig) -> None:
        self.bus = bus
        self.config = config
        self.valence = 0.45
        self.arousal = 0.35
        self.current = config.default_mood
        self._tasks: list[asyncio.Task[None]] = []
        self._last_update = time.monotonic()

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._event_loop(), name="mood-events"),
            asyncio.create_task(self._decay_loop(), name="mood-decay"),
        ]
        await self._emit()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _event_loop(self) -> None:
        visual = await self.bus.subscribe(VisualEvent, maxsize=16, name="mood-visual")
        control = await self.bus.subscribe(ControlEvent, maxsize=16, name="mood-control")
        failure = await self.bus.subscribe(PeripheralFailed, maxsize=8, name="mood-failure")
        transcript = await self.bus.subscribe(TranscriptCommitted, maxsize=16, name="mood-transcript")
        try:
            while True:
                tasks = {
                    asyncio.create_task(visual.get()): "visual",
                    asyncio.create_task(control.get()): "control",
                    asyncio.create_task(failure.get()): "failure",
                    asyncio.create_task(transcript.get()): "transcript",
                }
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                task = next(iter(done))
                event = task.result()
                kind = tasks[task]
                if kind == "visual" and event.type == "wave":
                    self._nudge(0.18, 0.16)
                elif kind == "control" and event.action == ControlAction.TOUCH_TAP:
                    self._nudge(0.08, 0.08)
                elif kind == "failure":
                    self._nudge(-0.18, 0.15)
                elif kind == "transcript":
                    lower = event.text.lower()
                    if any(word in lower for word in ("thanks", "thank you", "great", "nice", "love")):
                        self._nudge(0.10, 0.02)
                    elif any(word in lower for word in ("shut up", "stupid", "hate you")):
                        self._nudge(-0.16, 0.12)
                await self._emit()
        finally:
            visual.close()
            control.close()
            failure.close()
            transcript.close()

    def _nudge(self, valence: float, arousal: float) -> None:
        self.valence = max(-1.0, min(1.0, self.valence + valence))
        self.arousal = max(0.0, min(1.0, self.arousal + arousal))
        self._last_update = time.monotonic()

    async def _decay_loop(self) -> None:
        while True:
            await asyncio.sleep(max(2.0, self.config.mood_decay_sec / 8.0))
            self.valence += (0.45 - self.valence) * 0.18
            self.arousal += (0.35 - self.arousal) * 0.18
            await self._emit()

    async def _emit(self) -> None:
        if self.valence < -0.45 and self.arousal > 0.5:
            mood = "angry"
        elif self.valence < -0.15:
            mood = "annoyed"
        elif self.arousal > 0.72 and self.valence >= 0:
            mood = "excited"
        elif self.valence > 0.58:
            mood = "happy"
        elif self.arousal < 0.20:
            mood = "sleepy"
        else:
            mood = "curious"
        if mood != self.current:
            self.current = mood
        await self.bus.publish(MoodChanged(mood=mood, valence=self.valence, arousal=self.arousal))
