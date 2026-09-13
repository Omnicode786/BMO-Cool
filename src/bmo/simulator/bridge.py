"""Browser bridge that turns the simulator UI into real BMO hardware adapters."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import threading
import time
from dataclasses import fields, is_dataclass
from enum import Enum
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import ControlsConfig, OLEDHardware
from ..event_bus import EventBus
from ..events import (
    AgentTurnFinished,
    BargeIn,
    CameraFrame,
    ControlAction,
    ControlEvent,
    Event,
    GameStateUpdated,
    HandObservationEvent,
    ListeningModeChanged,
    MicAudioChunk,
    PlaybackStarted,
    PlaybackStopped,
    ProcessedAudioChunk,
    PushToTalkEnded,
    PushToTalkStarted,
    TTSAudioChunk,
    TTSFinished,
)
from ..latency import LatencyTracker
from ..state import StateMachine, TurnManager
from ..vision.frame_buffer import FrameBuffer, FrameRecord

LOG = logging.getLogger(__name__)
_HIGH_RATE_EVENTS = (
    MicAudioChunk,
    ProcessedAudioChunk,
    CameraFrame,
    TTSAudioChunk,
    HandObservationEvent,
    GameStateUpdated,
)
_MAX_MIC_BYTES = 512 * 1024
_MAX_CAMERA_BYTES = 2 * 1024 * 1024


class _StaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        LOG.debug("simulator http: " + fmt, *args)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class SimulatorControlsService:
    """Translate browser presses into the same typed events as the GPIO adapter."""

    def __init__(self, bus: EventBus, config: ControlsConfig) -> None:
        self.bus = bus
        self.config = config
        self._touch_press_at = 0.0
        self._touch_down = False
        self._pending_tap: asyncio.Task[None] | None = None
        self._listening_mode = "vad"

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        if self._pending_tap:
            self._pending_tap.cancel()
            await asyncio.gather(self._pending_tap, return_exceptions=True)
            self._pending_tap = None
        self._touch_down = False

    def set_listening_mode(self, mode: str) -> None:
        if mode not in {"always", "vad", "touch_to_talk"}:
            raise ValueError("invalid listening mode")
        self._listening_mode = mode
        if mode == "touch_to_talk" and self._pending_tap:
            self._pending_tap.cancel()
            self._pending_tap = None

    async def trigger(self, action: str, *, duration_ms: int = 0) -> None:
        try:
            control = ControlAction(action)
        except ValueError as exc:
            raise ValueError(f"unknown control action: {action}") from exc
        await self.bus.publish(ControlEvent(action=control, duration_ms=max(0, int(duration_ms))))

    async def touch_down(self) -> None:
        if self._touch_down:
            return
        self._touch_down = True
        self._touch_press_at = time.monotonic()
        await self.bus.publish(PushToTalkStarted(source="simulator-touch"))

    async def touch_up(self) -> None:
        if not self._touch_down:
            return
        self._touch_down = False
        duration_ms = int((time.monotonic() - self._touch_press_at) * 1000)
        await self.bus.publish(PushToTalkEnded(source="simulator-touch"))

        # In touch-to-talk mode the hold duration belongs to the microphone gesture,
        # not to BMO's tap/double/long touch UI. Without this guard a normal PTT hold
        # is interpreted as TOUCH_LONG and puts the device to sleep.
        if self._listening_mode == "touch_to_talk":
            return

        if duration_ms >= self.config.timing.long_press_ms:
            if self._pending_tap:
                self._pending_tap.cancel()
                self._pending_tap = None
            await self.bus.publish(ControlEvent(action=ControlAction.TOUCH_LONG, duration_ms=duration_ms))
            return
        if self._pending_tap and not self._pending_tap.done():
            self._pending_tap.cancel()
            self._pending_tap = None
            await self.bus.publish(ControlEvent(action=ControlAction.TOUCH_DOUBLE, duration_ms=duration_ms))
            return
        self._pending_tap = asyncio.create_task(self._publish_single_tap(duration_ms), name="sim-touch-tap")

    async def _publish_single_tap(self, duration_ms: int) -> None:
        try:
            await asyncio.sleep(self.config.timing.double_tap_ms / 1000.0)
            await self.bus.publish(ControlEvent(action=ControlAction.TOUCH_TAP, duration_ms=duration_ms))
        finally:
            self._pending_tap = None


class SimulatorAudioCaptureService:
    """Accept PCM16 microphone chunks from the browser and publish normal capture events."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.available = False
        self.queue_depth = 0

    async def start(self) -> None:
        LOG.info("simulator microphone adapter ready")

    async def stop(self) -> None:
        self.available = False

    async def ingest(self, pcm_b64: str, sample_rate: int, channels: int = 1) -> None:
        pcm = base64.b64decode(pcm_b64, validate=True)
        if not pcm or len(pcm) > _MAX_MIC_BYTES:
            raise ValueError("invalid simulator microphone chunk size")
        if not 8_000 <= sample_rate <= 192_000:
            raise ValueError("invalid simulator microphone sample rate")
        if channels != 1:
            raise ValueError("simulator microphone must be mono")
        self.available = True
        await self.bus.publish(MicAudioChunk(pcm=pcm, sample_rate=sample_rate, channels=channels))


class SimulatorCameraService:
    """Accept browser webcam JPEGs and feed the existing RAM frame buffer and vision pipeline."""

    def __init__(self, bus: EventBus, frame_buffer: FrameBuffer) -> None:
        self.bus = bus
        self.frame_buffer = frame_buffer
        self.available = False
        self._frames = 0
        self._fps_window_start = time.monotonic()
        self._measured_fps = 0.0
        self._ingest_lock = asyncio.Lock()

    @property
    def measured_fps(self) -> float:
        return self._measured_fps

    async def start(self) -> None:
        LOG.info("simulator camera adapter ready")

    async def stop(self) -> None:
        self.available = False

    async def ingest_jpeg(self, jpeg_b64: str) -> FrameRecord:
        raw = base64.b64decode(jpeg_b64, validate=True)
        if not raw or len(raw) > _MAX_CAMERA_BYTES:
            raise ValueError("invalid simulator camera frame size")
        async with self._ingest_lock:
            frame = await asyncio.to_thread(self._decode_jpeg, raw)
            record = self.frame_buffer.add(frame)
            self.available = True
            self._tick_fps()
            await self.bus.publish(
                CameraFrame(
                    frame_id=record.frame_id,
                    frame=record.frame,
                    width=int(record.frame.shape[1]),
                    height=int(record.frame.shape[0]),
                    sharpness=record.sharpness,
                    brightness=record.brightness,
                )
            )
            return record

    async def capture_snapshot(self, reason: str) -> FrameRecord:
        del reason
        latest = self.frame_buffer.latest()
        if latest is None:
            raise RuntimeError("browser camera has not provided a frame yet")
        return latest

    @staticmethod
    def _decode_jpeg(raw: bytes) -> Any:
        import numpy as np
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            return np.asarray(image.convert("RGB"))

    def _tick_fps(self) -> None:
        self._frames += 1
        now = time.monotonic()
        elapsed = now - self._fps_window_start
        if elapsed >= 2.0:
            self._measured_fps = self._frames / elapsed
            self._frames = 0
            self._fps_window_start = now


class SimulatorBridgeService:
    """Serve the UI, receive browser hardware input, and stream real Python events back."""

    def __init__(
        self,
        bus: EventBus,
        state: StateMachine,
        turns: TurnManager,
        static_dir: Path,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self.bus = bus
        self.state = state
        self.turns = turns
        self.static_dir = static_dir
        self.host = host
        self.port = port
        self.ws_port = port + 1
        self.controls: SimulatorControlsService | None = None
        self.capture: SimulatorAudioCaptureService | None = None
        self.camera: SimulatorCameraService | None = None
        self._speaker_complete: Callable[[int], Awaitable[None]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._http_server: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None
        self._ws_server: Any = None
        self._clients: set[Any] = set()
        self._outbound: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
        self._tasks: list[asyncio.Task[None]] = []
        self.latest_oled: dict[str, Any] | None = None

    def bind_adapters(
        self,
        *,
        controls: SimulatorControlsService,
        capture: SimulatorAudioCaptureService,
        camera: SimulatorCameraService,
    ) -> None:
        self.controls = controls
        self.capture = capture
        self.camera = camera

    def bind_speaker_complete(self, callback: Callable[[int], Awaitable[None]]) -> None:
        self._speaker_complete = callback

    async def start(self) -> None:
        if not (self.controls and self.capture and self.camera):
            raise RuntimeError("simulator bridge adapters were not bound")
        if not (self.static_dir / "index.html").exists():
            raise FileNotFoundError(self.static_dir / "index.html")
        self._loop = asyncio.get_running_loop()
        handler = partial(_StaticHandler, directory=str(self.static_dir))
        self._http_server = ThreadingHTTPServer((self.host, self.port), handler)
        self._http_thread = threading.Thread(
            target=self._http_server.serve_forever,
            name="bmo-simulator-http",
            daemon=True,
        )
        self._http_thread.start()
        try:
            from websockets.asyncio.server import serve

            self._ws_server = await serve(self._ws_handler, self.host, self.ws_port, max_size=3 * 1024 * 1024)
        except Exception:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None
            raise
        self._tasks = [
            asyncio.create_task(self._broadcast_loop(), name="simulator-broadcast"),
            asyncio.create_task(self._event_loop(), name="simulator-events"),
        ]
        LOG.info("BMO simulator UI: http://%s:%d (websocket :%d)", self.host, self.port, self.ws_port)
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            LOG.warning("simulator is bound beyond loopback and has no authentication; use only on a trusted network")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for client in tuple(self._clients):
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
            self._ws_server = None
        if self._http_server is not None:
            server, self._http_server = self._http_server, None
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        self._http_thread = None
        self._loop = None

    async def emit(self, payload: dict[str, Any]) -> None:
        self._enqueue_json(json.dumps(payload, separators=(",", ":"), default=str))

    def emit_from_thread(self, payload: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None:
            return
        encoded = json.dumps(payload, separators=(",", ":"), default=str)
        loop.call_soon_threadsafe(self._enqueue_json, encoded)

    def set_latest_oled(self, payload: dict[str, Any]) -> None:
        self.latest_oled = payload
        self.emit_from_thread(payload)

    def _enqueue_json(self, encoded: str) -> None:
        if self._outbound.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._outbound.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._outbound.put_nowait(encoded)

    async def _broadcast_loop(self) -> None:
        while True:
            encoded = await self._outbound.get()
            if not self._clients:
                continue
            clients = tuple(self._clients)
            results = await asyncio.gather(*(client.send(encoded) for client in clients), return_exceptions=True)
            for client, result in zip(clients, results, strict=False):
                if isinstance(result, Exception):
                    self._clients.discard(client)

    async def _event_loop(self) -> None:
        sub = await self.bus.subscribe(Event, maxsize=256, name="simulator-event-stream")
        async with sub:
            while True:
                event = await sub.get()
                if isinstance(event, _HIGH_RATE_EVENTS):
                    continue
                await self.emit({"type": "event", "event": _event_payload(event)})

    async def _ws_handler(self, websocket: Any) -> None:
        self._clients.add(websocket)
        try:
            await websocket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "state": self.state.current.value,
                        "turn_id": self.turns.current,
                        "ws_port": self.ws_port,
                    },
                    separators=(",", ":"),
                )
            )
            if self.latest_oled:
                await websocket.send(json.dumps(self.latest_oled, separators=(",", ":")))
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError("message must be an object")
                    await self._handle_message(message)
                except Exception as exc:
                    await websocket.send(json.dumps({"type": "input_error", "detail": str(exc)}))
        finally:
            self._clients.discard(websocket)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        kind = str(message.get("type", ""))
        if kind == "control":
            assert self.controls is not None
            await self.controls.trigger(str(message.get("action", "")), duration_ms=int(message.get("duration_ms", 0)))
            return
        if kind == "touch":
            assert self.controls is not None
            phase = message.get("phase")
            if phase == "down":
                await self.controls.touch_down()
            elif phase == "up":
                await self.controls.touch_up()
            else:
                raise ValueError("touch phase must be down or up")
            return
        if kind == "mic_chunk":
            assert self.capture is not None
            await self.capture.ingest(
                str(message.get("pcm", "")),
                int(message.get("sample_rate", 16_000)),
                int(message.get("channels", 1)),
            )
            return
        if kind == "camera_frame":
            assert self.camera is not None
            await self.camera.ingest_jpeg(str(message.get("jpeg", "")))
            return
        if kind == "listening_mode":
            mode = str(message.get("mode", ""))
            if mode not in {"always", "vad", "touch_to_talk"}:
                raise ValueError("invalid listening mode")
            assert self.controls is not None
            self.controls.set_listening_mode(mode)
            await self.bus.publish(ListeningModeChanged(mode=mode))
            return
        if kind == "speaker_complete":
            if self._speaker_complete is not None:
                await self._speaker_complete(int(message.get("turn_id", 0)))
            return
        if kind == "ping":
            await self.emit({"type": "pong", "time": time.time()})
            return
        raise ValueError(f"unsupported simulator message type: {kind}")


class SimulatorOLEDDevice:
    """Ship the exact PIL frame rendered by OLEDService to the browser."""

    def __init__(self, config: OLEDHardware, bridge: SimulatorBridgeService) -> None:
        self.width = config.width
        self.height = config.height
        self.bridge = bridge
        self.last_image: Any = None

    def display(self, image: Any) -> None:
        self.last_image = image.copy() if hasattr(image, "copy") else image
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        label = str(getattr(image, "info", {}).get("label", "frame"))
        self.bridge.set_latest_oled(
            {
                "type": "oled_frame",
                "png": base64.b64encode(buffer.getvalue()).decode("ascii"),
                "label": label,
                "width": self.width,
                "height": self.height,
            }
        )

    def close(self) -> None:
        self.last_image = None


class SimulatorPlaybackService:
    """Send real Python TTS PCM to the browser while preserving audible playback boundaries."""

    def __init__(
        self,
        bus: EventBus,
        turns: TurnManager,
        latency: LatencyTracker,
        bridge: SimulatorBridgeService,
        *,
        mock_cloud: bool = False,
    ) -> None:
        self.bus = bus
        self.turns = turns
        self.latency = latency
        self.bridge = bridge
        self.mock_cloud = mock_cloud
        self.queue_depth = 0
        self._task: asyncio.Task[None] | None = None
        self._playing_turn: int | None = None
        self._first_audio_turns: set[int] = set()
        self._tts_finished_turns: set[int] = set()
        self._mock_finish_task: asyncio.Task[None] | None = None
        self.bridge.bind_speaker_complete(self.browser_finished)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="simulator-playback")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._mock_finish_task:
            self._mock_finish_task.cancel()
            await asyncio.gather(self._mock_finish_task, return_exceptions=True)
            self._mock_finish_task = None
        await self._stop_playback(interrupted=False)

    async def _run(self) -> None:
        sub = await self.bus.subscribe(Event, maxsize=512, name="simulator-playback-events")
        async with sub:
            while True:
                event = await sub.get()
                if isinstance(event, TTSAudioChunk):
                    await self._audio(event)
                elif isinstance(event, TTSFinished):
                    if event.turn_id is not None:
                        turn_id = int(event.turn_id)
                        self._tts_finished_turns.add(turn_id)
                        if self._playing_turn == turn_id:
                            await self.bridge.emit({"type": "speaker_end", "turn_id": turn_id})
                elif isinstance(event, BargeIn):
                    if self._playing_turn == event.interrupted_turn_id:
                        await self._stop_playback(interrupted=True)
                elif self.mock_cloud and isinstance(event, AgentTurnFinished):
                    if event.turn_id is not None and self._playing_turn == int(event.turn_id):
                        if self._mock_finish_task:
                            self._mock_finish_task.cancel()
                        self._mock_finish_task = asyncio.create_task(
                            self._finish_mock_after_delay(int(event.turn_id)),
                            name="simulator-mock-playback-finish",
                        )

    async def _audio(self, event: TTSAudioChunk) -> None:
        if not self.turns.is_current(event.turn_id) or event.turn_id is None:
            return
        turn_id = int(event.turn_id)
        if self._playing_turn != turn_id:
            self._playing_turn = turn_id
            await self.bus.publish(PlaybackStarted(turn_id=turn_id, sample_rate=event.sample_rate))
        await self.bridge.emit(
            {
                "type": "speaker_audio",
                "turn_id": turn_id,
                "sample_rate": event.sample_rate,
                "pcm": base64.b64encode(event.pcm).decode("ascii"),
            }
        )
        if turn_id not in self._first_audio_turns:
            self._first_audio_turns.add(turn_id)
            self.latency.mark(turn_id, "speaker_first_audio")

    async def browser_finished(self, turn_id: int) -> None:
        if turn_id <= 0 or self._playing_turn != turn_id:
            return
        if turn_id not in self._tts_finished_turns:
            return
        await self._stop_playback(interrupted=False)

    async def _finish_mock_after_delay(self, turn_id: int) -> None:
        await asyncio.sleep(0.35)
        if self._playing_turn == turn_id:
            self._tts_finished_turns.add(turn_id)
            await self._stop_playback(interrupted=False)

    async def _stop_playback(self, *, interrupted: bool) -> None:
        turn_id, self._playing_turn = self._playing_turn, None
        if interrupted:
            await self.bridge.emit({"type": "speaker_clear", "interrupted": True})
        if turn_id is not None:
            self._tts_finished_turns.discard(turn_id)
            await self.bus.publish(PlaybackStopped(turn_id=turn_id, interrupted=interrupted))


def _event_payload(event: Event) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": type(event).__name__}
    if not is_dataclass(event):
        return payload
    for item in fields(event):
        if item.name in {"pcm", "frame"}:
            continue
        payload[item.name] = _json_value(getattr(event, item.name))
    return payload


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)
