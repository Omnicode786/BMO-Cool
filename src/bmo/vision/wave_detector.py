"""Temporal wave detector using wrist travel and direction reversals over time."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from ..config import WaveConfig


@dataclass(slots=True)
class HandObservation:
    timestamp: float
    wrist_x: float
    wrist_y: float
    confidence: float
    person_present: bool = True


@dataclass(slots=True)
class WaveResult:
    confidence: float
    is_wave: bool
    is_candidate: bool
    amplitude: float
    reversals: int
    raised_ratio: float


class WaveDetector:
    """Require a raised, tracked hand to oscillate; static raised hands are not waves."""

    def __init__(self, config: WaveConfig) -> None:
        self.config = config
        self._history: deque[HandObservation] = deque(maxlen=80)
        self._last_wave = -1e9
        self._last_candidate = -1e9

    def update(self, observation: HandObservation) -> WaveResult:
        self._history.append(observation)
        cutoff = observation.timestamp - self.config.window_sec
        while self._history and self._history[0].timestamp < cutoff:
            self._history.popleft()

        valid = [obs for obs in self._history if obs.confidence >= self.config.min_tracking_confidence]
        if len(valid) < self.config.min_samples:
            return WaveResult(0.0, False, False, 0.0, 0, 0.0)

        xs = [obs.wrist_x for obs in valid]
        amplitude = max(xs) - min(xs)
        raised_ratio = sum(obs.wrist_y <= self.config.max_wrist_y for obs in valid) / len(valid)
        tracking = sum(obs.confidence for obs in valid) / len(valid)

        signs: list[int] = []
        deadband = max(0.012, self.config.min_amplitude / 10.0)
        for left, right in zip(xs, xs[1:]):
            delta = right - left
            sign = 1 if delta > deadband else -1 if delta < -deadband else 0
            if sign and (not signs or sign != signs[-1]):
                signs.append(sign)
        reversals = max(0, len(signs) - 1)

        amplitude_score = min(1.0, amplitude / self.config.min_amplitude)
        reversal_score = min(1.0, reversals / max(1, self.config.min_reversals))
        raised_score = min(1.0, raised_ratio / self.config.raised_ratio)
        span = max(0.001, valid[-1].timestamp - valid[0].timestamp)
        temporal_score = min(1.0, span / min(self.config.window_sec, 1.0))
        confidence = (
            0.30 * tracking
            + 0.30 * amplitude_score
            + 0.22 * reversal_score
            + 0.12 * raised_score
            + 0.06 * temporal_score
        )
        # Gates prevent a high weighted score from turning a single sweep/static hand into a wave.
        wave_shape = (
            amplitude >= self.config.min_amplitude
            and reversals >= self.config.min_reversals
            and raised_ratio >= self.config.raised_ratio
        )
        is_wave = (
            wave_shape
            and confidence >= self.config.local_confidence_threshold
            and observation.timestamp - self._last_wave >= self.config.cooldown_sec
        )
        is_candidate = (
            not is_wave
            and amplitude >= self.config.min_amplitude * 0.55
            and reversals >= 1
            and raised_ratio >= self.config.raised_ratio * 0.7
            and confidence >= self.config.low_confidence_floor
            and observation.timestamp - self._last_candidate >= min(3.0, self.config.cooldown_sec / 3)
        )
        if is_wave:
            self._last_wave = observation.timestamp
            self._last_candidate = observation.timestamp
            self._history.clear()
        elif is_candidate:
            self._last_candidate = observation.timestamp
        return WaveResult(round(confidence, 4), is_wave, is_candidate, amplitude, reversals, raised_ratio)
