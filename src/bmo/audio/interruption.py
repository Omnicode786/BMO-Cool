"""Barge-in coordination: cancel old generation, TTS context, and playback."""

from __future__ import annotations

import asyncio
import logging

from ..event_bus import EventBus
from ..events import BargeIn, SpeechStarted
from ..state import AppState, StateMachine

LOG = logging.getLogger(__name__)


class InterruptionService:
    """Convert speech detected during output into an immediate barge-in event."""

    def __init__(self, bus: EventBus, state: StateMachine) -> None:
        self.bus = bus
        self.state = state
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="barge-in-detector")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        sub = await self.bus.subscribe(SpeechStarted, maxsize=16, name="barge-in-detector")
        async with sub:
            while True:
                event = await sub.get()
                if self.state.current is AppState.SPEAKING and event.turn_id is not None:
                    interrupted = max(0, event.turn_id - 1)
                    LOG.info(
                        "barge-in invalidating turn %d for turn %d",
                        interrupted,
                        event.turn_id,
                        extra={"turn_id": event.turn_id, "event_id": event.event_id},
                    )
                    await self.bus.publish(
                        BargeIn(
                            event_id=event.event_id,
                            timestamp=event.timestamp,
                            turn_id=event.turn_id,
                            interrupted_turn_id=interrupted,
                            new_turn_id=event.turn_id,
                        )
                    )
