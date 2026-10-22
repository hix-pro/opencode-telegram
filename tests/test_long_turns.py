"""Long-turn behaviour: delivery, retries, heads-up, and recovery."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import replace

import pytest

from bridgekit.api import ApiError, OpenCode
from bridgekit.commands import CommandError, run
from bridgekit.config import Config
from bridgekit.keyboards import Router
from bridgekit.policy import Policy
from bridgekit.schedule import ScheduleStore
from bridgekit.state import State


class StubAPI:
    """Minimal OpenCode stand-in that records prompts and can fail on demand."""

    def __init__(self, fail_status: int = 0, failures: int = 0, on_404=None):
        self.prompts: list[dict] = []
        self.fail_status = fail_status
        self.failures = failures
        self.on_404 = on_404
        self.created: list[str] = []

    def send_prompt(self, session_id, text, files=None, delivery=None):
        self.prompts.append(
            {"session": session_id, "text": text, "delivery": delivery, "files": files}
        )
        if self.failures > 0:
            self.failures -= 1
            raise ApiError(self.fail_status, "boom")
        return None

    def create_session(self, directory, title=None, model=None, agent=None):
        self.created.append(directory)
        return {"id": "ses_recovered000000000"}

    def messages(self, session_id, limit=20, order="desc"):
        return []

    def interrupt_session(self, session_id):
        self.interrupted = session_id
        return {}

    def get_session(self, session_id):
        return {"id": session_id, "title": "t", "time": {}}

    def permission_requests(self, directory=None):
        return []


# ------------------------------------------------------------------ delivery


def test_send_prompt_sends_delivery_in_the_body(monkeypatch):
    captured = {}

    def fake_request(self, method, path, *, params=None, body=None, **kwargs):
        captured["body"] = body
        return {}

    monkeypatch.setattr(OpenCode, "_request", fake_request)
    api = OpenCode("http://x", "pw")

    api.send_prompt("s", "hi", delivery="queue")
    assert captured["body"]["delivery"] == "queue"

    api.send_prompt("s", "hi", delivery="steer")
    assert captured["body"]["delivery"] == "steer"

    api.send_prompt("s", "hi")
    assert "delivery" not in captured["body"]


def test_send_prompt_includes_files(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        OpenCode,
        "_request",
        lambda self, method, path, *, body=None, **kw: captured.update(body=body) or {},
    )
    OpenCode("http://x", "pw").send_prompt("s", "see this", files=[{"uri": "file:///a.png"}])
    assert captured["body"]["files"] == [{"uri": "file:///a.png"}]


def test_connection_failures_are_retried(monkeypatch):
    calls = []

    def fake_request(self, method, path, *, body=None, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            # A connection that was never established: safe to retry.
            raise ApiError(0, "Connection refused", connect_failed=True)
        return {}

    monkeypatch.setattr(OpenCode, "_request", fake_request)
    monkeypatch.setattr("bridgekit.api.time.sleep", lambda s: None)

    OpenCode("http://x", "pw").send_prompt("s", "hi", attempts=5)
    assert len(calls) == 3


def test_retries_stop_at_the_attempt_limit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        OpenCode,
        "_request",
        lambda self, *a, **k: (
            calls.append(1),
            (_ for _ in ()).throw(ApiError(0, "refused", connect_failed=True)),
        )[1],
    )
    monkeypatch.setattr("bridgekit.api.time.sleep", lambda s: None)

    with pytest.raises(ApiError):
        OpenCode("http://x", "pw").send_prompt("s", "hi", attempts=3)
    assert len(calls) == 3


def test_an_ambiguous_failure_is_not_retried(monkeypatch):
    """A connection dropped *after* the request was sent is ambiguous.

    Retrying would submit the prompt twice, so the agent does the work twice -
    two file edits, two deploys. Only connect failures are retried.
    """
    calls = []
    monkeypatch.setattr(
        OpenCode,
        "_request",
        lambda self, *a, **k: (
            calls.append(1),
            (_ for _ in ()).throw(ApiError(0, "connection reset")),
        )[1],
    )
    monkeypatch.setattr("bridgekit.api.time.sleep", lambda s: None)

    with pytest.raises(ApiError):
        OpenCode("http://x", "pw").send_prompt("s", "hi", attempts=5)
    assert len(calls) == 1, "an ambiguous failure must not be retried"


def test_client_errors_are_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(
        OpenCode,
        "_request",
        lambda self, *a, **k: (calls.append(1), (_ for _ in ()).throw(ApiError(400, "bad")))[
            1
        ],
    )
    monkeypatch.setattr("bridgekit.api.time.sleep", lambda s: None)

    with pytest.raises(ApiError):
        OpenCode("http://x", "pw").send_prompt("s", "hi", attempts=5)
    assert len(calls) == 1


# ------------------------------------------------------------------ /delivery


def test_delivery_reports_the_default(ctx):
    assert "queue" in run(ctx, "/delivery")


def test_delivery_can_be_changed(ctx):
    run(ctx, "/delivery steer")
    assert ctx.state.pref("42", "delivery") == "steer"


def test_delivery_rejects_nonsense(ctx):
    with pytest.raises(CommandError, match="Usage"):
        run(ctx, "/delivery sideways")


def test_status_shows_the_turn_cap(ctx):
    reply = run(ctx, "/status")
    assert "Delivery" in reply
    assert "Turn cap" in reply


# --------------------------------------------------------------- turn pump


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []
        self.edits: list[str] = []

    def send(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        return 42

    def edit(self, chat_id, message_id, text, reply_markup=None, force=False):
        self.edits.append(text)
        return True

    def get_updates(self, offset, timeout=30):
        return []

    def answer_callback(self, callback_id, text=""):
        pass

    def effective_interval(self):
        return 0.0


class StubEvents:
    last_error = ""
    connected = True
    frames = 0
    reconnects = 0

    def listen(self, session_id, callback):
        pass

    def unlisten(self, session_id):
        pass

    def stop(self):
        pass


class SlowAPI(StubAPI):
    """Reports no idle marker for a while, then finishes the turn."""

    def __init__(self, polls_before_idle=2, **kw):
        super().__init__(**kw)
        self.polls_before_idle = polls_before_idle
        self.polls = 0

    def messages(self, session_id, limit=20, order="desc"):
        """Timestamps must be newer than the turn's submitted_at to count."""
        self.polls += 1
        stamp = time.time() * 1000 + 1000
        if self.polls <= self.polls_before_idle:
            return [
                {
                    "type": "assistant",
                    "time": {"created": stamp},
                    "content": [{"type": "text", "text": "working on it"}],
                }
            ]
        return [{"type": "idle", "time": {"created": stamp}}]


def build_bridge(api, tmp_path, **overrides):
    """A Bridge wired to fakes, with threads never started."""
    import bridge as B

    config = Config(
        token="t",
        allowed_chats=frozenset({"42"}),
        opencode_url="http://x",
        password="p",
        default_dir=str(tmp_path),
        state_file=tmp_path / ".state.json",
        poll_messages=0.01,
        edit_interval=0.0,
        prompt_timeout=5,
        heads_up_after=0,
    )
    for key, value in overrides.items():
        config = replace(config, **{key: value})

    instance = B.Bridge.__new__(B.Bridge)
    instance.config = config
    instance.api = api
    instance.telegram = FakeTelegram()
    instance.state = State(config.state_file, config.default_dir)
    instance.state.load()
    instance.events = StubEvents()
    instance.policy = Policy()
    instance.work = queue.Queue()
    instance.stop_event = threading.Event()
    instance.aborts = {}
    instance.health = B.Health(streaming=False)
    instance.router = Router()
    instance._lock = threading.Lock()
    instance.schedules = ScheduleStore()
    return instance


def test_long_turn_reports_itself_before_giving_up(tmp_path):
    """The heads-up is what stops a chat looking dead during a long job."""
    api = SlowAPI(polls_before_idle=4)
    bridge = build_bridge(api, tmp_path)
    bridge.state.set_session("42", "ses_test")

    bridge._run_turn("42", "do a long job")

    assert "still working" in bridge.telegram.edits[-1]
    assert "working on it" in bridge.telegram.edits[-1]


def test_busy_marker_is_cleared_after_the_turn(tmp_path):
    api = SlowAPI(polls_before_idle=3)
    bridge = build_bridge(api, tmp_path)
    bridge.state.set_session("42", "ses_test")
    bridge._run_turn("42", "job")
    assert bridge.state.busy("42") is None


def test_busy_marker_is_recorded_during_the_turn(tmp_path):
    seen = {}
    holder = []

    class Watcher(SlowAPI):
        def messages(self, session_id, limit=20, order="desc"):
            seen["busy"] = holder[0].state.busy("42")
            return super().messages(session_id, limit, order)

    api = Watcher(polls_before_idle=2)
    holder.append(build_bridge(api, tmp_path))
    holder[0].state.set_session("42", "ses_test")
    holder[0]._run_turn("42", "job")

    assert seen["busy"] is not None
    assert seen["busy"]["session"] == "ses_test"


def test_hard_timeout_says_the_job_may_still_run(tmp_path):
    api = SlowAPI(polls_before_idle=10_000)
    bridge = build_bridge(api, tmp_path, prompt_timeout=1)
    bridge.state.set_session("42", "ses_test")
    bridge._run_turn("42", "endless job")
    final = bridge.telegram.edits[-1]
    assert "still running" in final or "gave up waiting" in final


def test_orphaned_turn_is_reported_on_startup(tmp_path):
    api = SlowAPI(polls_before_idle=0)
    bridge = build_bridge(api, tmp_path)
    bridge.state.set_session("42", "ses_test")
    bridge.state.set_busy("42", "ses_test", started=time.time() - 600)

    sent: list[str] = []
    bridge.send = lambda chat_id, text, markup=None: sent.append(text)
    bridge._report_orphaned_turns()

    assert sent and "restarted" in sent[0]
    assert bridge.state.busy("42") is None


def test_abort_stops_a_turn_promptly(tmp_path):
    api = SlowAPI(polls_before_idle=10_000)
    bridge = build_bridge(api, tmp_path, prompt_timeout=30)
    bridge.state.set_session("42", "ses_test")

    bridge.abort_flag("42").set()
    bridge._run_turn("42", "long job")

    assert "interrupted" in bridge.telegram.edits[-1]
