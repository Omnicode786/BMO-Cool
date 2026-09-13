"""Cheap grayscale frame-difference motion gate with lighting/exposure rejection."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..config import MotionConfig


@dataclass(slots=True)
class MotionDecision:
    meaningful: bool
    confidence: float
    changed_fraction: float
    brightness_delta: float
    lighting_rejected: bool = False


class MotionDetector:
    """Reject one-pixel noise, exposure jumps, and non-persistent motion."""

    def __init__(self, config: MotionConfig) -> None:
        self.config = config
        self._previous: Any = None
        self._persistent = 0
        self._quiet = 0
        self._active = False
        self._last_emit = -1e9
        self._exposure_reject_until = 0.0

    def analyze(self, frame: Any, *, now: float | None = None) -> MotionDecision:
        import cv2
        import numpy as np

        now = time.monotonic() if now is None else now
        array = np.asarray(frame)
        gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY) if array.ndim == 3 else array
        gray = cv2.resize(gray, (160, 90), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        if self._previous is None:
            self._previous = gray
            return MotionDecision(False, 0.0, 0.0, 0.0)

        brightness_delta = abs(float(gray.mean()) - float(self._previous.mean()))
        delta = cv2.absdiff(gray, self._previous)
        changed = delta >= self.config.pixel_delta_threshold
        changed_fraction = float(np.count_nonzero(changed)) / float(changed.size)
        self._previous = gray

        lighting_jump = (
            brightness_delta >= self.config.lighting_change_mean_delta
            and changed_fraction >= self.config.lighting_change_fraction
        )
        if lighting_jump:
            self._persistent = 0
            self._active = False
            self._exposure_reject_until = now + self.config.exposure_reject_sec
            return MotionDecision(False, 0.0, changed_fraction, brightness_delta, lighting_rejected=True)
        if now < self._exposure_reject_until:
            return MotionDecision(False, 0.0, changed_fraction, brightness_delta, lighting_rejected=True)

        changed_enough = changed_fraction >= self.config.min_changed_area
        if changed_enough:
            self._persistent += 1
            self._quiet = 0
        else:
            self._persistent = max(0, self._persistent - 1)
            self._quiet += 1
            if self._quiet >= 2:
                self._active = False

        if not self._active and self._persistent >= self.config.persistence_frames:
            min_interval = max(self.config.debounce_sec, self.config.cooldown_sec)
            if now - self._last_emit < min_interval:
                return MotionDecision(False, 0.0, changed_fraction, brightness_delta)
            self._active = True
            self._last_emit = now

        if not self._active:
            return MotionDecision(False, 0.0, changed_fraction, brightness_delta)
        confidence = min(1.0, changed_fraction / max(self.config.threshold, 1e-6))
        return MotionDecision(True, confidence, changed_fraction, brightness_delta)
