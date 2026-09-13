"""Small typed asyncio event bus with bounded queues and drop policies."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Generic, TypeVar

from .events import Event

LOG = logging.getLogger(__name__)
E = TypeVar("E", bound=Event)


@dataclass(slots=True)
class _Subscriber:
    event_type: type[Event]
    queue: asyncio.Queue[Event]
    drop_oldest: bool
    name: str


class Subscription(Generic[E]):
    """Async context manager around one subscriber queue."""

    def __init__(self, bus: "EventBus", sub: _Subscriber) -> None:
        self._bus = bus
        self._sub = sub

    async def get(self) -> E:
        return await self._sub.queue.get()  # type: ignore[return-value]

    def get_nowait(self) -> E:
        return self._sub.queue.get_nowait()  # type: ignore[return-value]

    @property
    def qsize(self) -> int:
        return self._sub.queue.qsize()

    async def __aenter__(self) -> "Subscription[E]":
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._bus.unsubscribe(self._sub)


class EventBus:
    """Broadcast events to typed subscribers without unbounded backlog growth."""

    def __init__(self) -> None:
        self._subscribers: list[_Subscriber] = []
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        event_type: type[E],
        *,
        maxsize: int = 64,
        drop_oldest: bool = True,
        name: str | None = None,
    ) -> Subscription[E]:
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        sub = _Subscriber(
            event_type=event_type,
            queue=asyncio.Queue(maxsize=maxsize),
            drop_oldest=drop_oldest,
            name=name or event_type.__name__,
        )
        async with self._lock:
            self._subscribers.append(sub)
        return Subscription(self, sub)

    def unsubscribe(self, sub: _Subscriber) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(sub)

    async def publish(self, event: Event) -> None:
        # Snapshot keeps publish latency independent from subscription churn.
        subscribers = tuple(self._subscribers)
        for sub in subscribers:
            if not isinstance(event, sub.event_type):
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                if not sub.drop_oldest:
                    LOG.warning(
                        "dropping new event for full subscriber queue",
                        extra={"event_id": event.event_id, "subscriber": sub.name},
                    )
                    continue
                with contextlib.suppress(asyncio.QueueEmpty):
                    sub.queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(event)

    def queue_depths(self) -> dict[str, int]:
        return {sub.name: sub.queue.qsize() for sub in tuple(self._subscribers)}
