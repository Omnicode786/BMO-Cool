"""CPU-friendly local OCR using Tesseract; not used as scene understanding."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class OCRResult:
    text: str
    confidence: float


class TesseractOCR:
    """Extract visible text locally and expose a normalized confidence score."""

    def __init__(self, confidence_threshold: float = 0.62) -> None:
        self.confidence_threshold = confidence_threshold

    async def read(self, frame: Any) -> OCRResult:
        return await asyncio.to_thread(self._read_sync, frame)

    @staticmethod
    def _read_sync(frame: Any) -> OCRResult:
        import cv2
        import pytesseract
        from pytesseract import Output

        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY) if getattr(frame, "ndim", 0) == 3 else frame
        gray = cv2.bilateralFilter(gray, 5, 35, 35)
        data = pytesseract.image_to_data(gray, output_type=Output.DICT, config="--psm 6")
        words: list[str] = []
        confidences: list[float] = []
        for text, confidence in zip(data.get("text", []), data.get("conf", [])):
            clean = str(text).strip()
            try:
                value = float(confidence)
            except (TypeError, ValueError):
                value = -1
            if clean and value >= 0:
                words.append(clean)
                confidences.append(value / 100.0)
        return OCRResult(" ".join(words), sum(confidences) / len(confidences) if confidences else 0.0)
