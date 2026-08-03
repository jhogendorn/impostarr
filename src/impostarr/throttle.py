"""Worker-pool throttling: active-hours window, jobs/hour rate limiting.

`Settings.throttle` (see `config.ThrottleConfig`) supplies these; `paused`
is additionally flippable at runtime (`POST /api/v1/pause`/`resume`,
mutating the shared `ThrottleConfig` instance in place rather than the
config file) -- see `api/routes.py` and `worker.py`'s `_worker_loop`.
"""

from __future__ import annotations

import time


def in_active_hours(active_hours: str | None, now_hour: int) -> bool:
    """`active_hours` is an "HH-HH" hour range (0-23, each bound
    inclusive; both hours evaluated in UTC by the caller), possibly
    wrapping past midnight (e.g. "22-06" -- active 22:00 through 06:59).
    `None`/empty means always active."""
    if not active_hours:
        return True
    start_s, end_s = active_hours.split("-", 1)
    start, end = int(start_s), int(end_s)
    if start <= end:
        return start <= now_hour <= end
    return now_hour >= start or now_hour <= end


class TokenBucket:
    """Rate limiter shared across an entire worker pool: at most
    `jobs_per_hour` job claims may be consumed within any rolling
    60-minute window. Implemented as a sliding-window timestamp log rather
    than a classic continuously-refilling bucket -- simpler, and gives an
    exact "at most N in any trailing hour" guarantee rather than allowing
    a burst up to capacity. `jobs_per_hour` of `None` or `0` disables the
    limit (unlimited capacity)."""

    WINDOW_S = 3600.0

    def __init__(self, jobs_per_hour: int | None) -> None:
        self.jobs_per_hour = jobs_per_hour
        self._timestamps: list[float] = []

    def _prune(self, now: float) -> None:
        cutoff = now - self.WINDOW_S
        self._timestamps = [t for t in self._timestamps if t > cutoff]

    def has_capacity(self, now: float | None = None) -> bool:
        if not self.jobs_per_hour:
            return True
        now = now if now is not None else time.monotonic()
        self._prune(now)
        return len(self._timestamps) < self.jobs_per_hour

    def consume(self, now: float | None = None) -> None:
        """Records one claim. Callers should only call this after an
        actual job claim succeeds, not merely because `has_capacity()`
        allowed an attempt -- an empty queue shouldn't burn a token."""
        now = now if now is not None else time.monotonic()
        self._prune(now)
        self._timestamps.append(now)
