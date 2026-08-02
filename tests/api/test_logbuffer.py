from __future__ import annotations

import logging
import sys

from impostarr.api.logbuffer import RingBufferHandler


def make_record(logger_name: str, level: int, message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name=logger_name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_emit_captures_record_as_dict():
    handler = RingBufferHandler()
    handler.emit(make_record("impostarr.foo", logging.INFO, "hello %s"))

    logs = handler.get_logs()
    assert len(logs) == 1
    entry = logs[0]
    assert entry["level"] == "INFO"
    assert entry["logger"] == "impostarr.foo"
    assert entry["message"] == "hello %s"
    assert "ts" in entry
    assert entry["exc"] is None


def test_emit_captures_traceback_for_exc_info():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="impostarr.foo",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="it broke",
            args=(),
            exc_info=sys.exc_info(),
        )

    handler = RingBufferHandler()
    handler.emit(record)

    entry = handler.get_logs()[0]
    assert entry["message"] == "it broke"
    assert entry["exc"] is not None
    assert "ValueError: boom" in entry["exc"]


def test_emit_trims_traceback_to_last_n_lines():
    def level_5():
        raise ValueError("deep")

    def level_4():
        level_5()

    def level_3():
        level_4()

    def level_2():
        level_3()

    def level_1():
        level_2()

    try:
        level_1()
    except ValueError:
        record = logging.LogRecord(
            name="impostarr.foo",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="deep failure",
            args=(),
            exc_info=sys.exc_info(),
        )

    handler = RingBufferHandler()
    handler.emit(record)

    entry = handler.get_logs()[0]
    assert entry["exc"] is not None
    assert len(entry["exc"].splitlines()) <= 5
    assert "ValueError: deep" in entry["exc"]


def test_ring_bounds_at_capacity():
    handler = RingBufferHandler(capacity=5)
    for i in range(10):
        handler.emit(make_record("impostarr.foo", logging.INFO, f"msg-{i}"))

    logs = handler.get_logs(limit=1000)
    assert len(logs) == 5
    # oldest 5 dropped, newest 5 kept, in insertion order
    assert [entry["message"] for entry in logs] == [f"msg-{i}" for i in range(5, 10)]


def test_get_logs_filters_by_level_at_or_above():
    handler = RingBufferHandler()
    handler.emit(make_record("impostarr.foo", logging.INFO, "info msg"))
    handler.emit(make_record("impostarr.foo", logging.WARNING, "warn msg"))
    handler.emit(make_record("impostarr.foo", logging.ERROR, "error msg"))

    warning_and_up = handler.get_logs(level="WARNING")
    assert [entry["message"] for entry in warning_and_up] == ["warn msg", "error msg"]

    error_only = handler.get_logs(level="ERROR")
    assert [entry["message"] for entry in error_only] == ["error msg"]


def test_get_logs_default_returns_all_levels():
    handler = RingBufferHandler()
    handler.emit(make_record("impostarr.foo", logging.INFO, "info msg"))
    handler.emit(make_record("impostarr.foo", logging.ERROR, "error msg"))

    logs = handler.get_logs()
    assert len(logs) == 2


def test_get_logs_newest_last_and_respects_limit():
    handler = RingBufferHandler()
    for i in range(5):
        handler.emit(make_record("impostarr.foo", logging.INFO, f"msg-{i}"))

    logs = handler.get_logs(limit=2)
    assert [entry["message"] for entry in logs] == ["msg-3", "msg-4"]
