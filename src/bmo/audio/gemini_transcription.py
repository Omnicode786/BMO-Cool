"""Persistent Gemini Live transcription client with hybrid/manual VAD and resumption."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from typing import Any

from ..config import AudioConfig, GeminiConfig
from ..event_bus import EventBus
from ..events import (
    ListeningModeChanged,
    NetworkLost,
    NetworkRestored,
    ProcessedAudioChunk,
    PushToTalkEnded,
    PushToTalkStarted,
    SpeechEnded,
    TranscriptCommitted,
    TranscriptPartial,
)
from ..latency import LatencyTracker
from ..state import TurnManager

LOG = logging.getLogger(__name__)


class GeminiLiveTranscriptionService:
    """Keep one Live STT session alive across utterances and reconnect it safely."""

    def __init__(
        self,
        bus: EventBus,
        turns: TurnManager,
        gemini: GeminiConfig,
        audio: AudioConfig,
        api_key: str,
        latency: LatencyTracker,
    ) -> None:
        self.bus = bus
        self.turns = turns
        self.gemini = gemini
        self.audio = audio
        self.api_key = api_key
        self.latency = latency
        self._task: asyncio.Task[None] | None = None
        self._forward_tasks: list[asyncio.Task[None]] = []
        self._inputs: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=audio.queue_max_chunks * 2)
        self._mode = audio.listening_mode
        self._session_handle: str | None = None
        self._reconfigure = asyncio.Event()
        self._ptt_active = False
        self._connected = False

    async def start(self) -> None:
        if not self.api_key:
            LOG.warning("Gemini key absent: live transcription disabled")
            return
        subscriptions = [
            (ProcessedAudioChunk, "audio", self.audio.queue_max_chunks * 2),
            (SpeechEnded, "speech_end", 16),
            (PushToTalkStarted, "ptt_start", 8),
            (PushToTalkEnded, "ptt_end", 8),
            (ListeningModeChanged, "mode", 8),
        ]
        for event_type, kind, maxsize in subscriptions:
            sub = await self.bus.subscribe(event_type, maxsize=maxsize, name=f"stt-{kind}")
            self._forward_tasks.append(asyncio.create_task(self._forward(sub, kind), name=f"stt-forward-{kind}"))
        self._task = asyncio.create_task(self._connection_loop(), name="gemini-live-stt")

    async def stop(self) -> None:
        for task in self._forward_tasks:
            task.cancel()
        await asyncio.gather(*self._forward_tasks, return_exceptions=True)
        self._forward_tasks.clear()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _forward(self, subscription: Any, kind: str) -> None:
        async with subscription:
            while True:
                event = await subscription.get()
                if kind == "audio" and self._inputs.full():
                    # Drop oldest audio rather than growing an utterance-sized buffer.
                    with contextlib.suppress(asyncio.QueueEmpty):
                        self._inputs.get_nowait()
                await self._inputs.put((kind, event))

    def _build_config(self, types: Any) -> Any:
        kwargs: dict[str, Any] = {
            "response_modalities": ["TEXT"],
            "input_audio_transcription": types.AudioTranscriptionConfig(
                language_codes=self.gemini.transcription_language_codes,
            ),
        }
        if self._mode == "touch_to_talk":
            kwargs["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
            )
        if self.gemini.enable_context_compression:
            kwargs["context_window_compression"] = types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            )
        if self.gemini.enable_session_resumption:
            kwargs["session_resumption"] = types.SessionResumptionConfig(handle=self._session_handle)
        return types.LiveConnectConfig(**kwargs)

    async def _connection_loop(self) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            LOG.error("google-genai is not installed: %s", exc)
            return
        client = genai.Client(api_key=self.api_key)
        backoff = self.gemini.retry.initial_backoff_sec
        while True:
            self._reconfigure.clear()
            try:
                config = self._build_config(types)
                async with asyncio.timeout(self.gemini.retry.connect_timeout_sec):
                    connection = client.aio.live.connect(model=self.gemini.transcription_model, config=config)
                    session = await connection.__aenter__()
                try:
                    if not self._connected:
                        await self.bus.publish(NetworkRestored(service="gemini_stt"))
                    self._connected = True
                    LOG.info("Gemini Live transcription connected model=%s mode=%s", self.gemini.transcription_model, self._mode)
                    backoff = self.gemini.retry.initial_backoff_sec
                    await self._run_session(session, types)
                finally:
                    await connection.__aexit__(None, None, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected = False
                LOG.warning("Gemini Live transcription disconnected: %s", exc)
                await self.bus.publish(NetworkLost(service="gemini_stt", detail=type(exc).__name__))
                jitter = backoff * self.gemini.retry.jitter_ratio * random.random()
                await asyncio.sleep(backoff + jitter)
                backoff = min(self.gemini.retry.max_backoff_sec, backoff * 2)

    async def _run_session(self, session: Any, types: Any) -> None:
        sender = asyncio.create_task(self._send_loop(session, types), name="stt-send")
        receiver = asyncio.create_task(self._receive_loop(session), name="stt-receive")
        reconfigure = asyncio.create_task(self._reconfigure.wait(), name="stt-reconfigure")
        done, pending = await asyncio.wait({sender, receiver, reconfigure}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task is reconfigure:
                LOG.info("reconnecting Live transcription for listening mode=%s", self._mode)
                return
            exc = task.exception()
            if exc:
                raise exc

    async def _send_loop(self, session: Any, types: Any) -> None:
        while True:
            kind, event = await self._inputs.get()
            if kind == "mode":
                if event.mode not in {"always", "vad", "touch_to_talk"}:
                    LOG.warning("ignoring invalid listening mode %s", event.mode)
                    continue
                if event.mode != self._mode:
                    self._mode = event.mode
                    self._ptt_active = False
                    self._reconfigure.set()
                    return
                continue
            if kind == "ptt_start":
                if self._mode == "touch_to_talk":
                    self._ptt_active = True
                    await session.send_realtime_input(activity_start=types.ActivityStart())
                continue
            if kind == "ptt_end":
                if self._mode == "touch_to_talk" and self._ptt_active:
                    await session.send_realtime_input(activity_end=types.ActivityEnd())
                    self._ptt_active = False
                continue
            if kind == "speech_end":
                if self._mode == "vad":
                    await session.send_realtime_input(audio_stream_end=True)
                continue
            if kind == "audio":
                if self._mode == "touch_to_talk" and not self._ptt_active:
                    continue
                if not event.transmit:
                    continue
                await session.send_realtime_input(
                    audio=types.Blob(data=event.pcm, mime_type=f"audio/pcm;rate={event.sample_rate}")
                )

    async def _receive_loop(self, session: Any) -> None:
        async for response in session.receive():
            update = getattr(response, "session_resumption_update", None)
            if update and getattr(update, "resumable", False) and getattr(update, "new_handle", None):
                self._session_handle = update.new_handle
            if getattr(response, "go_away", None):
                LOG.info("Gemini Live GoAway received; reconnecting with resumption token")
                return
            server_content = getattr(response, "server_content", None)
            if not server_content:
                continue
            interim = getattr(server_content, "interim_input_transcription", None)
            if interim and getattr(interim, "text", "").strip():
                await self.bus.publish(TranscriptPartial(turn_id=self.turns.current, text=interim.text.strip()))
            final = getattr(server_content, "input_transcription", None)
            if final and getattr(final, "text", "").strip():
                turn_id = self.turns.current
                text = final.text.strip()
                self.latency.mark(turn_id, "stt_commit")
                LOG.info("committed transcript: %s", text, extra={"turn_id": turn_id})
                await self.bus.publish(TranscriptCommitted(turn_id=turn_id, text=text))


class MockTranscriptionService:
    """Development STT mock that commits a configured transcript at each local speech end."""

    def __init__(self, bus: EventBus, turns: TurnManager, text: str, latency: LatencyTracker) -> None:
        self.bus = bus
        self.turns = turns
        self.text = text
        self.latency = latency
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mock-transcription")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        sub = await self.bus.subscribe(SpeechEnded, maxsize=8, name="mock-stt-end")
        async with sub:
            while True:
                event = await sub.get()
                if event.turn_id is None or not self.turns.is_current(event.turn_id):
                    continue
                await self.bus.publish(TranscriptPartial(turn_id=event.turn_id, text=self.text[: max(1, len(self.text) // 2)]))
                self.latency.mark(event.turn_id, "stt_commit")
                await self.bus.publish(TranscriptCommitted(turn_id=event.turn_id, text=self.text))
