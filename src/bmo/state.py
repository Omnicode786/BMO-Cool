"""Central application state machine and monotonic turn ownership."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

from .event_bus import EventBus
from .events import (
    AgentTurnFinished,
    AgentTurnStarted,
    BargeIn,
    NetworkLost,
    NetworkRestored,
    PlaybackStarted,
    PlaybackStopped,
    ShutdownRequested,
    SpeechStarted,
    StateChanged,
    TTSTurnFailed,
)

LOG = logging.getLogger(__name__)


class AppState(str, Enum):
    BOOTING = "BOOTING"
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    SLEEPING = "SLEEPING"
    OFFLINE = "OFFLINE"
    ERROR = "ERROR"


_ALLOWED: dict[AppState, set[AppState]] = {
    AppState.BOOTING: {AppState.IDLE, AppState.OFFLINE, AppState.ERROR},
    AppState.IDLE: {AppState.LISTENING, AppState.THINKING, AppState.SPEAKING, AppState.SLEEPING, AppState.OFFLINE, AppState.ERROR},
    AppState.LISTENING: {AppState.IDLE, AppState.THINKING, AppState.SPEAKING, AppState.SLEEPING, AppState.OFFLINE, AppState.ERROR},
    AppState.THINKING: {AppState.IDLE, AppState.LISTENING, AppState.SPEAKING, AppState.SLEEPING, AppState.OFFLINE, AppState.ERROR},
    AppState.SPEAKING: {AppState.IDLE, AppState.LISTENING, AppState.THINKING, AppState.SLEEPING, AppState.OFFLINE, AppState.ERROR},
    AppState.SLEEPING: {AppState.IDLE, AppState.OFFLINE, AppState.ERROR},
    AppState.OFFLINE: {AppState.IDLE, AppState.LISTENING, AppState.SLEEPING, AppState.ERROR},
    AppState.ERROR: {AppState.IDLE, AppState.OFFLINE, AppState.SLEEPING},
}


class StateMachine:
    """Serialize state transitions and publish every accepted change."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._state = AppState.BOOTING
        self._lock = asyncio.Lock()
        self._network_down: set[str] = set()

    @property
    def current(self) -> AppState:
        return self._state

    @property
    def speaking(self) -> bool:
        return self._state is AppState.SPEAKING

    async def transition(self, target: AppState, reason: str, *, force: bool = False) -> bool:
        async with self._lock:
            previous = self._state
            if target is previous:
                return False
            if not force and target not in _ALLOWED[previous]:
                raise ValueError(f"invalid state transition {previous.value} -> {target.value}: {reason}")
            self._state = target
        LOG.info("state transition %s -> %s (%s)", previous.value, target.value, reason)
        await self._bus.publish(StateChanged(previous=previous.value, current=target.value, reason=reason))
        return True

    async def mark_network_lost(self, service: str) -> None:
        self._network_down.add(service)
        if self._state is not AppState.SLEEPING:
            await self.transition(AppState.OFFLINE, f"{service} network unavailable", force=True)

    async def mark_network_restored(self, service: str) -> None:
        self._network_down.discard(service)
        if not self._network_down and self._state is AppState.OFFLINE:
            await self.transition(AppState.IDLE, "network restored", force=True)


@dataclass(slots=True)
class TurnSnapshot:
    turn_id: int
    cancelled: bool


class TurnManager:
    """Owns the single current conversational turn and invalidates stale output."""

    def __init__(self) -> None:
        self._turn_id = 0
        self._cancelled_before = 0
        self._lock = asyncio.Lock()

    @property
    def current(self) -> int:
        return self._turn_id

    async def new_turn(self) -> int:
        async with self._lock:
            self._cancelled_before = max(self._cancelled_before, self._turn_id)
            self._turn_id += 1
            return self._turn_id

    async def invalidate_current(self) -> int:
        """Cancel current output immediately and reserve a new user turn id."""
        return await self.new_turn()

    def is_current(self, turn_id: int | None) -> bool:
        return turn_id is not None and turn_id == self._turn_id and turn_id > self._cancelled_before

    def snapshot(self, turn_id: int) -> TurnSnapshot:
        return TurnSnapshot(turn_id=turn_id, cancelled=not self.is_current(turn_id))


class StateEventService:
    """Translate high-level events into centralized state transitions."""

    def __init__(self, bus: EventBus, state: StateMachine, *, expect_playback: bool = True) -> None:
        self.bus = bus
        self.state = state
        self.expect_playback = expect_playback
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        await self.state.transition(AppState.IDLE, "boot complete")
        self._tasks = [
            asyncio.create_task(self._speech_loop(), name="state-speech"),
            asyncio.create_task(self._agent_loop(), name="state-agent"),
            asyncio.create_task(self._agent_finish_loop(), name="state-agent-finish"),
            asyncio.create_task(self._playback_loop(), name="state-playback"),
            asyncio.create_task(self._tts_failure_loop(), name="state-tts-failure"),
            asyncio.create_task(self._network_loop(), name="state-network"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _speech_loop(self) -> None:
        sub = await self.bus.subscribe(SpeechStarted, maxsize=16, name="state-speech")
        async with sub:
            while True:
                event = await sub.get()
                if self.state.current not in {AppState.SLEEPING, AppState.OFFLINE}:
                    await self.state.transition(AppState.LISTENING, "speech detected", force=True)

    async def _agent_loop(self) -> None:
        sub = await self.bus.subscribe(AgentTurnStarted, maxsize=16, name="state-agent")
        async with sub:
            while True:
                await sub.get()
                if self.state.current is not AppState.SLEEPING:
                    await self.state.transition(AppState.THINKING, "agent generation", force=True)

    async def _agent_finish_loop(self) -> None:
        sub = await self.bus.subscribe(AgentTurnFinished, maxsize=16, name="state-agent-finish")
        async with sub:
            while True:
                event = await sub.get()
                if self.state.current is AppState.THINKING and (event.silent or not self.expect_playback):
                    reason = "agent chose silence" if event.silent else "speech output unavailable"
                    await self.state.transition(AppState.IDLE, reason, force=True)

    async def _tts_failure_loop(self) -> None:
        sub = await self.bus.subscribe(TTSTurnFailed, maxsize=16, name="state-tts-failure")
        async with sub:
            while True:
                event = await sub.get()
                if self.state.current in {AppState.THINKING, AppState.SPEAKING}:
                    await self.state.transition(AppState.IDLE, f"TTS failed: {event.detail}", force=True)

    async def _playback_loop(self) -> None:
        started = await self.bus.subscribe(PlaybackStarted, maxsize=16, name="state-playback-start")
        stopped = await self.bus.subscribe(PlaybackStopped, maxsize=16, name="state-playback-stop")
        try:
            while True:
                get_start = asyncio.create_task(started.get())
                get_stop = asyncio.create_task(stopped.get())
                done, pending = await asyncio.wait({get_start, get_stop}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if get_start in done:
                    event = get_start.result()
                    await self.state.transition(AppState.SPEAKING, "speaker playback", force=True)
                else:
                    event = get_stop.result()
                    if self.state.current is not AppState.SLEEPING:
                        target = AppState.LISTENING if event.interrupted else AppState.IDLE
                        await self.state.transition(target, "playback stopped", force=True)
        finally:
            started.close()
            stopped.close()

    async def _network_loop(self) -> None:
        lost = await self.bus.subscribe(NetworkLost, maxsize=16, name="state-net-lost")
        restored = await self.bus.subscribe(NetworkRestored, maxsize=16, name="state-net-restored")
        try:
            while True:
                get_lost = asyncio.create_task(lost.get())
                get_restored = asyncio.create_task(restored.get())
                done, pending = await asyncio.wait({get_lost, get_restored}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if get_lost in done:
                    await self.state.mark_network_lost(get_lost.result().service)
                else:
                    await self.state.mark_network_restored(get_restored.result().service)
        finally:
            lost.close()
            restored.close()
