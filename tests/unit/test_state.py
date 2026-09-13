"""Central state-machine transition tests."""

import asyncio

import pytest

from bmo.event_bus import EventBus
from bmo.state import AppState, StateMachine


@pytest.mark.asyncio
async def test_state_transitions_are_explicit_and_validated() -> None:
    bus = EventBus()
    state = StateMachine(bus)
    await state.transition(AppState.IDLE, "boot")
    await state.transition(AppState.LISTENING, "speech")
    await state.transition(AppState.THINKING, "agent")
    await state.transition(AppState.SPEAKING, "audio")
    await state.transition(AppState.IDLE, "done")
    assert state.current is AppState.IDLE


@pytest.mark.asyncio
async def test_invalid_transition_raises_without_force() -> None:
    state = StateMachine(EventBus())
    with pytest.raises(ValueError):
        await state.transition(AppState.SPEAKING, "not allowed directly from boot")


@pytest.mark.asyncio
async def test_state_recovers_when_tts_turn_fails() -> None:
    from bmo.events import AgentTurnStarted, TTSTurnFailed
    from bmo.state import StateEventService

    bus = EventBus()
    state = StateMachine(bus)
    service = StateEventService(bus, state, expect_playback=True)
    await service.start()
    await asyncio.sleep(0)
    try:
        await bus.publish(AgentTurnStarted(turn_id=1, prompt_summary="hello"))
        await asyncio.sleep(0.01)
        assert state.current is AppState.THINKING
        await bus.publish(TTSTurnFailed(turn_id=1, detail="ConnectionError"))
        await asyncio.sleep(0.01)
        assert state.current is AppState.IDLE
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_state_returns_idle_if_tts_is_not_configured() -> None:
    from bmo.events import AgentTurnFinished, AgentTurnStarted
    from bmo.state import StateEventService

    bus = EventBus()
    state = StateMachine(bus)
    service = StateEventService(bus, state, expect_playback=False)
    await service.start()
    await asyncio.sleep(0)
    try:
        await bus.publish(AgentTurnStarted(turn_id=1, prompt_summary="hello"))
        await asyncio.sleep(0.01)
        assert state.current is AppState.THINKING
        await bus.publish(AgentTurnFinished(turn_id=1, full_text="hi", silent=False))
        await asyncio.sleep(0.01)
        assert state.current is AppState.IDLE
    finally:
        await service.stop()
