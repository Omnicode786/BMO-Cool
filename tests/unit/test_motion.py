"""Motion gate tests for persistence, cooldown/debounce, and lighting rejection."""

import numpy as np

from bmo.config import MotionConfig
from bmo.vision.motion import MotionDetector


def frame(rect: bool = False, value: int = 0) -> np.ndarray:
    image = np.full((120, 160, 3), value, dtype=np.uint8)
    if rect:
        image[30:90, 30:100] = 255
    return image


def test_motion_requires_temporal_persistence_and_debounces_reentry() -> None:
    cfg = MotionConfig(
        min_changed_area=0.02,
        persistence_frames=2,
        pixel_delta_threshold=10,
        threshold=0.05,
        debounce_sec=0.5,
        cooldown_sec=0.5,
        lighting_change_mean_delta=100,
    )
    detector = MotionDetector(cfg)
    assert not detector.analyze(frame(False), now=0.0).meaningful
    assert not detector.analyze(frame(True), now=1.0).meaningful
    assert detector.analyze(frame(False), now=1.1).meaningful
    # Two quiet frames reset activity.
    detector.analyze(frame(False), now=1.2)
    detector.analyze(frame(False), now=1.3)
    assert not detector.analyze(frame(True), now=1.35).meaningful
    assert not detector.analyze(frame(False), now=1.40).meaningful  # within cooldown
    # Persistence remains; once the cooldown passes, meaningful motion can reactivate.
    assert detector.analyze(frame(True), now=1.7).meaningful


def test_global_lighting_jump_is_rejected() -> None:
    cfg = MotionConfig(
        min_changed_area=0.01,
        persistence_frames=1,
        pixel_delta_threshold=5,
        lighting_change_mean_delta=15,
        lighting_change_fraction=0.50,
    )
    detector = MotionDetector(cfg)
    detector.analyze(frame(value=20), now=0.0)
    result = detector.analyze(frame(value=90), now=1.0)
    assert result.lighting_rejected
    assert not result.meaningful
