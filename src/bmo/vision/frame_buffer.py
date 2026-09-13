"""Bounded RAM-only frame ring with sharpness-based representative selection."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class FrameRecord:
    frame_id: str
    frame: Any
    timestamp: str
    monotonic_time: float
    sharpness: float
    brightness: float


class FrameBuffer:
    """Keep only a few seconds of frames in RAM; no persistence by default."""

    def __init__(self, max_frames: int) -> None:
        self._frames: deque[FrameRecord] = deque(maxlen=max(2, max_frames))

    def add(self, frame: Any, *, sharpness: float | None = None, brightness: float | None = None) -> FrameRecord:
        if sharpness is None or brightness is None:
            measured_sharpness, measured_brightness = self._metrics(frame)
            sharpness = measured_sharpness if sharpness is None else sharpness
            brightness = measured_brightness if brightness is None else brightness
        record = FrameRecord(
            frame_id=uuid4().hex[:12],
            frame=frame,
            timestamp=datetime.now(UTC).isoformat(),
            monotonic_time=time.monotonic(),
            sharpness=float(sharpness),
            brightness=float(brightness),
        )
        self._frames.append(record)
        return record

    def get(self, frame_id: str) -> FrameRecord | None:
        for record in reversed(self._frames):
            if record.frame_id == frame_id:
                return record
        return None

    def latest(self) -> FrameRecord | None:
        return self._frames[-1] if self._frames else None

    def select_best(self, *, around_frame_id: str | None = None, window_sec: float = 1.0) -> FrameRecord | None:
        if not self._frames:
            return None
        target = self.get(around_frame_id) if around_frame_id else self._frames[-1]
        if target is None:
            target = self._frames[-1]
        candidates = [
            frame
            for frame in self._frames
            if abs(frame.monotonic_time - target.monotonic_time) <= window_sec
        ]
        return max(candidates or list(self._frames), key=lambda frame: frame.sharpness)

    @property
    def size(self) -> int:
        return len(self._frames)

    @staticmethod
    def _metrics(frame: Any) -> tuple[float, float]:
        try:
            import cv2
            import numpy as np

            array = np.asarray(frame)
            gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY) if array.ndim == 3 else array
            return float(cv2.Laplacian(gray, cv2.CV_64F).var()), float(gray.mean())
        except Exception:
            return 0.0, 0.0
