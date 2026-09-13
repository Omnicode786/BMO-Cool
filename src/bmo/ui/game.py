"""Small local Star Catcher OLED game controlled by joystick and buttons."""

from __future__ import annotations

import asyncio
import random

from ..event_bus import EventBus
from ..events import ControlAction, ControlEvent, GameStateUpdated, ModeChanged


class StarCatcherGame:
    """Simple deterministic state machine: move the paddle and catch falling stars."""

    def __init__(self, width: int = 128, height: int = 64) -> None:
        self.width = width
        self.height = height
        self.active = False
        self.player_x = width // 2
        self.star_x = width // 2
        self.star_y = 12
        self.score = 0
        self.lives = 3
        self.message = ""
        self._rng = random.Random()

    def reset(self) -> None:
        self.player_x = self.width // 2
        self.score = 0
        self.lives = 3
        self.message = "Catch stars!"
        self._new_star()

    def move(self, delta: int) -> None:
        self.player_x = max(9, min(self.width - 9, self.player_x + delta))

    def tick(self) -> None:
        if not self.active:
            return
        self.message = ""
        self.star_y += 3
        if self.star_y >= self.height - 8:
            if abs(self.star_x - self.player_x) <= 13:
                self.score += 1
                self.message = "nice catch!"
            else:
                self.lives -= 1
                self.message = "bonk!"
            if self.lives <= 0:
                self.message = f"score {self.score} - A retry"
                self.active = False
            self._new_star()

    def _new_star(self) -> None:
        self.star_x = self._rng.randint(7, self.width - 7)
        self.star_y = 12

    def event(self) -> GameStateUpdated:
        return GameStateUpdated(
            active=self.active or self.lives <= 0,
            player_x=self.player_x,
            star_x=self.star_x,
            star_y=self.star_y,
            score=self.score,
            lives=self.lives,
            message=self.message,
        )


class GameService:
    """Run game ticks only while game mode is active; no network calls are used."""

    def __init__(self, bus: EventBus, width: int = 128, height: int = 64) -> None:
        self.bus = bus
        self.game = StarCatcherGame(width, height)
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._control_loop(), name="game-controls"),
            asyncio.create_task(self._tick_loop(), name="game-tick"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _control_loop(self) -> None:
        modes = await self.bus.subscribe(ModeChanged, maxsize=8, name="game-mode")
        controls = await self.bus.subscribe(ControlEvent, maxsize=16, name="game-input")
        try:
            while True:
                mode_task = asyncio.create_task(modes.get())
                control_task = asyncio.create_task(controls.get())
                done, pending = await asyncio.wait({mode_task, control_task}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                if mode_task in done:
                    mode = mode_task.result().mode
                    if mode == "game":
                        self.game.active = True
                        self.game.reset()
                        await self.bus.publish(self.game.event())
                    elif self.game.active or self.game.lives <= 0:
                        self.game.active = False
                        await self.bus.publish(GameStateUpdated(active=False))
                    continue
                event = control_task.result()
                if not self.game.active and self.game.lives > 0:
                    continue
                if event.action == ControlAction.JOY_LEFT:
                    self.game.move(-10)
                elif event.action == ControlAction.JOY_RIGHT:
                    self.game.move(10)
                elif event.action == ControlAction.BUTTON_A and self.game.lives <= 0:
                    self.game.active = True
                    self.game.reset()
                await self.bus.publish(self.game.event())
        finally:
            modes.close()
            controls.close()

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(0.12)
            if self.game.active:
                self.game.tick()
                await self.bus.publish(self.game.event())
