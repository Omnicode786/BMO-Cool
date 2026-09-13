"""Optional local detectors; default person detector uses OpenCV HOG without cloud upload."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class PersonDetection:
    present: bool
    confidence: float
    box: tuple[int, int, int, int] | None = None


class OpenCVPersonDetector:
    """CPU-only HOG person detector sampled only after the motion gate."""

    def __init__(self) -> None:
        self._hog: Any = None

    def _ensure(self) -> Any:
        if self._hog is None:
            import cv2

            self._hog = cv2.HOGDescriptor()
            self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        return self._hog

    def detect(self, frame: Any) -> PersonDetection:
        import cv2

        hog = self._ensure()
        height, width = frame.shape[:2]
        scale = min(1.0, 360.0 / max(height, width))
        small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else frame
        rects, weights = hog.detectMultiScale(small, winStride=(8, 8), padding=(8, 8), scale=1.08)
        if len(rects) == 0:
            return PersonDetection(False, 0.0)
        best_index = max(range(len(rects)), key=lambda index: float(weights[index]))
        x, y, w, h = [int(value / scale) for value in rects[best_index]]
        raw = float(weights[best_index])
        confidence = max(0.0, min(0.98, 0.5 + raw * 0.12))
        return PersonDetection(True, confidence, (x, y, w, h))
