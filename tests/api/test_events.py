from __future__ import annotations

import asyncio
import logging

import pytest

from impostarr.api.events import MAX_QUEUE_SIZE, EventBus


@pytest.mark.asyncio
async def test_publish_then_subscribe_receives_event():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish({"type": "job_update", "job_id": 1, "status": "matched"})

    event = await asyncio.wait_for(sub.__anext__(), timeout=1)

    assert event == {"type": "job_update", "job_id": 1, "status": "matched"}
    await sub.aclose()


@pytest.mark.asyncio
async def test_publish_drops_oldest_event_when_queue_full(caplog):
    bus = EventBus()
    sub = bus.subscribe()

    with caplog.at_level(logging.DEBUG, logger="impostarr.api.events"):
        for i in range(MAX_QUEUE_SIZE + 1):
            bus.publish({"seq": i})

    assert any("dropping oldest event" in r.message for r in caplog.records)

    received = [await asyncio.wait_for(sub.__anext__(), timeout=1) for _ in range(MAX_QUEUE_SIZE)]

    # Event 0 (the oldest) was dropped to make room for event MAX_QUEUE_SIZE;
    # events 1..MAX_QUEUE_SIZE survive, in order.
    assert received == [{"seq": i} for i in range(1, MAX_QUEUE_SIZE + 1)]
    await sub.aclose()


@pytest.mark.asyncio
async def test_disconnect_cleanup_shrinks_subscriber_set():
    bus = EventBus()
    sub = bus.subscribe()
    assert len(bus._subscribers) == 1

    # Mirror how routes.py actually uses a subscription: at least one
    # `__anext__()` before the generator is ever closed (an async
    # generator's `finally` doesn't run on `aclose()` if its body never
    # started — nothing would be there to close). Publish + consume once so
    # the generator is genuinely suspended mid-body, like a live SSE
    # connection, before simulating the client disconnecting.
    bus.publish({"seq": 0})
    await asyncio.wait_for(sub.__anext__(), timeout=1)

    await sub.aclose()

    assert len(bus._subscribers) == 0
