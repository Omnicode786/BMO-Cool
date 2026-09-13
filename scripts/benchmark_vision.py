#!/usr/bin/env python3
"""Offline CPU benchmark for the motion gate, person detector, and optional MediaPipe model."""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from bmo.config import load_settings  # noqa: E402
from bmo.event_bus import EventBus  # noqa: E402
from bmo.vision.hand_tracker import MediaPipeHandTracker  # noqa: E402
from bmo.vision.motion import MotionDetector  # noqa: E402
from bmo.vision.object_detector import OpenCVPersonDetector  # noqa: E402


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", type=Path, default=ROOT / "tests" / "fixtures" / "scene.png")
    parser.add_argument("--iterations", type=int, default=100)
    args = parser.parse_args()
    settings = load_settings(ROOT / "config", ROOT)
    if args.image.exists():
        frame = np.asarray(Image.open(args.image).convert("RGB"))
    else:
        w, h = settings.behavior.performance.active.camera_resolution
        frame = np.zeros((h, w, 3), dtype=np.uint8)

    motion = MotionDetector(settings.behavior.vision.motion)
    person = OpenCVPersonDetector()
    # Warm motion baseline, then alternate a shifted rectangle to create real work.
    motion.analyze(frame)
    motion_ms: list[float] = []
    person_ms: list[float] = []
    for i in range(max(1, args.iterations)):
        sample = frame.copy()
        x = 10 + (i % 20)
        sample[20:80, x : x + 40] = 255 - sample[20:80, x : x + 40]
        start = time.perf_counter()
        motion.analyze(sample)
        motion_ms.append((time.perf_counter() - start) * 1000)
        if i < min(20, args.iterations):
            start = time.perf_counter()
            person.detect(sample)
            person_ms.append((time.perf_counter() - start) * 1000)

    print(f"profile={settings.behavior.performance.mode} frame={frame.shape[1]}x{frame.shape[0]}")
    print(f"motion: mean={statistics.mean(motion_ms):.2f}ms p95={percentile(motion_ms, 0.95):.2f}ms")
    if person_ms:
        print(f"OpenCV HOG person detector: mean={statistics.mean(person_ms):.2f}ms p95={percentile(person_ms, 0.95):.2f}ms")
    model = ROOT / settings.behavior.vision.hand_landmarker_model
    tracker = MediaPipeHandTracker(EventBus(), model)
    print(f"MediaPipe hand model: {'present' if model.exists() else 'missing'} ({model})")
    print("Tip: choose BMO_PERFORMANCE_MODE=lite if p95 vision latency is too high for your Pi 4.")
    del tracker
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
