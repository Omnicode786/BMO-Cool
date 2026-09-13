"""Conversation coordinator connecting committed STT/events to the streaming ADK brain."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Protocol

from ..config import AppBehaviorConfig
from ..event_bus import EventBus
from ..events import AgentTextChunk, AgentTurnFinished, AgentTurnStarted, BargeIn, TranscriptCommitted, VisualEvent
from ..latency import LatencyTracker
from ..state import AppState, StateMachine, TurnManager
from .adk_agent import ADKAgentRuntime
from .memory import MemoryStore

LOG = logging.getLogger(__name__)


class BrainBackend(Protocol):
    async def stream(self, prompt: str, *, max_llm_calls: int | None = None) -> AsyncIterator[str]: ...


class BrainService:
    """Prioritize user speech, stream brain deltas, and gate proactive visual chatter."""

    def __init__(
        self,
        bus: EventBus,
        state: StateMachine,
        turns: TurnManager,
        memory: MemoryStore,
        backend: BrainBackend,
        behavior: AppBehaviorConfig,
        latency: LatencyTracker,
        mood_provider: object | None = None,
        event_policy: object | None = None,
    ) -> None:
        self.bus = bus
        self.state = state
        self.turns = turns
        self.memory = memory
        self.backend = backend
        self.behavior = behavior
        self.latency = latency
        self.mood_provider = mood_provider
        self.event_policy = event_policy
        self._tasks: list[asyncio.Task[None]] = []
        self._generation: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._transcript_loop(), name="brain-transcripts"),
            asyncio.create_task(self._visual_loop(), name="brain-visual"),
            asyncio.create_task(self._barge_loop(), name="brain-barge"),
        ]

    async def stop(self) -> None:
        await self.cancel_generation()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def cancel_generation(self) -> None:
        if self._generation and not self._generation.done():
            self._generation.cancel()
            await asyncio.gather(self._generation, return_exceptions=True)
        self._generation = None

    async def _transcript_loop(self) -> None:
        sub = await self.bus.subscribe(TranscriptCommitted, maxsize=32, name="brain-transcripts")
        async with sub:
            while True:
                event = await sub.get()
                if not self.turns.is_current(event.turn_id):
                    continue
                await self.cancel_generation()
                explicit = self.memory.extract_explicit_memory(event.text)
                memory_note = ""
                if explicit and self.memory.enabled:
                    item = await self.memory.remember(explicit, kind="explicit", tags=["voice_request"])
                    memory_note = f"\nRuntime note: the local memory layer already stored this explicit request as memory #{item.id}."
                if explicit and not self.memory.enabled:
                    memory_note = "\nRuntime note: durable local memory is disabled, so this remember request was not persisted."
                relevant = await self.memory.search(event.text, self.behavior.memory.retrieval_limit)
                memory_context = "\n".join(f"- {item.text}" for item in relevant[: self.behavior.memory.retrieval_limit])
                mood = getattr(self.mood_provider, "current", self.behavior.personality.default_mood)
                prompt = (
                    f"User said: {event.text}\n"
                    f"Current fictional mood: {mood}. Device state: {self.state.current.value}.\n"
                    f"Relevant local remembered facts (may be empty):\n{memory_context or '- none'}"
                    f"{memory_note}"
                )
                await self.memory.add_turn("user", event.text)
                self._generation = asyncio.create_task(
                    self._generate(int(event.turn_id), prompt, proactive=False),
                    name=f"brain-turn-{event.turn_id}",
                )

    async def _visual_loop(self) -> None:
        sub = await self.bus.subscribe(VisualEvent, maxsize=32, name="brain-visual")
        async with sub:
            while True:
                event = await sub.get()
                if self.event_policy is not None:
                    event = self.event_policy.apply(event, self.state.current)
                    if event is None:
                        continue
                await self.memory.add_visual_event(event.type, event.confidence, event.summary)
                if not event.should_assistant_react or self.state.current is not AppState.IDLE:
                    continue
                await self.cancel_generation()
                turn_id = await self.turns.new_turn()
                prompt = (
                    "A filtered local visual event occurred. Decide if a tiny social reaction is worthwhile; "
                    "otherwise output exactly [SILENCE]. Do not request an image for a high-confidence local event.\n"
                    f"Event: type={event.type}; confidence={event.confidence:.2f}; source={event.source}; summary={event.summary}"
                )
                self._generation = asyncio.create_task(
                    self._generate(turn_id, prompt, proactive=True),
                    name=f"brain-proactive-{turn_id}",
                )

    async def _barge_loop(self) -> None:
        sub = await self.bus.subscribe(BargeIn, maxsize=16, name="brain-barge")
        async with sub:
            while True:
                await sub.get()
                await self.cancel_generation()

    async def _generate(self, turn_id: int, prompt: str, *, proactive: bool) -> None:
        await self.bus.publish(AgentTurnStarted(turn_id=turn_id, prompt_summary=prompt[:100]))
        self.latency.mark(turn_id, "gemini_request_start")
        chunks: list[str] = []
        first = True
        try:
            async for chunk in self.backend.stream(
                prompt,
                max_llm_calls=(
                    self.behavior.gemini.proactive_max_llm_calls if proactive else self.behavior.gemini.max_llm_calls
                ),
            ):
                if not self.turns.is_current(turn_id):
                    return
                if first:
                    first = False
                    self.latency.mark(turn_id, "gemini_first_token")
                chunks.append(chunk)
                if not proactive:
                    await self.bus.publish(AgentTextChunk(turn_id=turn_id, text=chunk))
            full = "".join(chunks).strip()
            silent = not full or full.upper() == "[SILENCE]"
            if proactive and not silent and self.turns.is_current(turn_id):
                # Proactive output is short by instruction; buffering it prevents the literal
                # [SILENCE] sentinel from ever leaking into TTS.
                await self.bus.publish(AgentTextChunk(turn_id=turn_id, text=full))
            if full and not silent:
                await self.memory.add_turn("assistant", full)
            await self.bus.publish(AgentTurnFinished(turn_id=turn_id, full_text=full, silent=silent))
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.bus.publish(AgentTurnFinished(turn_id=turn_id, full_text="", silent=True))
            raise
        except Exception:
            # A transient cloud failure must not take down the event loop; network clients and
            # the next turn get a fresh chance to recover. Hardware-local services keep running.
            LOG.exception("brain generation failed", extra={"turn_id": turn_id})
            await self.bus.publish(AgentTurnFinished(turn_id=turn_id, full_text="", silent=True))


class MockBrainBackend:
    """Integration-test brain that streams configurable text in small deltas."""

    def __init__(self, response: str = "Hey! Tiny robot business, mostly.") -> None:
        self.response = response

    async def stream(self, prompt: str, *, max_llm_calls: int | None = None) -> AsyncIterator[str]:
        del prompt, max_llm_calls
        words = self.response.split()
        for index, word in enumerate(words):
            await asyncio.sleep(0)
            yield word + (" " if index < len(words) - 1 else "")
