"""MediaPipe Hand Landmarker LIVE_STREAM adapter for Raspberry Pi ARM64."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..event_bus import EventBus
from ..events import HandObservationEvent

LOG = logging.getLogger(__name__)


class MediaPipeHandTracker:
    """Submit motion-gated frames to MediaPipe without blocking the asyncio loop."""

    def __init__(self, bus: EventBus, model_path: Path, min_confidence: float = 0.5) -> None:
        self.bus = bus
        self.model_path = model_path
        self.min_confidence = min_confidence
        self._landmarker: Any = None
        self._mp: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._frame_ids: dict[int, str] = {}
        self.available = False

    async def start(self) -> None:
        if not self.model_path.exists():
            LOG.warning("MediaPipe hand model missing: %s; run scripts/fetch_models.sh", self.model_path)
            return
        try:
            import mediapipe as mp
        except ImportError:
            LOG.warning("MediaPipe not installed; local wave tracking disabled")
            return
        self._mp = mp
        self._loop = asyncio.get_running_loop()
        options = mp.tasks.vision.HandLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(self.model_path)),
            running_mode=mp.tasks.vision.RunningMode.LIVE_STREAM,
            num_hands=1,
            min_hand_detection_confidence=self.min_confidence,
            min_hand_presence_confidence=self.min_confidence,
            min_tracking_confidence=self.min_confidence,
            result_callback=self._callback,
        )
        self._landmarker = mp.tasks.vision.HandLandmarker.create_from_options(options)
        self.available = True
        LOG.info("MediaPipe Hand Landmarker initialized in LIVE_STREAM mode")

    async def stop(self) -> None:
        if self._landmarker is not None:
            await asyncio.to_thread(self._landmarker.close)
        self._landmarker = None
        self.available = False

    def submit(self, frame_id: str, frame: Any, timestamp_ms: int) -> None:
        if not self.available or self._landmarker is None:
            return
        import numpy as np

        array = np.ascontiguousarray(frame)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=array)
        self._frame_ids[timestamp_ms] = frame_id
        # MediaPipe LIVE_STREAM returns immediately and may drop frames to stay realtime.
        self._landmarker.detect_async(image, timestamp_ms)
        if len(self._frame_ids) > 100:
            for old in sorted(self._frame_ids)[:-80]:
                self._frame_ids.pop(old, None)

    def _callback(self, result: Any, _image: Any, timestamp_ms: int) -> None:
        if self._loop is None:
            return
        frame_id = self._frame_ids.pop(timestamp_ms, "unknown")
        if not getattr(result, "hand_landmarks", None):
            return
        landmarks = result.hand_landmarks[0]
        wrist = landmarks[0]
        confidence = 0.75
        handedness = getattr(result, "handedness", None)
        if handedness and handedness[0]:
            confidence = float(getattr(handedness[0][0], "score", confidence))
        event = HandObservationEvent(
            frame_id=frame_id,
            wrist_x=float(wrist.x),
            wrist_y=float(wrist.y),
            confidence=confidence,
            person_present=True,
        )
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self.bus.publish(event)))
