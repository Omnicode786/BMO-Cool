"""Browser simulator hardware adapter tests."""

from __future__ import annotations

import asyncio
import base64
import io

import pytest
from PIL import Image

from bmo.config import ControlsConfig
from bmo.event_bus import EventBus
from bmo.events import CameraFrame, ControlAction, ControlEvent, MicAudioChunk, PushToTalkEnded, PushToTalkStarted
from bmo.simulator.bridge import SimulatorAudioCaptureService, SimulatorCameraService, SimulatorControlsService
from bmo.vision.frame_buffer import FrameBuffer


@pytest.mark.asyncio
async def test_simulator_control_publishes_real_control_event() -> None:
    bus = EventBus()
    controls = SimulatorControlsService(bus, ControlsConfig())
    sub = await bus.subscribe(ControlEvent, name="test-control")
    try:
        await controls.trigger("button_a")
        event = await sub.get()
        assert event.action is ControlAction.BUTTON_A
    finally:
        sub.close()
        await controls.stop()


@pytest.mark.asyncio
async def test_simulator_touch_publishes_push_to_talk_edges() -> None:
    bus = EventBus()
    controls = SimulatorControlsService(bus, ControlsConfig())
    starts = await bus.subscribe(PushToTalkStarted, name="test-ptt-start")
    ends = await bus.subscribe(PushToTalkEnded, name="test-ptt-end")
    try:
        await controls.touch_down()
        await controls.touch_up()
        assert (await starts.get()).source == "simulator-touch"
        assert (await ends.get()).source == "simulator-touch"
    finally:
        starts.close()
        ends.close()
        await controls.stop()


@pytest.mark.asyncio
async def test_touch_to_talk_hold_does_not_publish_touch_gesture() -> None:
    bus = EventBus()
    controls = SimulatorControlsService(bus, ControlsConfig())
    gestures = await bus.subscribe(ControlEvent, name="test-ptt-no-gesture")
    starts = await bus.subscribe(PushToTalkStarted, name="test-ptt-mode-start")
    ends = await bus.subscribe(PushToTalkEnded, name="test-ptt-mode-end")
    controls.set_listening_mode("touch_to_talk")
    try:
        await controls.touch_down()
        await asyncio.sleep(0.01)
        await controls.touch_up()
        assert (await starts.get()).source == "simulator-touch"
        assert (await ends.get()).source == "simulator-touch"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(gestures.get(), timeout=0.05)
    finally:
        gestures.close()
        starts.close()
        ends.close()
        await controls.stop()


@pytest.mark.asyncio
async def test_simulator_microphone_publishes_normal_capture_event() -> None:
    bus = EventBus()
    capture = SimulatorAudioCaptureService(bus)
    sub = await bus.subscribe(MicAudioChunk, name="test-mic")
    pcm = b"\x01\x00\xff\x7f"
    try:
        await capture.ingest(base64.b64encode(pcm).decode("ascii"), 16_000)
        event = await sub.get()
        assert event.pcm == pcm
        assert event.sample_rate == 16_000
        assert capture.available
    finally:
        sub.close()


@pytest.mark.asyncio
async def test_simulator_camera_feeds_frame_buffer_and_camera_event() -> None:
    bus = EventBus()
    frames = FrameBuffer(4)
    camera = SimulatorCameraService(bus, frames)
    sub = await bus.subscribe(CameraFrame, name="test-camera")
    image = Image.new("RGB", (16, 12), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    try:
        record = await camera.ingest_jpeg(base64.b64encode(buffer.getvalue()).decode("ascii"))
        event = await sub.get()
        assert event.frame_id == record.frame_id
        assert (event.width, event.height) == (16, 12)
        assert frames.latest() is record
        assert camera.available
    finally:
        sub.close()
