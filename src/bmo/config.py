"""YAML + environment configuration loading and validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryConfig(StrictModel):
    connect_timeout_sec: float = Field(10.0, gt=0)
    read_timeout_sec: float = Field(30.0, gt=0)
    initial_backoff_sec: float = Field(0.5, gt=0)
    max_backoff_sec: float = Field(20.0, gt=0)
    jitter_ratio: float = Field(0.2, ge=0, le=1)


class AudioConfig(StrictModel):
    chunk_ms: int = Field(30, ge=20, le=40)
    listening_mode: Literal["always", "vad", "touch_to_talk"] = "vad"
    aec_mode: Literal["pipewire", "fallback", "none"] = "pipewire"
    echo_cancel_source_hint: str = "echo cancel source"
    echo_cancel_sink_hint: str = "echo cancel sink"
    queue_max_chunks: int = Field(64, ge=8, le=512)
    local_vad_threshold: float = Field(0.018, gt=0, lt=1)
    vad_threshold_while_speaking_multiplier: float = Field(1.8, ge=1.0, le=5.0)
    vad_start_frames: int = Field(2, ge=1, le=20)
    vad_end_frames: int = Field(10, ge=2, le=100)
    feedback_suppression_rms: float = Field(0.25, gt=0, le=1)
    speaker_duck_gain: float = Field(0.82, gt=0, le=1)


class TTSConfig(StrictModel):
    min_chunk_chars: int = Field(30, ge=5, le=200)
    max_chunk_chars: int = Field(130, ge=30, le=500)
    inactivity_timeout_sec: int = Field(120, ge=20, le=180)
    auto_mode: bool = False
    playback_slice_ms: int = Field(20, ge=10, le=60)
    queue_max_chunks: int = Field(160, ge=20, le=1000)


class GeminiConfig(StrictModel):
    brain_model: str = "gemini-3.7-flash"
    transcription_model: str = "gemini-3.5-transcribe-live"
    max_llm_calls: int = Field(8, ge=1, le=64)
    proactive_max_llm_calls: int = Field(3, ge=1, le=16)
    enable_session_resumption: bool = True
    enable_context_compression: bool = True
    transcription_language_codes: list[str] = Field(default_factory=list)
    retry: RetryConfig = Field(default_factory=RetryConfig)


class ElevenLabsConfig(StrictModel):
    model_id: str = "eleven_flash_v2_5"
    output_format: str = "pcm_24000"
    retry: RetryConfig = Field(default_factory=RetryConfig)

    @field_validator("output_format")
    @classmethod
    def validate_pcm_output(cls, value: str) -> str:
        if not value.startswith("pcm_"):
            raise ValueError("normal conversational playback must use a PCM ElevenLabs output format")
        return value

    @property
    def sample_rate(self) -> int:
        try:
            return int(self.output_format.split("_", 1)[1])
        except (IndexError, ValueError) as exc:
            raise ValueError(f"unsupported PCM output format: {self.output_format}") from exc


class PerformanceProfile(StrictModel):
    camera_resolution: tuple[int, int]
    camera_fps: int = Field(ge=1, le=30)
    vision_every_n_motion_frames: int = Field(ge=1, le=20)
    oled_fps: int = Field(ge=1, le=30)
    person_detector_enabled: bool = True


class PerformanceConfig(StrictModel):
    mode: Literal["lite", "normal"] = "normal"
    lite: PerformanceProfile
    normal: PerformanceProfile

    @property
    def active(self) -> PerformanceProfile:
        return self.lite if self.mode == "lite" else self.normal


class MotionConfig(StrictModel):
    threshold: float = Field(0.08, ge=0.001, le=1)
    pixel_delta_threshold: int = Field(18, ge=1, le=255)
    min_changed_area: float = Field(0.025, ge=0, le=1)
    persistence_frames: int = Field(3, ge=1, le=30)
    debounce_sec: float = Field(0.25, ge=0, le=10)
    cooldown_sec: float = Field(1.0, ge=0, le=60)
    lighting_change_mean_delta: float = Field(20.0, ge=0, le=255)
    lighting_change_fraction: float = Field(0.65, ge=0, le=1)
    exposure_reject_sec: float = Field(0.8, ge=0, le=10)


class WaveConfig(StrictModel):
    window_sec: float = Field(2.2, gt=0.3, le=5)
    min_samples: int = Field(6, ge=4, le=60)
    min_reversals: int = Field(2, ge=1, le=8)
    min_amplitude: float = Field(0.16, ge=0.03, le=0.8)
    min_tracking_confidence: float = Field(0.5, ge=0, le=1)
    max_wrist_y: float = Field(0.78, ge=0.1, le=1)
    raised_ratio: float = Field(0.7, ge=0.1, le=1)
    local_confidence_threshold: float = Field(0.60, ge=0, le=1)
    low_confidence_floor: float = Field(0.30, ge=0, le=1)
    cooldown_sec: float = Field(20.0, ge=1, le=300)

    @model_validator(mode="after")
    def thresholds_are_ordered(self) -> "WaveConfig":
        if self.low_confidence_floor >= self.local_confidence_threshold:
            raise ValueError("wave low_confidence_floor must be below local_confidence_threshold")
        return self


class VisionConfig(StrictModel):
    motion: MotionConfig = Field(default_factory=MotionConfig)
    wave: WaveConfig = Field(default_factory=WaveConfig)
    frame_buffer_seconds: float = Field(2.5, ge=0.5, le=10)
    cloud_max_width: int = Field(960, ge=320, le=2048)
    jpeg_quality: int = Field(78, ge=40, le=95)
    cloud_escalation_cooldown_sec: float = Field(4.0, ge=0, le=120)
    ocr_confidence_threshold: float = Field(0.62, ge=0, le=1)
    hand_landmarker_model: str = "models/hand_landmarker.task"
    person_detection_stride: int = Field(2, ge=1, le=30)


class EventPolicyConfig(StrictModel):
    dedupe_window_sec: float = Field(6.0, ge=0, le=120)
    proactive_min_priority: int = Field(70, ge=0, le=100)
    visual_reaction_cooldown_sec: float = Field(8.0, ge=0, le=300)
    wave_priority: int = Field(90, ge=0, le=100)
    person_arrived_priority: int = Field(65, ge=0, le=100)
    object_shown_priority: int = Field(75, ge=0, le=100)
    scene_change_priority: int = Field(35, ge=0, le=100)


class MemoryConfig(StrictModel):
    enabled: bool = True
    explicit_preferences_enabled: bool = False
    database_path: str = "data/bmo_memory.sqlite3"
    remembered_events_path: str = "data/remembered_events.jsonl"
    recent_turn_limit: int = Field(20, ge=2, le=200)
    retrieval_limit: int = Field(6, ge=1, le=20)


class PersonalityConfig(StrictModel):
    name: str = "Beemo"
    roast_level: int = Field(2, ge=0, le=5)
    default_mood: str = "happy"
    mood_decay_sec: float = Field(45.0, ge=5, le=600)
    proactive_chattiness: float = Field(0.35, ge=0, le=1)


class ControlTimingConfig(StrictModel):
    double_tap_ms: int = Field(400, ge=150, le=1000)
    long_press_ms: int = Field(1200, ge=500, le=5000)
    debounce_ms: int = Field(35, ge=5, le=500)


class ControlsConfig(StrictModel):
    timing: ControlTimingConfig = Field(default_factory=ControlTimingConfig)
    modes: list[str] = Field(default_factory=lambda: ["companion", "game", "status", "sleep"])
    keyboard_enabled_in_dev: bool = True


class OLEDBehaviorConfig(StrictModel):
    expression_hold_sec: float = Field(1.2, ge=0.1, le=30)
    blink_min_sec: float = Field(2.0, ge=0.5, le=20)
    blink_max_sec: float = Field(5.0, ge=0.5, le=30)


class HealthConfig(StrictModel):
    interval_sec: float = Field(10.0, ge=1, le=300)
    internet_host: str = "generativelanguage.googleapis.com"
    internet_port: int = 443
    network_failure_threshold: int = Field(2, ge=1, le=20)


class AppBehaviorConfig(StrictModel):
    audio: AudioConfig = Field(default_factory=AudioConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    elevenlabs: ElevenLabsConfig = Field(default_factory=ElevenLabsConfig)
    performance: PerformanceConfig
    vision: VisionConfig = Field(default_factory=VisionConfig)
    event_policy: EventPolicyConfig = Field(default_factory=EventPolicyConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    personality: PersonalityConfig = Field(default_factory=PersonalityConfig)
    controls: ControlsConfig = Field(default_factory=ControlsConfig)
    oled: OLEDBehaviorConfig = Field(default_factory=OLEDBehaviorConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)


class CameraHardware(StrictModel):
    enabled: bool = True
    resolution: tuple[int, int] = (640, 360)
    snapshot_resolution: tuple[int, int] = (1280, 720)
    fps: int = Field(8, ge=1, le=30)
    device: int = 0


class AudioHardware(StrictModel):
    device: str | int | None = None
    sample_rate: int
    channels: int = Field(1, ge=1, le=2)


class OLEDHardware(StrictModel):
    enabled: bool = True
    driver: Literal["ssd1306", "sh1106", "console"] = "ssd1306"
    i2c_bus: int = Field(1, ge=0, le=10)
    i2c_address: int = Field(0x3C, ge=0x03, le=0x77)
    width: int = Field(128, ge=32, le=512)
    height: int = Field(64, ge=16, le=256)


class DigitalInputHardware(StrictModel):
    enabled: bool = True
    driver: Literal["gpiozero", "keyboard"] = "gpiozero"
    gpio_pin: int = Field(ge=0, le=27)
    active_high: bool = True
    pull_up: bool | None = None


class JoystickHardware(StrictModel):
    enabled: bool = True
    driver: Literal["gpiozero", "keyboard"] = "gpiozero"
    up_pin: int = Field(ge=0, le=27)
    down_pin: int = Field(ge=0, le=27)
    left_pin: int = Field(ge=0, le=27)
    right_pin: int = Field(ge=0, le=27)
    press_pin: int = Field(ge=0, le=27)
    active_high: bool = False


class HardwareConfig(StrictModel):
    camera: CameraHardware = Field(default_factory=CameraHardware)
    microphone: AudioHardware = Field(default_factory=lambda: AudioHardware(sample_rate=16000))
    speaker: AudioHardware = Field(default_factory=lambda: AudioHardware(sample_rate=24000))
    oled: OLEDHardware = Field(default_factory=OLEDHardware)
    touch: DigitalInputHardware = Field(default_factory=lambda: DigitalInputHardware(gpio_pin=17))
    button_a: DigitalInputHardware = Field(default_factory=lambda: DigitalInputHardware(gpio_pin=22, active_high=False))
    button_b: DigitalInputHardware = Field(default_factory=lambda: DigitalInputHardware(gpio_pin=23, active_high=False))
    joystick: JoystickHardware = Field(
        default_factory=lambda: JoystickHardware(up_pin=5, down_pin=6, left_pin=13, right_pin=19, press_pin=26)
    )

    @model_validator(mode="after")
    def pins_are_unique(self) -> "HardwareConfig":
        pins: list[tuple[str, int]] = []
        for name in ("touch", "button_a", "button_b"):
            cfg = getattr(self, name)
            if cfg.enabled and cfg.driver == "gpiozero":
                pins.append((name, cfg.gpio_pin))
        if self.joystick.enabled and self.joystick.driver == "gpiozero":
            pins.extend(
                (f"joystick.{direction}", getattr(self.joystick, f"{direction}_pin"))
                for direction in ("up", "down", "left", "right", "press")
            )
        seen: dict[int, str] = {}
        for name, pin in pins:
            if pin in {2, 3}:
                raise ValueError(f"{name} uses GPIO{pin}, reserved for I2C OLED in the reference design")
            if pin in seen:
                raise ValueError(f"GPIO{pin} assigned to both {seen[pin]} and {name}")
            seen[pin] = name
        return self


class SecretsConfig(StrictModel):
    gemini_api_key: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    save_debug_media: bool = False
    log_level: str = "INFO"
    mock_cloud: bool = False

    def configured(self) -> dict[str, bool]:
        return {
            "gemini": bool(self.gemini_api_key),
            "elevenlabs": bool(self.elevenlabs_api_key and self.elevenlabs_voice_id),
        }


class Settings(StrictModel):
    behavior: AppBehaviorConfig
    hardware: HardwareConfig
    secrets: SecretsConfig
    project_root: Path


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def _parse_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without overwriting exported environment."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def load_settings(config_dir: Path, project_root: Path | None = None, env_file: Path | None = None) -> Settings:
    """Load validated settings from default.yaml, hardware.yaml, and environment."""
    project_root = (project_root or config_dir.parent).resolve()
    _parse_env_file(env_file or project_root / ".env")

    behavior_data = _read_yaml(config_dir / "default.yaml")
    hardware_data = _read_yaml(config_dir / "hardware.yaml")

    # Credentials and API-selectable model IDs live in environment variables.
    behavior_data.setdefault("gemini", {})["brain_model"] = os.getenv(
        "GEMINI_BRAIN_MODEL", behavior_data.get("gemini", {}).get("brain_model", "gemini-3.7-flash")
    )
    behavior_data.setdefault("gemini", {})["transcription_model"] = os.getenv(
        "GEMINI_TRANSCRIBE_MODEL",
        behavior_data.get("gemini", {}).get("transcription_model", "gemini-3.5-transcribe-live"),
    )
    behavior_data.setdefault("elevenlabs", {})["model_id"] = os.getenv(
        "ELEVENLABS_MODEL_ID", behavior_data.get("elevenlabs", {}).get("model_id", "eleven_flash_v2_5")
    )
    behavior_data.setdefault("elevenlabs", {})["output_format"] = os.getenv(
        "ELEVENLABS_OUTPUT_FORMAT", behavior_data.get("elevenlabs", {}).get("output_format", "pcm_24000")
    )
    perf_mode = os.getenv("BMO_PERFORMANCE_MODE")
    if perf_mode:
        behavior_data.setdefault("performance", {})["mode"] = perf_mode

    secrets = SecretsConfig(
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", ""),
        elevenlabs_voice_id=os.getenv("ELEVENLABS_VOICE_ID", ""),
        save_debug_media=os.getenv("SAVE_DEBUG_MEDIA", "false").lower() in {"1", "true", "yes", "on"},
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        mock_cloud=os.getenv("BMO_MOCK_CLOUD", "false").lower() in {"1", "true", "yes", "on"},
    )
    behavior = AppBehaviorConfig.model_validate(behavior_data)
    hardware = HardwareConfig.model_validate(hardware_data)

    # Keep the speaker format aligned with ElevenLabs PCM output unless explicitly changed later.
    if hardware.speaker.sample_rate != behavior.elevenlabs.sample_rate:
        hardware.speaker.sample_rate = behavior.elevenlabs.sample_rate

    return Settings(behavior=behavior, hardware=hardware, secrets=secrets, project_root=project_root)
