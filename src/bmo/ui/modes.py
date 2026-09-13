"""Physical control mode switching, touch attention, sleep/wake, and manual interruption."""

from __future__ import annotations

import asyncio

from ..config import ControlsConfig
from ..event_bus import EventBus
from ..events import BargeIn, ControlAction, ControlEvent, ExpressionRequested, ModeChanged
from ..state import AppState, StateMachine, TurnManager


class ModeController:
    """Give physical controls deterministic authority above routine AI behavior."""

    def __init__(self, bus: EventBus, state: StateMachine, turns: TurnManager, config: ControlsConfig) -> None:
        self.bus = bus
        self.state = state
        self.turns = turns
        self.config = config
        self.mode = config.modes[0]
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.bus.publish(ModeChanged(mode=self.mode))
        self._task = asyncio.create_task(self._run(), name="mode-controls")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        sub = await self.bus.subscribe(ControlEvent, maxsize=32, name="mode-control-events")
        async with sub:
            while True:
                event = await sub.get()
                if event.action == ControlAction.BUTTON_B:
                    await self._cycle_mode()
                elif event.action == ControlAction.BUTTON_A and self.mode == "companion":
                    await self.bus.publish(ExpressionRequested(expression="curious", duration_sec=0.8))
                elif event.action == ControlAction.TOUCH_LONG:
                    await self._toggle_sleep()
                elif event.action == ControlAction.TOUCH_DOUBLE and self.state.speaking:
                    old = self.turns.current
                    new = await self.turns.invalidate_current()
                    await self.bus.publish(BargeIn(turn_id=new, interrupted_turn_id=old, new_turn_id=new))
                    await self.bus.publish(ExpressionRequested(expression="surprised", duration_sec=0.5))
                elif event.action == ControlAction.TOUCH_TAP:
                    if self.state.current is AppState.SLEEPING:
                        await self.state.transition(AppState.IDLE, "touch wake", force=True)
                        self.mode = "companion"
                        await self.bus.publish(ModeChanged(mode=self.mode))
                    else:
                        await self.bus.publish(ExpressionRequested(expression="happy", duration_sec=0.8))
                elif event.action == ControlAction.JOY_PRESS and self.mode == "companion":
                    self.mode = "game"
                    await self.bus.publish(ModeChanged(mode=self.mode))

    async def _cycle_mode(self) -> None:
        modes = self.config.modes
        index = (modes.index(self.mode) + 1) % len(modes) if self.mode in modes else 0
        self.mode = modes[index]
        await self.bus.publish(ModeChanged(mode=self.mode))
        if self.mode == "sleep":
            await self.state.transition(AppState.SLEEPING, "button mode sleep", force=True)
        elif self.state.current is AppState.SLEEPING:
            await self.state.transition(AppState.IDLE, "button mode wake", force=True)

    async def _toggle_sleep(self) -> None:
        if self.state.current is AppState.SLEEPING:
            self.mode = "companion"
            await self.state.transition(AppState.IDLE, "long-press wake", force=True)
        else:
            self.mode = "sleep"
            await self.state.transition(AppState.SLEEPING, "long-press sleep", force=True)
        await self.bus.publish(ModeChanged(mode=self.mode))
