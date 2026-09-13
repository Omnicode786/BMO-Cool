"""Narrow, validated ADK tools for hardware, vision, mode, and local memory."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..event_bus import EventBus
from ..events import ExpressionRequested, ListeningModeChanged
from ..state import AppState, StateMachine
from .memory import MemoryStore


class ExpressionArgs(BaseModel):
    expression: Literal[
        "booting", "idle", "listening", "thinking", "speaking", "happy", "curious",
        "annoyed", "angry", "surprised", "proud", "sleepy", "offline", "error"
    ]


class ListeningModeArgs(BaseModel):
    mode: Literal["always", "vad", "touch_to_talk"]


class CaptureArgs(BaseModel):
    reason: str = Field(min_length=1, max_length=160)


class AnalyzeFrameArgs(BaseModel):
    frame_reference: str = Field(min_length=3, max_length=100)
    question: str = Field(min_length=1, max_length=500)


class MemoryArgs(BaseModel):
    fact: str = Field(min_length=1, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, tags: list[str]) -> list[str]:
        return [tag.strip().lower()[:40] for tag in tags if tag.strip()]


class SearchMemoryArgs(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class ToolRegistry:
    """Bind agent-visible operations to constrained local implementations."""

    def __init__(
        self,
        bus: EventBus,
        state: StateMachine,
        memory: MemoryStore,
        *,
        camera: Any = None,
        frame_buffer: Any = None,
        vision_client: Any = None,
        ocr: Any = None,
        event_policy: Any = None,
    ) -> None:
        self.bus = bus
        self.state = state
        self.memory = memory
        self.camera = camera
        self.frame_buffer = frame_buffer
        self.vision_client = vision_client
        self.ocr = ocr
        self.event_policy = event_policy

    async def get_device_status(self) -> dict[str, Any]:
        """Return safe high-level device status; never returns keys or raw GPIO access."""
        return {
            "state": self.state.current.value,
            "camera_available": bool(self.camera and getattr(self.camera, "available", False)),
            "vision_available": self.vision_client is not None,
            "memory_enabled": self.memory.enabled,
        }

    async def get_latest_visual_event(self) -> dict[str, Any]:
        """Return the most recent filtered visual event summary, if one exists."""
        event = getattr(self.event_policy, "latest_event", None) if self.event_policy else None
        if event is None:
            return {"available": False}
        return {
            "available": True,
            "type": event.type,
            "confidence": round(float(event.confidence), 3),
            "source": event.source,
            "summary": event.summary,
            "timestamp": event.timestamp,
        }

    async def capture_camera_frame(self, reason: str) -> dict[str, Any]:
        """Capture one current high-quality frame for an explicit visual task."""
        args = CaptureArgs(reason=reason)
        if not self.camera or not getattr(self.camera, "available", False):
            return {"ok": False, "error": "camera unavailable"}
        record = await self.camera.capture_snapshot(args.reason)
        return {"ok": True, "frame_reference": record.frame_id, "captured_at": record.timestamp}

    async def analyze_camera_frame(self, frame_reference: str, question: str) -> dict[str, Any]:
        """Analyze one selected frame with Gemini vision; no continuous video upload."""
        args = AnalyzeFrameArgs(frame_reference=frame_reference, question=question)
        if not self.frame_buffer or not self.vision_client:
            return {"ok": False, "error": "vision analysis unavailable"}
        record = self.frame_buffer.get(args.frame_reference)
        if record is None:
            return {"ok": False, "error": "frame reference expired from RAM buffer"}
        result = await self.vision_client.analyze_frame(record.frame, question=args.question)
        return {"ok": True, **result.model_dump()}

    async def read_visible_text(self, reason: str = "user asked to read visible text") -> dict[str, Any]:
        """Capture one frame, run local OCR first, then cloud vision only when OCR is weak."""
        args = CaptureArgs(reason=reason)
        if not self.camera or not getattr(self.camera, "available", False) or self.ocr is None:
            return {"ok": False, "error": "camera/OCR unavailable"}
        record = await self.camera.capture_snapshot(args.reason)
        result = await self.ocr.read(record.frame)
        if result.confidence >= self.ocr.confidence_threshold and result.text.strip():
            return {"ok": True, "source": "local_ocr", "text": result.text, "confidence": result.confidence}
        if self.vision_client is None:
            return {
                "ok": bool(result.text.strip()),
                "source": "local_ocr_low_confidence",
                "text": result.text,
                "confidence": result.confidence,
            }
        cloud = await self.vision_client.analyze_frame(
            record.frame,
            question="Read the visible text accurately. If text is unclear, say so rather than guessing.",
        )
        return {"ok": True, "source": "gemini_vision", **cloud.model_dump()}

    async def set_oled_expression(self, expression: str) -> dict[str, Any]:
        """Request one validated OLED expression."""
        args = ExpressionArgs(expression=expression)
        await self.bus.publish(ExpressionRequested(expression=args.expression))
        return {"ok": True, "expression": args.expression}

    async def set_listening_mode(self, mode: str) -> dict[str, Any]:
        """Switch between always-on, hybrid VAD, and touch-to-talk listening."""
        args = ListeningModeArgs(mode=mode)
        await self.bus.publish(ListeningModeChanged(mode=args.mode))
        return {"ok": True, "mode": args.mode}

    async def enter_sleep_mode(self) -> dict[str, Any]:
        """Enter low-activity sleep mode without powering off the Pi."""
        await self.state.transition(AppState.SLEEPING, "agent requested sleep", force=True)
        return {"ok": True, "state": self.state.current.value}

    async def wake_device(self) -> dict[str, Any]:
        """Wake from sleep to idle state."""
        await self.state.transition(AppState.IDLE, "agent requested wake", force=True)
        return {"ok": True, "state": self.state.current.value}

    async def remember_fact(self, fact: str, tags: list[str] | None = None) -> dict[str, Any]:
        """Persist one user-requested durable fact locally."""
        args = MemoryArgs(fact=fact, tags=tags or [])
        if not self.memory.enabled:
            return {"ok": False, "error": "local memory is disabled in config/default.yaml"}
        item = await self.memory.remember(args.fact, kind="explicit", tags=args.tags)
        return {"ok": True, "memory_id": item.id, "stored": item.text}

    async def search_memories(self, query: str) -> dict[str, Any]:
        """Search only local user-approved/structured memories."""
        args = SearchMemoryArgs(query=query)
        items = await self.memory.search(args.query)
        return {
            "ok": True,
            "results": [
                {"id": item.id, "kind": item.kind, "text": item.text, "created_at": item.created_at, "tags": item.tags}
                for item in items
            ],
        }

    def adk_functions(self) -> list[Any]:
        """Return only explicitly allowed tool functions to ADK."""
        return [
            self.get_device_status,
            self.get_latest_visual_event,
            self.capture_camera_frame,
            self.analyze_camera_frame,
            self.read_visible_text,
            self.set_oled_expression,
            self.set_listening_mode,
            self.enter_sleep_mode,
            self.wake_device,
            self.remember_fact,
            self.search_memories,
        ]
