"""Async application composition root for the Raspberry Pi companion."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from typing import Any

from .agent.adk_agent import ADKAgentRuntime
from .agent.brain import BrainService, MockBrainBackend
from .agent.memory import MemoryStore
from .agent.tools import ToolRegistry
from .audio.capture import AudioCaptureService, WavFixtureCaptureService
from .audio.elevenlabs_tts import ElevenLabsRealtimeClient, MockTTSService, TTSService
from .audio.gemini_transcription import GeminiLiveTranscriptionService, MockTranscriptionService
from .audio.interruption import InterruptionService
from .audio.playback import AudioPlaybackService, MemoryPlaybackService
from .audio.preprocessing import AudioPreprocessorService
from .audio.vad import VADService
from .config import Settings, load_settings
from .event_bus import EventBus
from .events import ExpressionRequested, PeripheralFailed, ShutdownRequested
from .hardware.oled import ConsoleOLEDDevice, create_oled_device
from .hardware.touch import GPIOControlsService, KeyboardControlsService
from .health import HealthService
from .latency import LatencyTracker
from .logging_config import configure_logging
from .state import StateEventService, StateMachine, TurnManager
from .ui.animator import OLEDService
from .ui.game import GameService
from .ui.modes import ModeController
from .ui.mood import MoodEngine
from .vision.camera import CameraService, FixtureCameraService
from .vision.event_classifier import EventPolicy
from .vision.frame_buffer import FrameBuffer
from .vision.gemini_vision import GeminiVisionClient, MockVisionClient
from .vision.hand_tracker import MediaPipeHandTracker
from .vision.motion import MotionDetector
from .vision.object_detector import OpenCVPersonDetector
from .vision.ocr import TesseractOCR
from .vision.pipeline import CloudEscalationService, LocalVisionService, MotionService

LOG = logging.getLogger(__name__)


class Application:
    """Build and supervise modular services while tolerating optional peripheral failure."""

    def __init__(self, settings: Settings, args: argparse.Namespace) -> None:
        self.settings = settings
        self.args = args
        self.bus = EventBus()
        self.state = StateMachine(self.bus)
        self.turns = TurnManager()
        self.latency = LatencyTracker()
        self._services: list[tuple[str, Any, bool]] = []
        self._started: list[tuple[str, Any]] = []
        self._shutdown = asyncio.Event()

    async def build(self) -> None:
        root = self.settings.project_root
        behavior = self.settings.behavior
        hardware = self.settings.hardware
        secrets = self.settings.secrets
        profile = behavior.performance.active
        simulator_enabled = bool(getattr(self.args, "simulator", False))
        simulator_bridge: Any = None
        simulator_controls_cls: Any = None
        simulator_capture_cls: Any = None
        simulator_camera_cls: Any = None
        simulator_oled_cls: Any = None
        simulator_playback_cls: Any = None
        if simulator_enabled:
            from .simulator import (
                SimulatorAudioCaptureService,
                SimulatorBridgeService,
                SimulatorCameraService,
                SimulatorControlsService,
                SimulatorOLEDDevice,
                SimulatorPlaybackService,
            )

            simulator_controls_cls = SimulatorControlsService
            simulator_capture_cls = SimulatorAudioCaptureService
            simulator_camera_cls = SimulatorCameraService
            simulator_oled_cls = SimulatorOLEDDevice
            simulator_playback_cls = SimulatorPlaybackService
            simulator_bridge = SimulatorBridgeService(
                self.bus,
                self.state,
                self.turns,
                root / "simulator",
                host=getattr(self.args, "simulator_host", "127.0.0.1"),
                port=getattr(self.args, "simulator_port", 8765),
            )

        memory = MemoryStore(
            root / behavior.memory.database_path,
            root / behavior.memory.remembered_events_path,
            recent_turn_limit=behavior.memory.recent_turn_limit,
            enabled=behavior.memory.enabled,
        )
        await memory.initialize()

        max_frames = max(4, int(profile.camera_fps * behavior.vision.frame_buffer_seconds))
        frame_buffer = FrameBuffer(max_frames)
        event_policy = EventPolicy(behavior.event_policy)
        ocr = TesseractOCR(behavior.vision.ocr_confidence_threshold)

        mock_cloud = bool(self.args.mock_cloud or secrets.mock_cloud)
        vision_client: Any = None
        if mock_cloud:
            vision_client = MockVisionClient()
        elif secrets.gemini_api_key:
            vision_client = GeminiVisionClient(secrets.gemini_api_key, behavior.gemini, behavior.vision)

        if simulator_enabled:
            camera: Any = simulator_camera_cls(self.bus, frame_buffer)
        elif self.args.camera_fixtures:
            camera = FixtureCameraService(self.bus, frame_buffer, Path(self.args.camera_fixtures), profile.camera_fps)
        else:
            camera = CameraService(self.bus, hardware.camera, profile, frame_buffer)

        hand_tracker = MediaPipeHandTracker(
            self.bus,
            root / behavior.vision.hand_landmarker_model,
            behavior.vision.wave.min_tracking_confidence,
        )
        person_detector = OpenCVPersonDetector() if profile.person_detector_enabled else None
        motion_service = MotionService(self.bus, MotionDetector(behavior.vision.motion))
        local_vision = LocalVisionService(
            self.bus,
            behavior,
            frame_buffer,
            hand_tracker,
            person_detector,
        )
        cloud_escalation = (
            CloudEscalationService(self.bus, behavior, frame_buffer, vision_client) if vision_client is not None else None
        )

        tools = ToolRegistry(
            self.bus,
            self.state,
            memory,
            camera=camera,
            frame_buffer=frame_buffer,
            vision_client=vision_client,
            ocr=ocr,
            event_policy=event_policy,
        )
        if mock_cloud:
            backend: Any = MockBrainBackend(self.args.mock_response)
        elif secrets.gemini_api_key:
            backend = ADKAgentRuntime(behavior.gemini, secrets.gemini_api_key, tools)
        else:
            backend = None

        mood = MoodEngine(self.bus, behavior.personality)
        brain = (
            BrainService(
                self.bus,
                self.state,
                self.turns,
                memory,
                backend,
                behavior,
                self.latency,
                mood_provider=mood,
                event_policy=event_policy,
            )
            if backend is not None
            else None
        )

        preprocess = AudioPreprocessorService(self.bus, self.state, behavior.audio)
        vad = VADService(self.bus, self.state, self.turns, behavior.audio, self.latency)
        interruption = InterruptionService(self.bus, self.state)
        if simulator_enabled:
            capture: Any = simulator_capture_cls(self.bus)
        elif self.args.wav:
            capture = WavFixtureCaptureService(self.bus, Path(self.args.wav), behavior.audio.chunk_ms, self.args.loop_wav)
        else:
            capture = AudioCaptureService(self.bus, hardware.microphone, behavior.audio)

        if mock_cloud:
            stt: Any = MockTranscriptionService(self.bus, self.turns, self.args.mock_transcript, self.latency)
            tts: Any = MockTTSService(self.bus, self.turns, behavior.elevenlabs.sample_rate)
        else:
            stt = GeminiLiveTranscriptionService(
                self.bus,
                self.turns,
                behavior.gemini,
                behavior.audio,
                secrets.gemini_api_key,
                self.latency,
            )
            if secrets.elevenlabs_api_key and secrets.elevenlabs_voice_id:
                eleven_client = ElevenLabsRealtimeClient(
                    self.bus,
                    self.turns,
                    behavior.elevenlabs,
                    behavior.tts,
                    secrets.elevenlabs_api_key,
                    secrets.elevenlabs_voice_id,
                    self.latency,
                )
                tts = TTSService(self.bus, self.turns, eleven_client, behavior.tts)
            else:
                tts = None

        if simulator_enabled:
            playback: Any = simulator_playback_cls(
                self.bus, self.turns, self.latency, simulator_bridge, mock_cloud=mock_cloud
            )
        elif self.args.dev or self.args.no_playback:
            playback = MemoryPlaybackService(self.bus, self.turns)
        else:
            playback = AudioPlaybackService(
                self.bus,
                hardware.speaker,
                behavior.tts,
                self.turns,
                self.latency,
                fallback_duck_gain=behavior.audio.speaker_duck_gain if behavior.audio.aec_mode == "fallback" else 1.0,
                aec_device_hint=(behavior.audio.echo_cancel_sink_hint if behavior.audio.aec_mode == "pipewire" else None),
            )

        oled_config = hardware.oled.model_copy()
        if simulator_enabled:
            oled_config.driver = "console"
            oled_device: Any = simulator_oled_cls(oled_config, simulator_bridge)
        else:
            if self.args.oled_console or self.args.dev:
                oled_config.driver = "console"
            oled_device = ConsoleOLEDDevice(oled_config) if oled_config.driver == "console" else None
            if oled_device is None:
                try:
                    oled_device = create_oled_device(oled_config)
                except Exception as exc:
                    LOG.warning("OLED initialization failed; using console fallback: %s", exc)
                    oled_config.driver = "console"
                    oled_device = ConsoleOLEDDevice(oled_config)
        oled = OLEDService(self.bus, oled_device, behavior.oled, fps=profile.oled_fps)

        if simulator_enabled:
            controls: Any = simulator_controls_cls(self.bus, behavior.controls)
        elif self.args.keyboard_controls or self.args.dev:
            controls = KeyboardControlsService(self.bus)
        else:
            controls = GPIOControlsService(self.bus, hardware, behavior.controls)
        if simulator_enabled:
            simulator_bridge.bind_adapters(controls=controls, capture=capture, camera=camera)
        modes = ModeController(self.bus, self.state, self.turns, behavior.controls)
        game = GameService(self.bus, hardware.oled.width, hardware.oled.height)
        state_events = StateEventService(self.bus, self.state, expect_playback=tts is not None)

        health = HealthService(
            self.bus,
            behavior.health,
            camera_fps=lambda: float(getattr(camera, "measured_fps", 0.0)),
            vision_latency_ms=lambda: max(motion_service.last_latency_ms, local_vision.last_inference_latency_ms),
            audio_queue_depth=lambda: int(getattr(capture, "queue_depth", 0)),
            tts_queue_depth=lambda: int(getattr(playback, "queue_depth", 0)),
        )

        # Consumers start before producers so first frames/audio cannot race subscriptions.
        self._services = [
            ("oled", oled, False),
            ("state", state_events, True),
            ("mood", mood, False),
            ("modes", modes, False),
            ("game", game, False),
            ("preprocess", preprocess, True),
            ("vad", vad, True),
            ("interruption", interruption, True),
            ("stt", stt, False),
            ("brain", brain, False),
            ("tts", tts, False),
            ("playback", playback, False),
            ("motion", motion_service, False),
            ("local_vision", local_vision, False),
            ("cloud_vision", cloud_escalation, False),
            ("controls", controls, False),
            ("health", health, False),
            ("simulator", simulator_bridge, simulator_enabled),
            ("camera", camera, False),
            ("capture", capture, False),
        ]

    async def start(self) -> None:
        for name, service, required in self._services:
            if service is None:
                LOG.warning("service %s disabled by configuration", name)
                continue
            try:
                await service.start()
                self._started.append((name, service))
                LOG.info("service started: %s", name)
            except Exception as exc:
                LOG.exception("service failed to start: %s", name)
                await self.bus.publish(PeripheralFailed(peripheral=name, detail=str(exc), fatal=required))
                if required:
                    raise
        if not self.settings.secrets.gemini_api_key and not (self.args.mock_cloud or self.settings.secrets.mock_cloud):
            LOG.warning("GEMINI_API_KEY is not configured; local hardware remains active but cloud conversation is unavailable")
        if not self.settings.secrets.elevenlabs_api_key and not (self.args.mock_cloud or self.settings.secrets.mock_cloud):
            LOG.warning("ELEVENLABS_API_KEY is not configured; TTS is disabled")

    async def stop(self) -> None:
        for name, service in reversed(self._started):
            with contextlib.suppress(Exception):
                await service.stop()
            LOG.info("service stopped: %s", name)
        self._started.clear()

    async def run(self) -> None:
        await self.build()
        await self.start()
        shutdown_sub = await self.bus.subscribe(ShutdownRequested, maxsize=2, name="main-shutdown")
        loop = asyncio.get_running_loop()
        for signame in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signame, self._shutdown.set)
        waiter = asyncio.create_task(shutdown_sub.get(), name="shutdown-event")
        stopper = asyncio.create_task(self._shutdown.wait(), name="shutdown-signal")
        try:
            await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            stopper.cancel()
            shutdown_sub.close()
            await self.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the BMO-inspired Raspberry Pi companion")
    parser.add_argument("--config-dir", type=Path, default=None, help="directory containing default.yaml and hardware.yaml")
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    parser.add_argument("--dev", action="store_true", help="hardware-free console-oriented development mode")
    parser.add_argument("--mock-cloud", action="store_true", help="use explicit Gemini/STT/TTS mocks; never for production")
    parser.add_argument("--mock-transcript", default="Hey, what are you doing?", help="transcript emitted by --mock-cloud after fixture speech")
    parser.add_argument("--mock-response", default="Tiny robot business. Very important. Probably.", help="mock agent response in dev tests")
    parser.add_argument("--wav", type=Path, help="stream a mono PCM16 WAV instead of the microphone")
    parser.add_argument("--loop-wav", action="store_true")
    parser.add_argument("--camera-fixtures", type=Path, help="loop PNG/JPEG fixtures instead of Picamera2")
    parser.add_argument("--oled-console", action="store_true")
    parser.add_argument("--keyboard-controls", action="store_true")
    parser.add_argument("--no-playback", action="store_true", help="consume TTS PCM without opening a speaker device")
    parser.add_argument(
        "--simulator",
        action="store_true",
        help="replace physical camera/mic/controls/speaker/OLED with the browser simulator",
    )
    parser.add_argument("--simulator-host", default="127.0.0.1", help="simulator HTTP/WebSocket bind host")
    parser.add_argument("--simulator-port", type=int, default=8765, help="simulator HTTP port; WebSocket uses port+1")
    return parser


async def async_main(args: argparse.Namespace) -> None:
    project_root = Path(__file__).resolve().parents[2]
    config_dir = (args.config_dir or project_root / "config").resolve()
    settings = load_settings(config_dir, project_root)
    configure_logging(project_root / "logs", settings.secrets.log_level, args.debug)
    LOG.info(
        "starting Beemo model=%s stt=%s performance=%s",
        settings.behavior.gemini.brain_model,
        settings.behavior.gemini.transcription_model,
        settings.behavior.performance.mode,
    )
    app = Application(settings, args)
    await app.run()


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
