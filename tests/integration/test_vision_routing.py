"""Acceptance-style local-vs-cloud visual routing tests with no hardware/API calls."""

import asyncio
from pathlib import Path

import numpy as np
import pytest

from bmo.config import load_settings
from bmo.event_bus import EventBus
from bmo.events import HandObservationEvent, LowConfidenceVisualCandidate, VisualEvent
from bmo.vision.frame_buffer import FrameBuffer
from bmo.vision.gemini_vision import GeminiVisionResult, MockVisionClient
from bmo.vision.pipeline import CloudEscalationService, LocalVisionService

ROOT = Path(__file__).resolve().parents[2]


class FakeHandTracker:
    available = True

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def submit(self, frame_id, frame, timestamp_ms) -> None:
        del frame_id, frame, timestamp_ms


@pytest.mark.asyncio
async def test_high_confidence_wave_stays_local() -> None:
    settings = load_settings(ROOT / "config", ROOT, env_file=ROOT / "tests" / "fixtures" / "empty.env")
    bus = EventBus()
    frames = FrameBuffer(8)
    local = LocalVisionService(bus, settings.behavior, frames, FakeHandTracker(), None)
    visual = await bus.subscribe(VisualEvent, maxsize=8, name="test-high-wave")
    low = await bus.subscribe(LowConfidenceVisualCandidate, maxsize=8, name="test-low-wave")
    await local.start()
    await asyncio.sleep(0)
    try:
        xs = [0.38, 0.62, 0.36, 0.65, 0.35, 0.64, 0.37]
        for i, x in enumerate(xs):
            await bus.publish(HandObservationEvent(frame_id=f"f{i}", wrist_x=x, wrist_y=0.32, confidence=0.96))
            await asyncio.sleep(0.005)
        event = await asyncio.wait_for(visual.get(), timeout=1)
        assert event.type == "wave"
        assert event.source == "local_vision"
        assert event.confidence >= 0.60
        assert low.qsize == 0
    finally:
        visual.close()
        low.close()
        await local.stop()


@pytest.mark.asyncio
async def test_low_confidence_candidate_uploads_exactly_one_representative_frame() -> None:
    settings = load_settings(ROOT / "config", ROOT, env_file=ROOT / "tests" / "fixtures" / "empty.env")
    bus = EventBus()
    frames = FrameBuffer(8)
    record = frames.add(np.zeros((120, 160, 3), dtype=np.uint8), sharpness=12, brightness=30)
    client = MockVisionClient(
        GeminiVisionResult(
            event_type="wave",
            description="The raised hand is probably waving.",
            confidence=0.71,
            should_assistant_react=True,
            suggested_context="probable wave",
        )
    )
    service = CloudEscalationService(bus, settings.behavior, frames, client)
    visual = await bus.subscribe(VisualEvent, maxsize=4, name="test-cloud-result")
    await service.start()
    await asyncio.sleep(0)
    try:
        candidate = LowConfidenceVisualCandidate(
            frame_id=record.frame_id,
            event_type="wave",
            confidence=0.51,
            summary="uncertain wave",
        )
        await bus.publish(candidate)
        event = await asyncio.wait_for(visual.get(), timeout=1)
        assert event.source == "gemini_vision"
        assert client.calls == 1
        await bus.publish(candidate)
        await asyncio.sleep(0.05)
        assert client.calls == 1  # cooldown prevents duplicate upload
    finally:
        visual.close()
        await service.stop()
