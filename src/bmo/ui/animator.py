"""Non-blocking OLED state/face animator with bounded refresh rate."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time

from ..config import OLEDBehaviorConfig
from ..event_bus import EventBus
from ..events import ExpressionRequested, GameStateUpdated, HealthSnapshot, ModeChanged, MoodChanged, StateChanged
from ..hardware.oled import OLEDDevice
from .faces import FaceFrame, FaceRenderer

LOG = logging.getLogger(__name__)

_STATE_FACE = {
    "BOOTING": "booting",
    "IDLE": "idle",
    "LISTENING": "listening",
    "THINKING": "thinking",
    "SPEAKING": "speaking",
    "SLEEPING": "sleepy",
    "OFFLINE": "offline",
    "ERROR": "error",
}


class OLEDService:
    """Animate cute faces without blocking the audio/event loop or flooding I2C."""

    def __init__(
        self,
        bus: EventBus,
        device: OLEDDevice,
        config: OLEDBehaviorConfig,
        *,
        fps: int,
    ) -> None:
        self.bus = bus
        self.device = device
        self.config = config
        self.fps = max(1, fps)
        self.renderer = FaceRenderer(device.width, device.height)
        self._state = "BOOTING"
        self._mood = "happy"
        self._override: str | None = None
        self._override_until = 0.0
        self._game: GameStateUpdated | None = None
        self._mode = "companion"
        self._health: HealthSnapshot | None = None
        self._dirty = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._phase = 0
        self._next_blink = time.monotonic() + random.uniform(config.blink_min_sec, config.blink_max_sec)
        self._blink_until = 0.0
        self.available = True

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._event_loop(), name="oled-events"),
            asyncio.create_task(self._render_loop(), name="oled-render"),
        ]
        self._dirty.set()

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.device.close)

    async def _event_loop(self) -> None:
        state_sub = await self.bus.subscribe(StateChanged, maxsize=16, name="oled-state")
        expression_sub = await self.bus.subscribe(ExpressionRequested, maxsize=16, name="oled-expression")
        mood_sub = await self.bus.subscribe(MoodChanged, maxsize=16, name="oled-mood")
        game_sub = await self.bus.subscribe(GameStateUpdated, maxsize=4, name="oled-game")
        mode_sub = await self.bus.subscribe(ModeChanged, maxsize=8, name="oled-mode")
        health_sub = await self.bus.subscribe(HealthSnapshot, maxsize=2, name="oled-health")
        try:
            while True:
                tasks = {
                    asyncio.create_task(state_sub.get()): "state",
                    asyncio.create_task(expression_sub.get()): "expression",
                    asyncio.create_task(mood_sub.get()): "mood",
                    asyncio.create_task(game_sub.get()): "game",
                    asyncio.create_task(mode_sub.get()): "mode",
                    asyncio.create_task(health_sub.get()): "health",
                }
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                task = next(iter(done))
                event = task.result()
                kind = tasks[task]
                if kind == "state":
                    self._state = event.current
                elif kind == "expression":
                    self._override = event.expression
                    self._override_until = time.monotonic() + (event.duration_sec or self.config.expression_hold_sec)
                elif kind == "mood":
                    self._mood = event.mood
                elif kind == "game":
                    self._game = event if event.active else None
                elif kind == "mode":
                    self._mode = event.mode
                elif kind == "health":
                    self._health = event
                self._dirty.set()
        finally:
            state_sub.close()
            expression_sub.close()
            mood_sub.close()
            game_sub.close()
            mode_sub.close()
            health_sub.close()

    async def _render_loop(self) -> None:
        frame_period = 1.0 / self.fps
        while True:
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=frame_period)
            except TimeoutError:
                self._dirty.clear()
            self._dirty.clear()
            now = time.monotonic()
            if now >= self._next_blink:
                self._blink_until = now + 0.12
                self._next_blink = now + random.uniform(self.config.blink_min_sec, self.config.blink_max_sec)
            if self._override and now >= self._override_until:
                self._override = None
            self._phase += 1
            if self._game is not None:
                image = self.renderer.render_game(self._game)
            elif self._mode == "status":
                image = self.renderer.render_status(self._health)
            else:
                expression = self._override or _STATE_FACE.get(self._state, "idle")
                image = self.renderer.render_face(
                    FaceFrame(expression=expression, blink=now < self._blink_until, mouth_phase=self._phase, mood=self._mood)
                )
            try:
                await asyncio.to_thread(self.device.display, image)
                self.available = True
            except Exception as exc:
                if self.available:
                    LOG.warning("OLED failed; continuing headless: %s", exc)
                self.available = False
                await asyncio.sleep(1.0)
