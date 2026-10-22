"""Second audit batch: correctness fixes from the independent review.

Grouped by the reviewer's finding numbers so it is obvious which regression
each test guards.
"""

from __future__ import annotations

import queue
import threading
import time

import pytest

from bridgekit.api import ApiError, OpenCode
from bridgekit.commands import run
from bridgekit.config import Config
from bridgekit.policy import Policy
from bridgekit.schedule import ScheduleStore
from bridgekit.state import State

CHAT = "42"


# ------------------------------------------------------------------- #16, #32


def test_invalid_regex_is_rejected_loudly():
    """It used to report success while protecting nothing."""
    with pytest.raises(ValueError):
        Policy().add_deny("(unclosed")


def test_guard_test_names_the_right_rule():
    """A bad pattern used to shift the deny/regex pairing."""
    policy = Policy()
    policy.deny.append("(this one is invalid")
    policy._compile()
    policy.deny.append(r"\bterraform\s+destroy\b")
    policy._compile()

    assert policy.blocked_command("terraform destroy") == r"\bterraform\s+destroy\b"


def test_duplicate_deny_is_not_stored_twice():
    policy = Policy()
    policy.add_deny(r"\bfoo\b")
    policy.add_deny(r"\bfoo\b")
    assert policy.deny.count(r"\bfoo\b") == 1


# ------------------------------------------------------------------- #25, #26


def test_health_reports_a_real_uptime(tmp_path):
    import bridge as B

    config = Config(
        token="t",
        allowed_chats=frozenset({CHAT}),
        opencode_url="http://x",
        password="p",
        default_dir=str(tmp_path),
        state_file=tmp_path / ".s.json",
    )
    bridge = B.Bridge.__new__(B.Bridge)
    bridge.config = config
    bridge.health = B.Health()
    bridge.health.started_at = time.time() - 90
    bridge.api = type("A", (), {"info": lambda self: {"version": "2.0.31", "pid": 1}})()
    bridge.state = State(config.state_file, str(tmp_path))
    bridge.state.load()
    bridge.policy = Policy()
    bridge.schedules = ScheduleStore()
    bridge.telegram = type(
        "T",
        (),
        {
            "edit_failures": 0,
            "rate_limited": 0,
            "effective_interval": lambda self: 2.0,
        },
    )()
    bridge.events = type(
        "E", (), {"connected": True, "frames": 5, "reconnects": 0}
    )()
    bridge._requests = {}
    bridge._backlog = {}
    bridge.aborts = {}
    bridge._workers = {}
    bridge._lock = threading.Lock()

    ctx = bridge.context(CHAT)
    reply = run(ctx, "/health")
    uptime_line = next(line for line in reply.split("\n") if "Uptime" in line)
    assert "90s" in uptime_line, uptime_line
    assert "n/a" not in uptime_line


def test_doctor_reports_a_dead_watcher(ctx):
    ctx.health = {
        "permissions_alive": False,
        "events": {"connected": True, "frames": 1, "reconnects": 0},
        "telegram": {"edit_failures": 0, "rate_limited": 0, "edit_interval": 2.0},
    }
    ctx.api.info = lambda: {"version": "2", "pid": 1}
    assert "Permission watcher" in run(ctx, "/doctor")


# ------------------------------------------------------------------- #13


def test_ambiguous_post_failure_is_not_retried(monkeypatch):
    """Retrying a POST that may have landed runs the agent's work twice."""
    calls = []
    monkeypatch.setattr(
        OpenCode,
        "_request",
        lambda self, *a, **k: (
            calls.append(1),
            (_ for _ in ()).throw(ApiError(0, "connection reset by peer")),
        )[1],
    )
    monkeypatch.setattr("bridgekit.api.time.sleep", lambda s: None)

    with pytest.raises(ApiError):
        OpenCode("http://x", "pw").send_prompt("s", "hi", attempts=5)
    assert len(calls) == 1


def test_connect_failures_are_still_retried(monkeypatch):
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


# ------------------------------------------------------------------- #17


def test_sh_does_not_block_the_poll_thread(ctx):
    """A long command used to freeze commands and /abort for the whole chat."""
    import bridgekit.commands as commands

    released = threading.Event()

    class SlowShell:
        def run_shell(self, command, cwd, directory=None):
            return {"id": "sh_1", "status": "running"}

        def shell_output(self, shell_id, directory=None):
            released.wait(timeout=3)
            return {"output": "late output", "status": "completed"}

    ctx.api = SlowShell()
    thread = threading.Thread(
        target=lambda: commands.run(ctx, "/sh sleep-forever"), daemon=True
    )
    thread.start()
    time.sleep(0.3)
    # The poll thread is free: another command still runs immediately.
    assert ctx.state.session(CHAT) is None
    released.set()
    thread.join(timeout=4)


# ------------------------------------------------------------------- #5


def test_queued_messages_are_held_not_dropped(tmp_path):
    import bridge as B
    from bridgekit.keyboards import Router

    class TG:
        edit_failures = 0
        rate_limited = 0

        def __init__(self):
            self.sent = []

        def send(self, chat_id, text, reply_markup=None):
            self.sent.append(text)
            return 1

        def effective_interval(self):
            return 2.0

    config = Config(
        token="t",
        allowed_chats=frozenset({CHAT}),
        opencode_url="http://x",
        password="p",
        default_dir=str(tmp_path),
        state_file=tmp_path / ".s.json",
    )
    b = B.Bridge.__new__(B.Bridge)
    b.config = config
    b.api = type("A", (), {"permission_requests": lambda self, d=None: []})()
    b.telegram = TG()
    b.state = State(config.state_file, str(tmp_path))
    b.state.load()
    b.policy = Policy()
    b.events = type(
        "E",
        (),
        {"listen": lambda *a: None, "unlisten": lambda *a: None, "stop": lambda self: None},
    )()
    b.router = Router()
    b.work = queue.Queue()
    b.stop_event = threading.Event()
    b.aborts = {}
    b._workers = {}
    b._requests = {}
    b._backlog = {}
    b.health = B.Health()
    b._lock = threading.Lock()

    blocker = threading.Event()
    b._workers[CHAT] = threading.Thread(target=blocker.wait, daemon=True)
    b._workers[CHAT].start()

    threading.Thread(target=b._worker_loop, daemon=True).start()
    b.work.put((CHAT, "do this later", None))
    time.sleep(0.3)

    assert len(b._backlog.get(CHAT, [])) == 1
    assert b._backlog[CHAT][0][1] == "do this later"
    assert any("Queued" in m for m in b.telegram.sent)
    b.stop_event.set()
    blocker.set()


# ------------------------------------------------------------------- #8


def test_watcher_survives_an_arbitrary_error(tmp_path):
    """Any exception used to kill auto-approval, prompts and pruning."""
    import bridge as B

    class Exploding:
        def __init__(self):
            self.calls = 0

        def permission_requests(self, directory=None):
            self.calls += 1
            raise RuntimeError("something nobody predicted")

    config = Config(
        token="t",
        allowed_chats=frozenset({CHAT}),
        opencode_url="http://x",
        password="p",
        default_dir=str(tmp_path),
        state_file=tmp_path / ".s.json",
        permission_poll=0.01,
    )
    b = B.Bridge.__new__(B.Bridge)
    b.config = config
    b.api = Exploding()
    b.telegram = None
    b.state = State(config.state_file, str(tmp_path))
    b.state.load()
    b.state.set_session(CHAT, "ses_x")
    b.policy = Policy()
    b.stop_event = threading.Event()
    b.health = B.Health()
    b._requests = {}

    thread = threading.Thread(target=b._permission_loop, daemon=True)
    thread.start()
    time.sleep(0.25)
    alive = thread.is_alive()
    calls = b.api.calls
    b.stop_event.set()
    thread.join(timeout=2)

    assert alive, "watcher exited on an unexpected error"
    assert calls >= 2


# ------------------------------------------------------------------- #9


def test_failed_notification_is_retried_next_poll(tmp_path):
    """Marking a request seen before the ask succeeded blocked the agent
    forever with the user never told."""
    import bridge as B

    REQUEST = {
        "id": "perm_x",
        "sessionID": "ses_x",
        "action": "read",
        "resources": ["a.py"],
    }

    class Ticker:
        def __init__(self):
            self.fail = True

        def send(self, chat_id, text, reply_markup=None):
            if self.fail:
                raise ConnectionError("network down")
            return 1

    class API:
        def permission_requests(self, directory=None):
            return [REQUEST]

        def reply_permission(self, *a, **k):
            return {}

    config = Config(
        token="t",
        allowed_chats=frozenset({CHAT}),
        opencode_url="http://x",
        password="p",
        default_dir=str(tmp_path),
        state_file=tmp_path / ".s.json",
        permission_poll=0.02,
        approval_mode="ask",
    )
    b = B.Bridge.__new__(B.Bridge)
    b.config = config
    b.api = API()
    b.telegram = Ticker()
    b.state = State(config.state_file, str(tmp_path))
    b.state.load()
    b.state.set_session(CHAT, "ses_x")
    b.policy = Policy(mode="ask")
    b.stop_event = threading.Event()
    b.health = B.Health()
    b._requests = {}

    thread = threading.Thread(target=b._permission_loop, daemon=True)
    thread.start()
    time.sleep(0.15)
    b.telegram.fail = False
    time.sleep(0.15)
    b.stop_event.set()
    thread.join(timeout=2)

    assert "perm_x" in b._requests, "the request must still be pending and known"
