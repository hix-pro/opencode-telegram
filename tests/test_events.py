"""Event stream dispatch.

The HTTP layer is exercised through a fake ``requests.get`` so the reconnect
and parse logic is tested without a server.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from bridgekit import events


class FakeResponse:
    """Mirrors requests.Response closely enough, including the context manager
    the event stream now relies on to release the socket."""

    def __init__(self, lines, status=200):
        self._lines = lines
        self.status_code = status
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("http error")

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


@pytest.fixture
def stream():
    return events.EventStream("http://127.0.0.1:1", ("opencode", "pw"))


def frame(kind: str, session: str = "s1", **data):
    return "data:" + json.dumps({"type": kind, "data": {"sessionID": session, **data}})


def test_dispatch_routes_to_the_right_session(stream):
    got = []
    stream.listen("s1", lambda kind, event: got.append(kind))
    stream._dispatch(json.loads(frame("session.text.delta")[5:]))
    assert got == ["session.text.delta"]


def test_listeners_only_receive_their_own_session(stream):
    mine, theirs = [], []
    stream.listen("s1", lambda kind, event: mine.append(event))
    stream.listen("s2", lambda kind, event: theirs.append(event))
    stream._dispatch(json.loads(frame("session.text.delta", "s1")[5:]))
    assert len(mine) == 1 and not theirs


def test_unlisten_stops_delivery(stream):
    got = []
    stream.listen("s1", lambda kind, event: got.append(kind))
    stream.unlisten("s1")
    stream._dispatch(json.loads(frame("session.text.delta")[5:]))
    assert got == []


def test_a_failing_listener_does_not_break_the_stream(stream):
    good = []

    def bad(kind, event):
        raise RuntimeError("boom")

    stream.listen("s1", bad)
    stream.listen("s1", lambda kind, event: good.append(kind))
    stream._dispatch(json.loads(frame("session.text.delta")[5:]))
    assert good == ["session.text.delta"]


def test_session_id_falls_back_to_the_durable_aggregate(stream):
    got = []
    stream.listen("s9", lambda kind, event: got.append(kind))
    event = {
        "type": "session.execution.succeeded",
        "data": {},
        "durable": {"aggregateID": "s9"},
    }
    stream._dispatch(event)
    assert got == ["session.execution.succeeded"]


def test_health_counters_move(stream):
    stream._dispatch(json.loads(frame("session.text.delta")[5:]))
    assert stream.frames == 1
    assert stream.last_event_at > 0


def test_malformed_frames_are_ignored(monkeypatch, stream):
    frames = [
        ": comment",
        "event: ping",
        "data: {not json",
        "data: ",
        frame("session.text.delta"),
    ]
    monkeypatch.setattr(events.requests, "get", lambda *a, **k: FakeResponse(frames))
    got = []
    stream.listen("s1", lambda kind, event: got.append(kind))
    stream._consume()
    assert got == ["session.text.delta"]


def test_reconnect_backoff_is_capped(monkeypatch):
    sleeps = []
    monkeypatch.setattr(events.time, "sleep", lambda s: sleeps.append(s))

    stream = events.EventStream("http://127.0.0.1:1", ("opencode", "pw"))

    def always_fail(*args, **kwargs):
        raise events.requests.ConnectionError("refused")

    monkeypatch.setattr(events.requests, "get", always_fail)
    stream._stop = _FakeStop(after=4)
    stream._run()

    # The fake stop event trips after four checks: two per iteration.
    assert sleeps == [1.0, 2.0]
    assert stream.reconnects == 2
    assert stream.connected is False


class _FakeStop:
    def __init__(self, after):
        self.count = 0
        self.after = after

    def is_set(self):
        self.count += 1
        return self.count > self.after

    def set(self):
        pass

    def wait(self, timeout=None):
        return False


def test_repeated_drops_warn_once_then_go_quiet(monkeypatch, caplog):
    """A server outage must not flood the log: one warning, then DEBUG detail.

    During the outage every reconnect attempt used to log a WARNING with a
    multi-line exception dump, 2 per minute, forever.
    """
    sleeps = []
    monkeypatch.setattr(events.time, "sleep", lambda s: sleeps.append(s))

    stream = events.EventStream("http://127.0.0.1:1", ("opencode", "pw"))

    def always_fail(*args, **kwargs):
        raise events.requests.ConnectionError("refused")

    monkeypatch.setattr(events.requests, "get", always_fail)
    stream._stop = _FakeStop(after=16)  # eight attempts: two checks each
    with caplog.at_level(events.logging.DEBUG, logger="bridge.events"):
        stream._run()

    warnings = [r for r in caplog.records if r.levelno == events.logging.WARNING]
    assert len(warnings) == 1, "exactly one warning for a short outage"
    assert stream.reconnects == 8
    assert stream.drops == 8
    assert "refused" in stream.last_error
    debug_notes = [r for r in caplog.records if r.levelno == events.logging.DEBUG]
    assert len(debug_notes) == 7, "consecutive drops are counted, not lost"


def test_reconnected_stream_reports_recovery(monkeypatch, caplog):
    """A successful reconnect logs with the failure count and resets the tally."""
    monkeypatch.setattr(events.time, "sleep", lambda s: None)

    stream = events.EventStream("http://127.0.0.1:1", ("opencode", "pw"))
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise events.requests.ConnectionError("refused")
        return FakeResponse([": keepalive"])

    monkeypatch.setattr(events.requests, "get", flaky)
    stream._stop = _FakeStop(after=4)  # two attempts
    with caplog.at_level(events.logging.INFO, logger="bridge.events"):
        stream._run()

    messages = [r.getMessage() for r in caplog.records]
    assert any("after 1 failed attempt" in m for m in messages)
    assert stream.drops == 0
    assert stream.last_error == ""


def test_start_is_idempotent(monkeypatch, stream):
    started = []
    release = threading.Event()
    monkeypatch.setattr(stream, "_run", lambda: (started.append(1), release.wait(2)))
    stream.start()
    time.sleep(0.05)
    stream.start()
    release.set()
    time.sleep(0.05)
    assert len(started) == 1
    stream.stop()
