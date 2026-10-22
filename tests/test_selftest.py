"""The self-test must be able to fail.

A check that reports PASS regardless is worse than no check at all: it turns
"everything is fine" into a claim the bridge cannot actually make. These tests
pin the failure reporting, not just the happy path.
"""

from __future__ import annotations

import tempfile

import pytest

from bridgekit import selftest
from bridgekit.selftest import Report, Result


def test_render_marks_passes_and_failures():
    report = Report(
        results=[
            Result("One", True, "fine"),
            Result("Two", False, "broke"),
        ]
    )
    rendered = report.render()
    assert "PASS  One  -  fine" in rendered
    assert "FAIL  Two  -  broke" in rendered
    assert "1 passed, 1 failed" in rendered


def test_render_when_everything_passes():
    report = Report(results=[Result("One", True)])
    assert "All 1 checks passed." in report.render()


def test_a_raising_check_becomes_a_failure():
    def boom():
        raise TimeoutError("the turn never finished")

    result = selftest._timed("Slow", boom)
    assert result.ok is False
    assert "TimeoutError" in result.detail
    assert "never finished" in result.detail


def test_a_check_reports_its_detail_on_success():
    result = selftest._timed("Fast", lambda: "1 delta")
    assert result.ok is True
    assert result.detail == "1 delta"


def test_duration_is_recorded():
    result = selftest._timed("Quick", lambda: None)
    assert result.seconds >= 0


def test_event_probe_fails_when_no_deltas_arrive(monkeypatch):
    """The event check must raise when nothing streams.

    Regression: the probe used to return a descriptive string while the stream
    was already stopped, so it could never fail and always said PASS.
    """
    from bridgekit import events as events_module

    class FakeStream:
        def __init__(self, base, auth):
            self.base, self.auth = base, auth
            self.connected = True
            self.frames = 3

        def listen(self, session_id, callback):
            return None

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(events_module, "EventStream", FakeStream)
    monkeypatch.setattr(selftest, "EVENT_TIMEOUT", 0.2)

    class FakeApi:
        base, auth = "http://x", None

        def send_prompt(self, session_id, prompt):
            return None

    with pytest.raises(AssertionError, match="no session.text.delta"):
        selftest._probe_events(FakeApi(), "ses_1")


def test_event_probe_fails_when_the_stream_will_not_connect(monkeypatch):
    from bridgekit import events as events_module

    class DeadStream:
        def __init__(self, base, auth):
            self.connected = False
            self.frames = 0

        def listen(self, session_id, callback):
            return None

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(events_module, "EventStream", DeadStream)
    monkeypatch.setattr(selftest, "CONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(selftest.time, "sleep", lambda _s: None)

    class FakeApi:
        base, auth = "http://x", None

    with pytest.raises(ConnectionError, match="could not connect"):
        selftest._probe_events(FakeApi(), "ses_1")


def test_run_cleans_up_its_session_even_when_a_check_fails():
    """A failed self-test must not leave sessions lying around."""
    deleted = []

    class FakeApi:
        base, auth = "http://x", None
        sessions = []

        def create_session(self, directory):
            self.sessions.append("ses_1")
            return {"id": "ses_1"}

        def delete_session(self, session_id):
            deleted.append(session_id)

        def send_prompt(self, session_id, prompt):
            raise RuntimeError("server went away")

    report = selftest.run(FakeApi(), tempfile.gettempdir())

    assert report.failed > 0
    assert deleted == ["ses_1"], "the throwaway session must be removed"
