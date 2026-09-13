"""Proactive event cooldown and deduplication tests."""

from bmo.config import EventPolicyConfig
from bmo.events import VisualEvent
from bmo.state import AppState
from bmo.vision.event_classifier import EventPolicy


def wave(summary: str = "A person appears to be waving toward the device.") -> VisualEvent:
    return VisualEvent(
        type="wave",
        confidence=0.86,
        source="local_vision",
        person_present=True,
        summary=summary,
        should_assistant_react=True,
    )


def test_duplicate_event_is_dropped() -> None:
    policy = EventPolicy(EventPolicyConfig(dedupe_window_sec=6, visual_reaction_cooldown_sec=8))
    first = policy.apply(wave(), AppState.IDLE, now=10.0)
    second = policy.apply(wave(), AppState.IDLE, now=11.0)
    assert first is not None and first.should_assistant_react
    assert second is None


def test_reaction_cooldown_keeps_distinct_event_silent() -> None:
    policy = EventPolicy(EventPolicyConfig(dedupe_window_sec=1, visual_reaction_cooldown_sec=8))
    assert policy.apply(wave("wave one"), AppState.IDLE, now=10.0).should_assistant_react
    second = policy.apply(wave("a different wave gesture"), AppState.IDLE, now=12.0)
    assert second is not None
    assert not second.should_assistant_react


def test_visual_reaction_never_preempts_listening() -> None:
    policy = EventPolicy(EventPolicyConfig())
    filtered = policy.apply(wave(), AppState.LISTENING, now=10.0)
    assert filtered is not None
    assert not filtered.should_assistant_react
