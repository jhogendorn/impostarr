"""In-memory ring buffer of recent log records, backing the log viewer's
`GET /api/v1/logs` endpoint.

Installed on the root `impostarr` logger (`logging.getLogger("impostarr")`)
by `main.create_app`, at level INFO, so it captures records from every
`impostarr.*` module logger (all of which use `getLogger(__name__)` and
propagate up to this ancestor). Bounded via `collections.deque(maxlen=...)`
so long-running processes don't grow this without limit — oldest records
are silently dropped once the buffer is full.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime
from typing import TypedDict

DEFAULT_CAPACITY = 1000

# Ascending severity, matching the stdlib's level names.
_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


class LogRecordDict(TypedDict):
    ts: str
    level: str
    logger: str
    message: str


class RingBufferHandler(logging.Handler):
    """Keeps the last `capacity` log records in memory as plain dicts."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        super().__init__()
        self.capacity = capacity
        self._buffer: deque[LogRecordDict] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        self._buffer.append(
            {
                "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
        )

    def get_logs(self, level: str | None = None, limit: int = 200) -> list[LogRecordDict]:
        """Records newest-last, optionally filtered to `level`-or-above,
        capped to the most recent `limit` (post-filter)."""
        records = list(self._buffer)
        if level is not None:
            min_order = _LEVEL_ORDER.get(level.upper(), 0)
            records = [r for r in records if _LEVEL_ORDER.get(r["level"], 0) >= min_order]
        return records[-limit:] if limit > 0 else []
