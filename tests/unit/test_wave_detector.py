"""Temporal wave recognition tests: oscillation is required; a raised hand is not enough."""

from bmo.config import WaveConfig
from bmo.vision.wave_detector import HandObservation, WaveDetector


def obs(t: float, x: float, y: float = 0.35, confidence: float = 0.95) -> HandObservation:
    return HandObservation(timestamp=t, wrist_x=x, wrist_y=y, confidence=confidence)


def test_temporal_wave_requires_reversals_and_amplitude() -> None:
    cfg = WaveConfig(cooldown_sec=15)
    detector = WaveDetector(cfg)
    results = [
        detector.update(obs(i * 0.18, x))
        for i, x in enumerate([0.38, 0.62, 0.36, 0.65, 0.35, 0.64, 0.37])
    ]
    waves = [result for result in results if result.is_wave]
    assert len(waves) == 1
    result = waves[0]
    assert result.confidence >= cfg.local_confidence_threshold
    assert result.reversals >= cfg.min_reversals
    assert result.amplitude >= cfg.min_amplitude


def test_static_raised_hand_is_not_wave() -> None:
    detector = WaveDetector(WaveConfig())
    result = None
    for i in range(9):
        result = detector.update(obs(i * 0.18, 0.50 + (i % 2) * 0.002))
    assert result is not None
    assert not result.is_wave
    assert not result.is_candidate


def test_uncertain_oscillation_becomes_cloud_candidate_not_local_wave() -> None:
    detector = WaveDetector(WaveConfig())
    results = [
        detector.update(obs(i * 0.18, x))
        for i, x in enumerate([0.45, 0.56, 0.47, 0.56, 0.46, 0.55, 0.47])
    ]
    assert not any(result.is_wave for result in results)
    candidates = [result for result in results if result.is_candidate]
    assert len(candidates) == 1
    assert candidates[0].confidence >= 0.30
