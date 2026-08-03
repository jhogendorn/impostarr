from __future__ import annotations

from impostarr.throttle import TokenBucket, in_active_hours

# -- in_active_hours ------------------------------------------------------


def test_none_active_hours_always_active():
    assert in_active_hours(None, 0) is True
    assert in_active_hours(None, 23) is True


def test_simple_daytime_window():
    assert in_active_hours("08-17", 8) is True
    assert in_active_hours("08-17", 17) is True
    assert in_active_hours("08-17", 12) is True
    assert in_active_hours("08-17", 7) is False
    assert in_active_hours("08-17", 18) is False


def test_overnight_wrap_window():
    # "22-06": active 22:00 through 06:59, inactive 07:00-21:59.
    assert in_active_hours("22-06", 22) is True
    assert in_active_hours("22-06", 23) is True
    assert in_active_hours("22-06", 0) is True
    assert in_active_hours("22-06", 6) is True
    assert in_active_hours("22-06", 7) is False
    assert in_active_hours("22-06", 21) is False


def test_single_hour_window():
    assert in_active_hours("03-03", 3) is True
    assert in_active_hours("03-03", 4) is False


# -- TokenBucket ------------------------------------------------------------


def test_none_jobs_per_hour_always_has_capacity():
    bucket = TokenBucket(None)
    for _ in range(50):
        assert bucket.has_capacity() is True
        bucket.consume()


def test_zero_jobs_per_hour_always_has_capacity():
    # 0 is treated the same as "unset" -- an explicit 0-per-hour limit
    # would just mean "never run", which `paused` already expresses more
    # clearly.
    bucket = TokenBucket(0)
    assert bucket.has_capacity() is True


def test_capacity_exhausted_after_limit_reached():
    bucket = TokenBucket(3)
    now = 1000.0
    for _ in range(3):
        assert bucket.has_capacity(now) is True
        bucket.consume(now)
    assert bucket.has_capacity(now) is False


def test_capacity_frees_up_after_rolling_window_elapses():
    bucket = TokenBucket(2)
    bucket.consume(now=1000.0)
    bucket.consume(now=1000.0)
    assert bucket.has_capacity(now=1000.0) is False
    # Just under an hour later: still counted.
    assert bucket.has_capacity(now=1000.0 + 3599) is False
    # Just over an hour later: the first two timestamps have aged out.
    assert bucket.has_capacity(now=1000.0 + 3601) is True


def test_consume_without_capacity_check_still_records():
    bucket = TokenBucket(1)
    bucket.consume(now=0.0)
    assert bucket.has_capacity(now=0.0) is False
