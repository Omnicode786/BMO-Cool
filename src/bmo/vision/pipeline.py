"""Motion-gated local vision pipeline with temporal wave detection and one-frame escalation."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..config import AppBehaviorConfig
from ..event_bus import EventBus
from ..events import (
    CameraFrame,
    HandObservationEvent,
    LowConfidenceVisualCandidate,
    MotionCandidate,
    VisualEvent,
)
from ..state import StateMachine
from .event_classifier import EventPolicy
from .frame_buffer import FrameBuffer
from .hand_tracker import MediaPipeHandTracker
from .motion import MotionDetector
from .object_detector import OpenCVPersonDetector
from .wave_detector import HandObservation, WaveDetector

LOG = logging.getLogger(__name__)


class MotionService:
    """Apply cheap motion gating to newest camera frames; stale frame events are dropped by the bus."""

    def __init__(self, bus: EventBus, detector: MotionDetector) -> None:
        self.bus = bus
        self.detector = detector
        self.last_latency_ms = 0.0
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="motion-gate")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        sub = await self.bus.subscribe(CameraFrame, maxsize=2, drop_oldest=True, name="motion-camera")
        async with sub:
            while True:
                frame_event = await sub.get()
                started = time.perf_counter()
                result = await asyncio.to_thread(self.detector.analyze, frame_event.frame)
                self.last_latency_ms = (time.perf_counter() - started) * 1000.0
                if result.meaningful:
                    await self.bus.publish(
                        MotionCandidate(
                            frame_id=frame_event.frame_id,
                            confidence=result.confidence,
                            changed_fraction=result.changed_fraction,
                            brightness_delta=result.brightness_delta,
                        )
                    )


class LocalVisionService:
    """Run only motion-triggered local inference and emit wave/person candidates."""

    def __init__(
        self,
        bus: EventBus,
        behavior: AppBehaviorConfig,
        frame_buffer: FrameBuffer,
        hand_tracker: MediaPipeHandTracker,
        person_detector: OpenCVPersonDetector | None = None,
    ) -> None:
        self.bus = bus
        self.behavior = behavior
        self.frame_buffer = frame_buffer
        self.hand_tracker = hand_tracker
        self.person_detector = person_detector
        self.wave = WaveDetector(behavior.vision.wave)
        self.last_inference_latency_ms = 0.0
        self._tasks: list[asyncio.Task[None]] = []
        self._motion_count = 0
        self._person_present = False

    async def start(self) -> None:
        await self.hand_tracker.start()
        self._tasks = [
            asyncio.create_task(self._motion_loop(), name="local-vision-motion"),
            asyncio.create_task(self._hand_loop(), name="local-vision-hands"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.hand_tracker.stop()

    async def _motion_loop(self) -> None:
        sub = await self.bus.subscribe(MotionCandidate, maxsize=3, drop_oldest=True, name="local-vision-candidates")
        async with sub:
            while True:
                event = await sub.get()
                record = self.frame_buffer.get(event.frame_id)
                if record is None:
                    continue
                self._motion_count += 1
                if self.hand_tracker.available and self._motion_count % self.behavior.performance.active.vision_every_n_motion_frames == 0:
                    self.hand_tracker.submit(event.frame_id, record.frame, int(record.monotonic_time * 1000))

                if (
                    self.person_detector is not None
                    and self.behavior.performance.active.person_detector_enabled
                    and self._motion_count % self.behavior.vision.person_detection_stride == 0
                ):
                    started = time.perf_counter()
                    detection = await asyncio.to_thread(self.person_detector.detect, record.frame)
                    self.last_inference_latency_ms = (time.perf_counter() - started) * 1000.0
                    if detection.present and not self._person_present:
                        self._person_present = True
                        await self.bus.publish(
                            VisualEvent(
                                type="person_arrived",
                                confidence=detection.confidence,
                                source="local_vision",
                                person_present=True,
                                summary="A person appears to have newly approached the device.",
                                should_assistant_react=detection.confidence >= 0.70,
                            )
                        )
                    elif not detection.present and self._person_present:
                        self._person_present = False
                        await self.bus.publish(
                            VisualEvent(
                                type="person_left",
                                confidence=max(0.55, 1.0 - detection.confidence),
                                source="local_vision",
                                person_present=False,
                                summary="The nearby person is no longer visible.",
                                should_assistant_react=False,
                            )
                        )

    async def _hand_loop(self) -> None:
        sub = await self.bus.subscribe(HandObservationEvent, maxsize=16, drop_oldest=True, name="wave-observations")
        async with sub:
            while True:
                event = await sub.get()
                result = self.wave.update(
                    HandObservation(
                        timestamp=time.monotonic(),
                        wrist_x=event.wrist_x,
                        wrist_y=event.wrist_y,
                        confidence=event.confidence,
                        person_present=event.person_present,
                    )
                )
                if result.is_wave:
                    # High-confidence local gesture path: no image upload occurs here.
                    await self.bus.publish(
                        VisualEvent(
                            type="wave",
                            confidence=result.confidence,
                            source="local_vision",
                            person_present=event.person_present,
                            summary="A person appears to be waving toward the device.",
                            should_assistant_react=True,
                        )
                    )
                elif result.is_candidate:
                    await self.bus.publish(
                        LowConfidenceVisualCandidate(
                            frame_id=event.frame_id,
                            event_type="wave",
                            confidence=result.confidence,
                            summary="Hand motion could be a wave, but local temporal confidence is below threshold.",
                        )
                    )


class CloudEscalationService:
    """Escalate one useful RAM frame only for meaningful low-confidence local candidates."""

    def __init__(
        self,
        bus: EventBus,
        behavior: AppBehaviorConfig,
        frame_buffer: FrameBuffer,
        vision_client: Any,
    ) -> None:
        self.bus = bus
        self.behavior = behavior
        self.frame_buffer = frame_buffer
        self.vision_client = vision_client
        self._task: asyncio.Task[None] | None = None
        self._last_escalation = -1e9
        self.request_count = 0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="vision-cloud-escalation")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        sub = await self.bus.subscribe(LowConfidenceVisualCandidate, maxsize=4, name="vision-cloud-candidates")
        async with sub:
            while True:
                candidate = await sub.get()
                now = time.monotonic()
                if not candidate.eligible_for_cloud:
                    continue
                if now - self._last_escalation < self.behavior.vision.cloud_escalation_cooldown_sec:
                    continue
                record = self.frame_buffer.select_best(around_frame_id=candidate.frame_id, window_sec=0.8)
                if record is None:
                    continue
                self._last_escalation = now
                self.request_count += 1
                try:
                    result = await self.vision_client.analyze_frame(
                        record.frame,
                        question=(
                            "A meaningful local motion candidate exists, but classification confidence was low. "
                            f"Local candidate: {candidate.event_type}, confidence={candidate.confidence:.2f}. "
                            "Classify what probably happened. Do not manufacture certainty."
                        ),
                    )
                except Exception as exc:
                    LOG.warning("Gemini one-frame vision escalation failed: %s", exc)
                    continue
                await self.bus.publish(
                    VisualEvent(
                        type=result.event_type,
                        confidence=result.confidence,
                        source="gemini_vision",
                        person_present=result.event_type not in {"person_left", "unknown"},
                        summary=result.description,
                        should_assistant_react=result.should_assistant_react and result.confidence >= 0.45,
                    )
                )


class EventPolicyService:
    """Republish only filtered visual events on a dedicated consumer bus callback."""

    def __init__(self, bus: EventBus, state: StateMachine, policy: EventPolicy) -> None:
        self.bus = bus
        self.state = state
        self.policy = policy
        self._task: asyncio.Task[None] | None = None
        self._output_callbacks: list[Any] = []

    def add_callback(self, callback: Any) -> None:
        self._output_callbacks.append(callback)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="event-policy")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        # This service is intentionally callback-based instead of republishing VisualEvent,
        # which would recursively feed its own subscription. BrainService can be attached as
        # a callback in future; main currently gives policy to a bus bridge below.
        sub = await self.bus.subscribe(VisualEvent, maxsize=32, name="event-policy-raw")
        async with sub:
            while True:
                event = await sub.get()
                filtered = self.policy.apply(event, self.state.current)
                if filtered is None:
                    continue
                for callback in tuple(self._output_callbacks):
                    result = callback(filtered)
                    if asyncio.iscoroutine(result):
                        await result
