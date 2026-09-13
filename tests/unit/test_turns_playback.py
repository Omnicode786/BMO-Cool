"""Turn cancellation and stale PCM rejection tests."""

import asyncio

import pytest

from bmo.audio.playback import MemoryPlaybackService
from bmo.event_bus import EventBus
from bmo.events import TTSAudioChunk
from bmo.state import TurnManager


@pytest.mark.asyncio
async def test_new_turn_invalidates_previous_turn() -> None:
    turns = TurnManager()
    first = await turns.new_turn()
    assert turns.is_current(first)
    second = await turns.new_turn()
    assert second == first + 1
    assert not turns.is_current(first)
    assert turns.is_current(second)


@pytest.mark.asyncio
async def test_stale_audio_is_rejected_after_barge_in_turn_change() -> None:
    bus = EventBus()
    turns = TurnManager()
    sink = MemoryPlaybackService(bus, turns)
    await sink.start()
    await asyncio.sleep(0)
    first = await turns.new_turn()
    await bus.publish(TTSAudioChunk(turn_id=first, pcm=b"old", sample_rate=24000, context_id="old"))
    await asyncio.sleep(0.01)
    second = await turns.new_turn()
    await bus.publish(TTSAudioChunk(turn_id=first, pcm=b"stale", sample_rate=24000, context_id="old"))
    await bus.publish(TTSAudioChunk(turn_id=second, pcm=b"new", sample_rate=24000, context_id="new"))
    await asyncio.sleep(0.02)
    await sink.stop()
    assert (first, b"old") in sink.played
    assert (first, b"stale") not in sink.played
    assert (second, b"new") in sink.played
