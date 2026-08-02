"""Tiny in-process pub/sub for job-update / stats notifications.

One `EventBus` per app (`main.py` constructs it and hangs it off
`app.state.event_bus`; `PipelineDeps.event_bus` gets the same instance so
the worker pool can publish into the same bus the SSE route reads from).

`subscribe()` registers the subscriber's queue synchronously, before
returning the async generator — not inside the generator body on first
`__anext__`. That ordering matters: a caller that does
`sub = bus.subscribe()` is guaranteed to receive any event published after
that call returns, even if it hasn't started iterating `sub` yet. If queue
registration were deferred to the generator's first resumption, a publish
racing between `subscribe()` and the first `anext()` would be silently
dropped.

Each subscriber's queue is bounded (`MAX_QUEUE_SIZE`): a slow or stalled SSE
client (a stuck TCP connection, a browser tab backgrounded) must not let
its queue grow unboundedly while the rest of the process keeps running.
`publish()` drops the oldest queued event to make room rather than
blocking the publisher or dropping the new event — SSE consumers care
about current state more than a complete history, so keeping the freshest
events is the right trade-off here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

MAX_QUEUE_SIZE = 100


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict]] = []

    def subscribe(self) -> AsyncIterator[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._subscribers.append(queue)
        return self._iter_queue(queue)

    async def _iter_queue(self, queue: asyncio.Queue[dict]) -> AsyncIterator[dict]:
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.remove(queue)

    def publish(self, event: dict) -> None:
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dropped = queue.get_nowait()
                logger.debug("subscriber queue full (%d); dropping oldest event: %r", MAX_QUEUE_SIZE, dropped)
                queue.put_nowait(event)
