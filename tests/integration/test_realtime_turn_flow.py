"""Streaming TTS and interruption acceptance tests using an in-memory ElevenLabs client."""

import asyncio

import pytest

from bmo.audio.elevenlabs_tts import TTSService
from bmo.config import TTSConfig
from bmo.event_bus import EventBus
from bmo.events import AgentTextChunk, AgentTurnFinished, BargeIn
from bmo.state import TurnManager


class FakeElevenClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def start_context(self, turn_id: int) -> str:
        ctx = f"turn-{turn_id}"
        self.calls.append(("start", turn_id, ctx))
        return ctx

    async def send_text(self, context_id: str, text: str, *, flush: bool = False) -> None:
        self.calls.append(("text", context_id, text, flush))

    async def finish_context(self, context_id: str) -> None:
        self.calls.append(("finish", context_id))

    async def cancel_context(self, turn_id: int) -> None:
        self.calls.append(("cancel", turn_id))

    async def close(self) -> None:
        self.calls.append(("close",))


@pytest.mark.asyncio
async def test_response_text_reaches_tts_before_agent_turn_finishes() -> None:
    bus = EventBus()
    turns = TurnManager()
    turn = await turns.new_turn()
    fake = FakeElevenClient()
    service = TTSService(bus, turns, fake, TTSConfig(min_chunk_chars=10, max_chunk_chars=60))
    await service.start()
    await asyncio.sleep(0)
    try:
        await bus.publish(AgentTextChunk(turn_id=turn, text="Hey there. This response is still being generated "))
        await asyncio.sleep(0.02)
        assert any(call[0] == "text" for call in fake.calls)
        assert not any(call[0] == "finish" for call in fake.calls)
        await bus.publish(AgentTurnFinished(turn_id=turn, full_text="Hey there. This response is still being generated.", silent=False))
        await asyncio.sleep(0.02)
        assert any(call[0] == "finish" for call in fake.calls)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_barge_in_cancels_old_tts_and_stale_text_cannot_resume() -> None:
    bus = EventBus()
    turns = TurnManager()
    old = await turns.new_turn()
    fake = FakeElevenClient()
    service = TTSService(bus, turns, fake, TTSConfig(min_chunk_chars=8, max_chunk_chars=40))
    await service.start()
    await asyncio.sleep(0)
    try:
        await bus.publish(AgentTextChunk(turn_id=old, text="This is a long old answer. "))
        await asyncio.sleep(0.02)
        new = await turns.new_turn()
        await bus.publish(BargeIn(turn_id=new, interrupted_turn_id=old, new_turn_id=new))
        await asyncio.sleep(0.02)
        before = len([c for c in fake.calls if c[0] == "text"])
        await bus.publish(AgentTextChunk(turn_id=old, text="stale words must never speak again. "))
        await asyncio.sleep(0.02)
        after = len([c for c in fake.calls if c[0] == "text"])
        assert ("cancel", old) in fake.calls
        assert after == before
    finally:
        await service.stop()
