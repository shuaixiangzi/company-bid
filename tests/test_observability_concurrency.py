"""Concurrent local JSONL/SDK failure boundaries; no external transport."""
from __future__ import annotations

import contextvars
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import company_bid.observability as module
from company_bid.observability import Observer, ObservabilityWriteError
from company_bid.storage import json_bytes


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_events_and_sdk_exports_serialize_whole_lines_and_retain_each_unit_context(tmp_path, monkeypatch):
    observer = Observer(tmp_path, "concurrent-run")
    active, maximum = 0, 0
    guard = threading.Lock()

    def split_append(path, value):
        # Deliberately split an append to expose concurrent JSONL writers;
        # correctness must come from the observer, not one OS write syscall.
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        try:
            raw = json_bytes(value) + b"\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab", buffering=0) as handle:
                handle.write(raw[:len(raw) // 2])
                time.sleep(0)
                handle.write(raw[len(raw) // 2:])
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(module, "append_json", split_append)
    try:
        with observer.span("node.N2") as parent:
            parent_id = format(parent.get_span_context().span_id, "016x")
            trace_id = format(parent.get_span_context().trace_id, "032x")

            def work(index):
                identity = f"unit-{index}"
                with observer.span("unit.N2", {"unit_id": identity}):
                    for attempt in range(6):
                        observer.event("unit.progress", {"unit_id": identity, "count": attempt,
                            "source_text": "PRIVATE_SOURCE_CANARY", "api_key": "PRIVATE_SECRET_CANARY"})

            # Scheduler context propagation is explicit in this test. Observer
            # only preserves that real context while synchronizing its sinks.
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(contextvars.copy_context().run, work, index) for index in range(8)]
                for future in futures:
                    future.result(timeout=5)
        observer.check()
    finally:
        observer.close()  # Workers and their unit spans have all drained.
    events, spans = rows(tmp_path / "events.jsonl"), rows(tmp_path / "spans.jsonl")
    assert len(events) == 48 and len(spans) == 9
    assert maximum == 1
    units = {span["attributes"]["unit_id"]: span for span in spans if span["name"] == "unit.N2"}
    assert set(units) == {f"unit-{index}" for index in range(8)}
    for event in events:
        unit = units[event["unit_id"]]
        assert event["span_id"] == unit["span_id"]
        assert event["trace_id"] == unit["trace_id"] == trace_id
        assert unit["parent_span_id"] == parent_id
    encoded = json.dumps(events) + json.dumps(spans)
    assert "PRIVATE_SOURCE_CANARY" not in encoded and "PRIVATE_SECRET_CANARY" not in encoded


def test_concurrent_event_failure_is_sticky_without_becoming_a_trace_export_failure(tmp_path, monkeypatch):
    observer = Observer(tmp_path, "event-failure")
    original = module.append_json

    def fail_one(path, value):
        if path.name == "events.jsonl" and value.get("unit_id") == "bad":
            raise OSError("PRIVATE_FAILURE_CANARY")
        return original(path, value)

    monkeypatch.setattr(module, "append_json", fail_one)
    with ThreadPoolExecutor(max_workers=2) as pool:
        bad = pool.submit(observer.event, "unit.progress", {"unit_id": "bad"})
        good = pool.submit(observer.event, "unit.progress", {"unit_id": "good"})
        with pytest.raises(ObservabilityWriteError) as error:
            bad.result(timeout=5)
        good.result(timeout=5)
    assert "PRIVATE_FAILURE_CANARY" not in str(error.value)
    assert observer.event_error == "EVENT_WRITE_FAILED"
    assert observer.export_error is None and observer.trace_status == "captured"
    with observer.span("after.event.failure"):
        observer.event("unit.progress", {"unit_id": "later"})
    observer.close()
    with pytest.raises(ObservabilityWriteError, match="event"):
        observer.check()
    assert {row["unit_id"] for row in rows(tmp_path / "events.jsonl")} == {"good", "later"}
    assert observer.trace_status == "captured"


@pytest.mark.parametrize("stream", ["event", "span"])
def test_check_cannot_pass_a_concurrent_write_before_its_failure_is_latched(tmp_path, monkeypatch, stream):
    observer = Observer(tmp_path, "check-failure")
    entered, release, checking = threading.Event(), threading.Event(), threading.Event()

    def failing_append(path, value):
        entered.set()
        assert release.wait(timeout=3)
        raise OSError("synthetic write failure")

    monkeypatch.setattr(module, "append_json", failing_append)

    def check():
        checking.set()
        observer.check()

    def write():
        if stream == "event":
            observer.event("unit.progress")
        else:
            with observer.span("unit.N2"):
                pass

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            writing = pool.submit(write)
            assert entered.wait(timeout=3)
            reading = pool.submit(check)
            assert checking.wait(timeout=3)
            assert not reading.done()
            release.set()
            if stream == "event":
                with pytest.raises(ObservabilityWriteError):
                    writing.result(timeout=3)
            else:
                writing.result(timeout=3)  # The SDK exporter reports FAILURE.
            with pytest.raises(ObservabilityWriteError):
                reading.result(timeout=3)
    finally:
        release.set()
        observer.close()


def test_span_export_failure_survives_later_success_and_close_failures(tmp_path, monkeypatch):
    observer = Observer(tmp_path, "span-failure")
    append = module.append_json

    def failing_spans(path, value):
        if path.name == "spans.jsonl":
            raise OSError("private span error")
        return append(path, value)

    monkeypatch.setattr(module, "append_json", failing_spans)
    with ThreadPoolExecutor(max_workers=2) as pool:
        def span(index):
            with observer.span("unit.N2", {"unit_id": f"u{index}"}):
                observer.event("unit.progress", {"unit_id": f"u{index}"})
        for future in [pool.submit(span, index) for index in range(2)]:
            future.result(timeout=5)
    assert observer.trace_status == "write_failed" and observer.export_error == "TRACE_WRITE_FAILED"
    shutdown = observer.provider.shutdown
    monkeypatch.setattr(observer.provider, "force_flush", lambda: False)
    monkeypatch.setattr(observer.provider, "shutdown", lambda: (_ for _ in ()).throw(OSError("private shutdown")))
    try:
        observer.close()
        observer.close()
        assert observer.export_error == "TRACE_WRITE_FAILED"
        with pytest.raises(ObservabilityWriteError, match="span"):
            observer.check()
    finally:
        shutdown()


@pytest.mark.parametrize("failure,expected", [
    ("flush_false", "TRACE_FLUSH_FAILED"), ("flush_raise", "TRACE_FLUSH_FAILED"),
    ("shutdown_raise", "TRACE_SHUTDOWN_FAILED"), ("both_raise", "TRACE_FLUSH_FAILED"),
])
def test_close_failures_are_safe_sticky_and_always_attempt_shutdown(tmp_path, monkeypatch, failure, expected):
    observer = Observer(tmp_path, "close-failure")
    shutdown = observer.provider.shutdown
    calls = []

    def flush():
        calls.append("flush")
        if failure in {"flush_raise", "both_raise"}:
            raise OSError("private flush error")
        return failure != "flush_false"

    def stop():
        calls.append("shutdown")
        if failure in {"shutdown_raise", "both_raise"}:
            raise OSError("private shutdown error")

    monkeypatch.setattr(observer.provider, "force_flush", flush)
    monkeypatch.setattr(observer.provider, "shutdown", stop)
    try:
        observer.close()
        observer.close()
        assert calls == ["flush", "shutdown"]
        assert observer.trace_status == "write_failed" and observer.export_error == expected
        with pytest.raises(ObservabilityWriteError) as error:
            observer.check()
        assert "private" not in str(error.value)
    finally:
        shutdown()


def test_close_does_not_hold_the_write_lock_while_sdk_callbacks_finish(tmp_path, monkeypatch):
    observer = Observer(tmp_path, "flush-callback")
    finished = threading.Event()
    threads = []

    def flush():
        def callback():
            with observer.span("sdk.flush.callback"):
                pass  # Ending this SDK span synchronously uses DurableExporter.
            finished.set()
        thread = threading.Thread(target=callback, daemon=True)
        threads.append(thread)
        thread.start()
        return finished.wait(timeout=3)

    monkeypatch.setattr(observer.provider, "force_flush", flush)
    observer.close()
    for thread in threads:
        thread.join(timeout=3)
    assert finished.is_set() and all(not thread.is_alive() for thread in threads)
    observer.check()
    assert rows(tmp_path / "spans.jsonl")[0]["name"] == "sdk.flush.callback"


@pytest.mark.parametrize("interrupted", ["flush", "shutdown"])
def test_close_interrupt_is_not_swallowed_and_still_attempts_shutdown(tmp_path, monkeypatch, interrupted):
    observer = Observer(tmp_path, "close-interrupted")
    shutdown = observer.provider.shutdown
    calls = []

    def flush():
        calls.append("flush")
        if interrupted == "flush":
            raise KeyboardInterrupt
        return True

    def stop():
        calls.append("shutdown")
        if interrupted == "shutdown":
            raise KeyboardInterrupt

    monkeypatch.setattr(observer.provider, "force_flush", flush)
    monkeypatch.setattr(observer.provider, "shutdown", stop)
    try:
        with pytest.raises(KeyboardInterrupt):
            observer.close()
        observer.close()
        assert calls == ["flush", "shutdown"]
        assert observer.export_error == ("TRACE_FLUSH_FAILED" if interrupted == "flush" else "TRACE_SHUTDOWN_FAILED")
        with pytest.raises(ObservabilityWriteError):
            observer.check()
    finally:
        shutdown()


def test_concurrent_close_is_idempotent_and_late_events_are_rejected(tmp_path, monkeypatch):
    observer = Observer(tmp_path, "concurrent-close")
    shutdown = observer.provider.shutdown
    entered, release = threading.Event(), threading.Event()
    calls = []

    def flush():
        calls.append("flush")
        entered.set()
        assert release.wait(timeout=3)
        return True

    def stop():
        calls.append("shutdown")

    monkeypatch.setattr(observer.provider, "force_flush", flush)
    monkeypatch.setattr(observer.provider, "shutdown", stop)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(observer.close)
            assert entered.wait(timeout=3)
            second = pool.submit(observer.close)
            with pytest.raises(ObservabilityWriteError, match="closing"):
                observer.event("unit.late")
            release.set()
            first.result(timeout=3)
            second.result(timeout=3)
        assert calls == ["flush", "shutdown"]
        with pytest.raises(ObservabilityWriteError, match="closed"):
            observer.event("unit.late")
        assert not (tmp_path / "events.jsonl").exists()
        observer.check()
    finally:
        release.set()
        shutdown()
