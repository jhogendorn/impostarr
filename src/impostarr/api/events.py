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
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[dict]] = []

    def subscribe(self) -> AsyncIterator[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
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
            queue.put_nowait(event)
