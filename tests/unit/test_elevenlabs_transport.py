"""ElevenLabs transport retry and context-safety tests without network access."""

from __future__ import annotations

import pytest

from bmo.audio.elevenlabs_tts import ElevenLabsRealtimeClient
from bmo.config import ElevenLabsConfig, RetryConfig, TTSConfig
from bmo.event_bus import EventBus
from bmo.latency import LatencyTracker
from bmo.state import TurnManager


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail_sends = False
        self.closed = False

    async def send(self, payload: str) -> None:
        if self.fail_sends:
            raise OSError("simulated transport loss")
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


def make_client() -> ElevenLabsRealtimeClient:
    return ElevenLabsRealtimeClient(
        EventBus(),
        TurnManager(),
        ElevenLabsConfig(
            retry=RetryConfig(
                connect_timeout_sec=0.1,
                read_timeout_sec=0.1,
                initial_backoff_sec=0.001,
                max_backoff_sec=0.002,
                jitter_ratio=0.0,
            )
        ),
        TTSConfig(),
        api_key="test-key-not-real",
        voice_id="test-voice",
        latency=LatencyTracker(),
    )


@pytest.mark.asyncio
async def test_context_creation_reconnects_with_backoff_before_turn_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client()
    ws = FakeWebSocket()
    attempts = 0

    async def flaky_connect() -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("simulated connect failure")
        client._ws = ws  # noqa: SLF001 - intentional transport unit test.

    monkeypatch.setattr(client, "connect", flaky_connect)
    context_id = await client.start_context(7)

    assert context_id == "turn-7"
    assert attempts == 3
    assert context_id in client._contexts  # noqa: SLF001 - verify server/local context registration ordering.
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_mid_context_transport_loss_fails_closed_and_next_turn_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client()
    first_ws = FakeWebSocket()
    second_ws = FakeWebSocket()
    sockets = iter((first_ws, second_ws))
    connect_calls = 0

    async def fake_connect() -> None:
        nonlocal connect_calls
        connect_calls += 1
        client._ws = next(sockets)  # noqa: SLF001 - intentional transport unit test.

    monkeypatch.setattr(client, "connect", fake_connect)
    first_context = await client.start_context(1)
    first_ws.fail_sends = True

    with pytest.raises(OSError):
        await client.send_text(first_context, "This must not be replayed after reconnect. ")

    assert client._ws is None  # noqa: SLF001
    assert not client._contexts  # noqa: SLF001
    second_context = await client.start_context(2)
    assert second_context == "turn-2"
    assert connect_calls == 2
    assert len(second_ws.sent) == 1
