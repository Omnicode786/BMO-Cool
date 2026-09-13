"""Visual event deduplication, priority, cooldown, and local/cloud reaction policy."""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import replace

from ..config import EventPolicyConfig
from ..events import VisualEvent
from ..state import AppState


class EventPolicy:
    """Prevent low-value/repeated visual events from turning into constant narration."""

    def __init__(self, config: EventPolicyConfig) -> None:
        self.config = config
        self.latest_event: VisualEvent | None = None
        self._last_signature: dict[str, float] = {}
        self._last_type: dict[str, float] = defaultdict(lambda: -1e9)
        self._last_reaction = -1e9

    def priority_for(self, event_type: str) -> int:
        return {
            "wave": self.config.wave_priority,
            "person_arrived": self.config.person_arrived_priority,
            "object_shown": self.config.object_shown_priority,
            "scene_change": self.config.scene_change_priority,
        }.get(event_type, 40)

    def apply(self, event: VisualEvent, state: AppState, *, now: float | None = None) -> VisualEvent | None:
        """Return a filtered event, or None when it is a duplicate/routine background event."""
        now = time.monotonic() if now is None else now
        signature = self._signature(event)
        if now - self._last_signature.get(signature, -1e9) < self.config.dedupe_window_sec:
            return None
        self._last_signature[signature] = now
        self._last_type[event.type] = now

        # Speech/listening always wins over routine proactive visual behavior.
        priority = self.priority_for(event.type)
        eligible = (
            event.should_assistant_react
            and priority >= self.config.proactive_min_priority
            and state is AppState.IDLE
            and now - self._last_reaction >= self.config.visual_reaction_cooldown_sec
        )
        if event.type == "wave" and event.confidence >= 0.60 and state is AppState.IDLE:
            eligible = now - self._last_reaction >= self.config.visual_reaction_cooldown_sec
        if eligible:
            self._last_reaction = now
        filtered = replace(event, should_assistant_react=eligible)
        self.latest_event = filtered
        return filtered

    @staticmethod
    def _signature(event: VisualEvent) -> str:
        words = re.sub(r"[^a-z0-9 ]+", " ", event.summary.lower()).split()
        compact = " ".join(words[:8])
        return f"{event.type}|{round(event.confidence, 1)}|{compact}"
