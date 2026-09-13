"""Per-turn latency instrumentation for the realtime voice pipeline."""

from __future__ import annotations

import logging
import time
from collections import defaultdict

LOG = logging.getLogger(__name__)


class LatencyTracker:
    """Track monotonic timestamps and emit derived turn latency metrics."""

    STAGES = (
        "speech_start",
        "speech_end",
        "stt_commit",
        "gemini_request_start",
        "gemini_first_token",
        "elevenlabs_first_audio",
        "speaker_first_audio",
    )

    def __init__(self, retain_turns: int = 32) -> None:
        self._marks: dict[int, dict[str, float]] = defaultdict(dict)
        self._retain_turns = retain_turns

    def mark(self, turn_id: int | None, stage: str) -> None:
        if turn_id is None or stage not in self.STAGES:
            return
        marks = self._marks[turn_id]
        marks.setdefault(stage, time.monotonic())
        if stage == "speaker_first_audio":
            self._log(turn_id, marks)
        if len(self._marks) > self._retain_turns:
            for old in sorted(self._marks)[: len(self._marks) - self._retain_turns]:
                self._marks.pop(old, None)

    @staticmethod
    def _ms(marks: dict[str, float], start: str, end: str) -> float | None:
        if start not in marks or end not in marks:
            return None
        return round((marks[end] - marks[start]) * 1000.0, 1)

    def metrics(self, turn_id: int) -> dict[str, float | None]:
        marks = self._marks.get(turn_id, {})
        return {
            "speech_to_stt_ms": self._ms(marks, "speech_end", "stt_commit"),
            "stt_to_first_token_ms": self._ms(marks, "stt_commit", "gemini_first_token"),
            "first_token_to_tts_audio_ms": self._ms(marks, "gemini_first_token", "elevenlabs_first_audio"),
            "speech_end_to_speaker_ms": self._ms(marks, "speech_end", "speaker_first_audio"),
            "gemini_request_to_speaker_ms": self._ms(marks, "gemini_request_start", "speaker_first_audio"),
        }

    def _log(self, turn_id: int, marks: dict[str, float]) -> None:
        LOG.info("latency metrics %s", self.metrics(turn_id), extra={"turn_id": turn_id})
