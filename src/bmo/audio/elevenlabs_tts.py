"""Persistent ElevenLabs multi-context WebSocket TTS with turn-safe interruption."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import random
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from ..config import ElevenLabsConfig, TTSConfig
from ..event_bus import EventBus
from ..events import (
    AgentTextChunk,
    AgentTurnFinished,
    BargeIn,
    NetworkLost,
    NetworkRestored,
    TTSAudioChunk,
    TTSFinished,
    TTSStarted,
    TTSTurnFailed,
)
from ..latency import LatencyTracker
from ..state import TurnManager

LOG = logging.getLogger(__name__)


class TextChunker:
    """Turn token-like model deltas into natural TTS phrase/sentence chunks."""

    def __init__(self, min_chars: int = 30, max_chars: int = 130) -> None:
        if min_chars < 1 or max_chars <= min_chars:
            raise ValueError("invalid text chunker bounds")
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.buffer = ""

    def add(self, text: str) -> list[str]:
        self.buffer += text
        output: list[str] = []
        while True:
            cut = self._find_cut()
            if cut <= 0:
                break
            chunk = self.buffer[:cut].strip()
            self.buffer = self.buffer[cut:].lstrip()
            if chunk:
                output.append(chunk + " ")
        return output

    def flush(self) -> str:
        remainder = self.buffer.strip()
        self.buffer = ""
        return remainder + (" " if remainder else "")

    def _find_cut(self) -> int:
        if len(self.buffer) < self.min_chars:
            return 0
        search_end = min(len(self.buffer), self.max_chars)
        for punctuation in (". ", "! ", "? ", "; ", ": ", ", "):
            index = self.buffer.rfind(punctuation, self.min_chars - 1, search_end)
            if index >= 0:
                return index + len(punctuation)
        if len(self.buffer) >= self.max_chars:
            index = self.buffer.rfind(" ", self.min_chars - 1, self.max_chars)
            return index + 1 if index >= 0 else self.max_chars
        return 0


@dataclass(slots=True)
class _Context:
    turn_id: int
    context_id: str
    started: bool = False
    first_audio_seen: bool = False


class ElevenLabsRealtimeClient:
    """Reuse one WebSocket and one context per response turn."""

    def __init__(
        self,
        bus: EventBus,
        turns: TurnManager,
        config: ElevenLabsConfig,
        tts_config: TTSConfig,
        api_key: str,
        voice_id: str,
        latency: LatencyTracker,
    ) -> None:
        self.bus = bus
        self.turns = turns
        self.config = config
        self.tts_config = tts_config
        self.api_key = api_key
        self.voice_id = voice_id
        self.latency = latency
        self._ws: Any = None
        self._receiver: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._contexts: dict[str, _Context] = {}
        self._connected = False

    def _url(self) -> str:
        query = urlencode(
            {
                "model_id": self.config.model_id,
                "output_format": self.config.output_format,
                "inactivity_timeout": self.tts_config.inactivity_timeout_sec,
                "auto_mode": str(self.tts_config.auto_mode).lower(),
            }
        )
        return f"wss://api.elevenlabs.io/v1/text-to-speech/{quote(self.voice_id)}/multi-stream-input?{query}"

    async def connect(self) -> None:
        if self._ws is not None:
            return
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("websockets is required for ElevenLabs TTS") from exc
        async with self._lock:
            if self._ws is not None:
                return
            ws = await asyncio.wait_for(
                websockets.connect(
                    self._url(),
                    additional_headers={"xi-api-key": self.api_key},
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=2**23,
                ),
                timeout=self.config.retry.connect_timeout_sec,
            )
            self._ws = ws
            self._receiver = asyncio.create_task(self._receive_loop(ws), name="elevenlabs-receive")
            self._connected = True
            await self.bus.publish(NetworkRestored(service="elevenlabs_tts"))
            LOG.info("ElevenLabs TTS WebSocket connected model=%s format=%s", self.config.model_id, self.config.output_format)

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"close_socket": True}))
            with contextlib.suppress(Exception):
                await ws.close()
        if self._receiver:
            self._receiver.cancel()
            await asyncio.gather(self._receiver, return_exceptions=True)
            self._receiver = None
        self._contexts.clear()

    async def _send(self, payload: dict[str, Any], *, allow_reconnect: bool = False) -> None:
        """Send one protocol message, reconnecting only before a context exists.

        ElevenLabs multi-context state belongs to one WebSocket. If that socket dies
        after a context has begun, replaying text on a new socket could duplicate or
        reorder speech. Therefore only context creation may reconnect/retry; an
        established turn fails closed and the next turn opens a fresh context.
        """

        backoff = self.config.retry.initial_backoff_sec
        attempts = 4 if allow_reconnect else 1
        for attempt in range(attempts):
            ws = None
            try:
                if self._ws is None:
                    if not allow_reconnect:
                        raise ConnectionError("ElevenLabs context transport is no longer connected")
                    await self.connect()
                ws = self._ws
                if ws is None:
                    raise ConnectionError("ElevenLabs WebSocket unavailable after connect")
                await asyncio.wait_for(
                    ws.send(json.dumps(payload)),
                    timeout=self.config.retry.read_timeout_sec,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("ElevenLabs send failed attempt=%d/%d: %s", attempt + 1, attempts, exc)
                await self._drop_socket(exc, expected_ws=ws)
                if attempt + 1 >= attempts:
                    raise
                jitter = backoff * self.config.retry.jitter_ratio * random.random()
                await asyncio.sleep(backoff + jitter)
                backoff = min(self.config.retry.max_backoff_sec, backoff * 2.0)

    async def start_context(self, turn_id: int) -> str:
        context_id = f"turn-{turn_id}"
        # A single space initializes a context without generating a spoken word. Context
        # creation is the only protocol operation safe to replay after reconnect.
        await self._send({"context_id": context_id, "text": " "}, allow_reconnect=True)
        self._contexts[context_id] = _Context(turn_id=turn_id, context_id=context_id)
        await self.bus.publish(TTSStarted(turn_id=turn_id, context_id=context_id))
        return context_id

    async def send_text(self, context_id: str, text: str, *, flush: bool = False) -> None:
        if not text and not flush:
            return
        if context_id not in self._contexts:
            raise ConnectionError("ElevenLabs context is no longer active")
        payload: dict[str, Any] = {"context_id": context_id}
        if text:
            payload["text"] = text
        if flush:
            payload["flush"] = True
        await self._send(payload)

    async def finish_context(self, context_id: str) -> None:
        if context_id not in self._contexts:
            return
        await self._send({"context_id": context_id, "flush": True})
        # Closing a context also flushes it and frees one of the five context slots.
        await self._send({"context_id": context_id, "close_context": True})

    async def cancel_context(self, turn_id: int) -> None:
        for context_id, ctx in list(self._contexts.items()):
            if ctx.turn_id == turn_id:
                with contextlib.suppress(Exception):
                    await self._send({"context_id": context_id, "close_context": True})
                self._contexts.pop(context_id, None)

    async def _receive_loop(self, ws: Any) -> None:
        try:
            async for raw in ws:
                data = json.loads(raw)
                context_id = data.get("context_id")
                ctx = self._contexts.get(context_id)
                if ctx is None:
                    continue
                audio_b64 = data.get("audio")
                if audio_b64:
                    pcm = base64.b64decode(audio_b64)
                    if self.turns.is_current(ctx.turn_id):
                        if not ctx.first_audio_seen:
                            ctx.first_audio_seen = True
                            self.latency.mark(ctx.turn_id, "elevenlabs_first_audio")
                        await self.bus.publish(
                            TTSAudioChunk(
                                turn_id=ctx.turn_id,
                                pcm=pcm,
                                sample_rate=self.config.sample_rate,
                                context_id=context_id,
                            )
                        )
                if data.get("is_final"):
                    await self.bus.publish(TTSFinished(turn_id=ctx.turn_id, context_id=context_id))
                    self._contexts.pop(context_id, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._ws is ws:
                LOG.warning("ElevenLabs receive loop ended: %s", exc)
                await self._drop_socket(exc, expected_ws=ws)

    async def _drop_socket(self, exc: Exception, *, expected_ws: Any = None) -> None:
        # A late exception from an old receiver must never tear down a newer socket.
        if expected_ws is not None and self._ws is not expected_ws:
            with contextlib.suppress(Exception):
                await expected_ws.close()
            return
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if self._connected:
            await self.bus.publish(NetworkLost(service="elevenlabs_tts", detail=type(exc).__name__))
        self._connected = False
        # Server contexts do not survive a transport reconnect. The next turn will
        # reconnect with backoff; the interrupted TTS turn fails instead of replaying.
        self._contexts.clear()


class TTSService:
    """Segment streaming Gemini text and feed ElevenLabs before generation finishes."""

    def __init__(self, bus: EventBus, turns: TurnManager, client: ElevenLabsRealtimeClient, config: TTSConfig) -> None:
        self.bus = bus
        self.turns = turns
        self.client = client
        self.config = config
        self._tasks: list[asyncio.Task[None]] = []
        self._chunkers: dict[int, TextChunker] = {}
        self._contexts: dict[int, str] = {}

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._text_loop(), name="tts-text"),
            asyncio.create_task(self._finish_loop(), name="tts-finish"),
            asyncio.create_task(self._barge_loop(), name="tts-barge"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.client.close()

    async def _ensure_turn(self, turn_id: int) -> tuple[TextChunker, str]:
        chunker = self._chunkers.get(turn_id)
        if chunker is None:
            chunker = TextChunker(self.config.min_chunk_chars, self.config.max_chunk_chars)
            self._chunkers[turn_id] = chunker
        context = self._contexts.get(turn_id)
        if context is None:
            context = await self.client.start_context(turn_id)
            self._contexts[turn_id] = context
        return chunker, context

    async def _text_loop(self) -> None:
        sub = await self.bus.subscribe(AgentTextChunk, maxsize=128, drop_oldest=False, name="tts-text")
        async with sub:
            while True:
                event = await sub.get()
                if not self.turns.is_current(event.turn_id):
                    continue
                turn_id = int(event.turn_id)
                try:
                    chunker, context = await self._ensure_turn(turn_id)
                    for phrase in chunker.add(event.text):
                        # Current ElevenLabs multi-context guidance recommends flushing
                        # complete sentences while allowing partial phrases to buffer naturally.
                        sentence_boundary = phrase.rstrip().endswith((".", "!", "?"))
                        await self.client.send_text(context, phrase, flush=sentence_boundary)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.exception("TTS text streaming failed", extra={"turn_id": turn_id})
                    self._chunkers.pop(turn_id, None)
                    self._contexts.pop(turn_id, None)
                    await self.bus.publish(TTSTurnFailed(turn_id=turn_id, detail=type(exc).__name__))

    async def _finish_loop(self) -> None:
        sub = await self.bus.subscribe(AgentTurnFinished, maxsize=32, name="tts-finish")
        async with sub:
            while True:
                event = await sub.get()
                if event.turn_id is None:
                    continue
                turn_id = event.turn_id
                if event.silent or not self.turns.is_current(turn_id):
                    self._chunkers.pop(turn_id, None)
                    context = self._contexts.pop(turn_id, None)
                    if context:
                        await self.client.cancel_context(turn_id)
                    continue
                try:
                    chunker, context = await self._ensure_turn(turn_id)
                    remainder = chunker.flush()
                    if remainder:
                        await self.client.send_text(context, remainder)
                    await self.client.finish_context(context)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.exception("TTS turn flush failed", extra={"turn_id": turn_id})
                    await self.bus.publish(TTSTurnFailed(turn_id=turn_id, detail=type(exc).__name__))
                finally:
                    self._chunkers.pop(turn_id, None)
                    self._contexts.pop(turn_id, None)

    async def _barge_loop(self) -> None:
        sub = await self.bus.subscribe(BargeIn, maxsize=16, name="tts-barge")
        async with sub:
            while True:
                event = await sub.get()
                self._chunkers.pop(event.interrupted_turn_id, None)
                self._contexts.pop(event.interrupted_turn_id, None)
                with contextlib.suppress(Exception):
                    await self.client.cancel_context(event.interrupted_turn_id)


class MockTTSService:
    """Development TTS mock that emits short PCM chunks without network access."""

    def __init__(self, bus: EventBus, turns: TurnManager, sample_rate: int = 24000) -> None:
        self.bus = bus
        self.turns = turns
        self.sample_rate = sample_rate
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mock-tts")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        sub = await self.bus.subscribe(AgentTextChunk, name="mock-tts-input")
        async with sub:
            while True:
                event = await sub.get()
                if self.turns.is_current(event.turn_id) and event.turn_id is not None:
                    samples = max(240, min(2400, len(event.text) * 80))
                    pcm = b"\x00\x00" * samples
                    await self.bus.publish(
                        TTSAudioChunk(
                            turn_id=event.turn_id,
                            pcm=pcm,
                            sample_rate=self.sample_rate,
                            context_id=f"mock-{event.turn_id}",
                        )
                    )
