"""Low-cost PCM metering and fallback feedback suppression."""

from __future__ import annotations

import asyncio
import math
from array import array

from ..config import AudioConfig
from ..event_bus import EventBus
from ..events import MicAudioChunk, ProcessedAudioChunk
from ..state import StateMachine


def pcm16_rms(pcm: bytes) -> float:
    """Return normalized RMS for little-endian signed 16-bit PCM."""
    if len(pcm) < 2:
        return 0.0
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return min(1.0, math.sqrt(mean_square) / 32768.0)


def scale_pcm16(pcm: bytes, gain: float) -> bytes:
    """Scale little-endian PCM without requiring NumPy in the audio hot path."""
    if gain >= 0.999:
        return pcm
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    for index, sample in enumerate(samples):
        samples[index] = max(-32768, min(32767, int(sample * gain)))
    return samples.tobytes()


class AudioPreprocessorService:
    """Annotate capture chunks and apply only conservative non-AEC fallback gating."""

    def __init__(self, bus: EventBus, state: StateMachine, config: AudioConfig) -> None:
        self.bus = bus
        self.state = state
        self.config = config
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="audio-preprocess")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        sub = await self.bus.subscribe(MicAudioChunk, maxsize=self.config.queue_max_chunks, name="audio-preprocess")
        async with sub:
            while True:
                event = await sub.get()
                rms = pcm16_rms(event.pcm)
                transmit = True
                if self.config.aec_mode == "fallback" and self.state.speaking:
                    # Suppress only frames overwhelmingly likely to be speaker bleed. A
                    # strong near-field user remains eligible for local VAD/barge-in.
                    if rms >= self.config.feedback_suppression_rms:
                        transmit = False
                await self.bus.publish(
                    ProcessedAudioChunk(
                        event_id=event.event_id,
                        timestamp=event.timestamp,
                        turn_id=event.turn_id,
                        pcm=event.pcm,
                        sample_rate=event.sample_rate,
                        channels=event.channels,
                        rms=rms,
                        transmit=transmit,
                    )
                )
