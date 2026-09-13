"""Typed event definitions shared by asynchronous BMO Pi services."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4


def _event_id() -> str:
    return uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True, kw_only=True)
class Event:
    """Base event carrying correlation metadata without retaining media."""

    event_id: str = field(default_factory=_event_id)
    timestamp: str = field(default_factory=_now_iso)
    turn_id: int | None = None


@dataclass(slots=True, kw_only=True)
class ShutdownRequested(Event):
    reason: str = "shutdown"


@dataclass(slots=True, kw_only=True)
class StateChanged(Event):
    previous: str
    current: str
    reason: str


@dataclass(slots=True, kw_only=True)
class MicAudioChunk(Event):
    pcm: bytes = field(repr=False)
    sample_rate: int
    channels: int = 1
    rms: float = 0.0


@dataclass(slots=True, kw_only=True)
class ProcessedAudioChunk(Event):
    pcm: bytes = field(repr=False)
    sample_rate: int
    channels: int = 1
    rms: float = 0.0
    transmit: bool = True


@dataclass(slots=True, kw_only=True)
class SpeechStarted(Event):
    rms: float = 0.0


@dataclass(slots=True, kw_only=True)
class SpeechEnded(Event):
    duration_ms: int = 0


@dataclass(slots=True, kw_only=True)
class TranscriptPartial(Event):
    text: str


@dataclass(slots=True, kw_only=True)
class TranscriptCommitted(Event):
    text: str


@dataclass(slots=True, kw_only=True)
class PushToTalkStarted(Event):
    source: str = "control"


@dataclass(slots=True, kw_only=True)
class PushToTalkEnded(Event):
    source: str = "control"


@dataclass(slots=True, kw_only=True)
class ListeningModeChanged(Event):
    mode: str


@dataclass(slots=True, kw_only=True)
class AgentTextChunk(Event):
    text: str


@dataclass(slots=True, kw_only=True)
class AgentTurnStarted(Event):
    prompt_summary: str = ""


@dataclass(slots=True, kw_only=True)
class AgentTurnFinished(Event):
    full_text: str = ""
    silent: bool = False


@dataclass(slots=True, kw_only=True)
class TTSStarted(Event):
    context_id: str


@dataclass(slots=True, kw_only=True)
class TTSAudioChunk(Event):
    pcm: bytes = field(repr=False)
    sample_rate: int
    context_id: str


@dataclass(slots=True, kw_only=True)
class TTSFinished(Event):
    context_id: str


@dataclass(slots=True, kw_only=True)
class TTSTurnFailed(Event):
    """Signal that speech synthesis for a turn cannot continue."""

    detail: str


@dataclass(slots=True, kw_only=True)
class PlaybackStarted(Event):
    sample_rate: int


@dataclass(slots=True, kw_only=True)
class PlaybackStopped(Event):
    interrupted: bool = False


@dataclass(slots=True, kw_only=True)
class BargeIn(Event):
    interrupted_turn_id: int
    new_turn_id: int


@dataclass(slots=True, kw_only=True)
class CameraFrame(Event):
    frame_id: str
    frame: Any = field(repr=False)
    width: int
    height: int
    sharpness: float = 0.0
    brightness: float = 0.0


@dataclass(slots=True, kw_only=True)
class MotionCandidate(Event):
    frame_id: str
    confidence: float
    changed_fraction: float
    brightness_delta: float


@dataclass(slots=True, kw_only=True)
class HandObservationEvent(Event):
    frame_id: str
    wrist_x: float
    wrist_y: float
    confidence: float
    person_present: bool = True


@dataclass(slots=True, kw_only=True)
class LowConfidenceVisualCandidate(Event):
    frame_id: str
    event_type: str
    confidence: float
    summary: str
    eligible_for_cloud: bool = True


@dataclass(slots=True, kw_only=True)
class VisualEvent(Event):
    type: str
    confidence: float
    source: str
    person_present: bool
    summary: str
    should_assistant_react: bool = False


@dataclass(slots=True, kw_only=True)
class OCRResultEvent(Event):
    text: str
    confidence: float
    source: str = "local_ocr"


class ControlAction(str, Enum):
    TOUCH_TAP = "touch_tap"
    TOUCH_DOUBLE = "touch_double"
    TOUCH_LONG = "touch_long"
    BUTTON_A = "button_a"
    BUTTON_B = "button_b"
    JOY_UP = "joy_up"
    JOY_DOWN = "joy_down"
    JOY_LEFT = "joy_left"
    JOY_RIGHT = "joy_right"
    JOY_PRESS = "joy_press"


@dataclass(slots=True, kw_only=True)
class ControlEvent(Event):
    action: ControlAction
    pressed: bool = True
    duration_ms: int = 0


@dataclass(slots=True, kw_only=True)
class ModeChanged(Event):
    mode: str


@dataclass(slots=True, kw_only=True)
class ExpressionRequested(Event):
    expression: str
    duration_sec: float | None = None


@dataclass(slots=True, kw_only=True)
class MoodChanged(Event):
    mood: str
    valence: float
    arousal: float


@dataclass(slots=True, kw_only=True)
class GameStateUpdated(Event):
    active: bool
    player_x: int = 64
    star_x: int = 64
    star_y: int = 0
    score: int = 0
    lives: int = 3
    message: str = ""


@dataclass(slots=True, kw_only=True)
class MemoryStored(Event):
    memory_id: int
    summary: str


@dataclass(slots=True, kw_only=True)
class NetworkLost(Event):
    service: str
    detail: str = ""


@dataclass(slots=True, kw_only=True)
class NetworkRestored(Event):
    service: str


@dataclass(slots=True, kw_only=True)
class PeripheralFailed(Event):
    peripheral: str
    detail: str
    fatal: bool = False


@dataclass(slots=True, kw_only=True)
class HealthSnapshot(Event):
    cpu_percent: float
    memory_percent: float
    cpu_temp_c: float | None
    throttled_hex: str | None
    camera_fps: float
    vision_latency_ms: float
    audio_queue_depth: int
    tts_queue_depth: int
