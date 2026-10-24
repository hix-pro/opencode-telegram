"""Regressions from the second reliability audit.

Every test here failed before the fix it describes. Grouped by the failure
mode rather than the function, because the failure mode is what the chat saw:
a dispatcher that stopped dispatching, an interrupt that went silent, an
approval aimed at the wrong request.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import bridge as bridge_module
from bridgekit import attachments, commands, watchers
from bridgekit.api import ApiError
from bridgekit.config import Config
from bridgekit.keyboards import Router
from bridgekit.policy import Policy
from bridgekit.schedule import ScheduleStore
from bridgekit.state import State
from bridgekit.telegram import Telegram, TelegramConflictError, TelegramError, split_message

CHAT = "42"

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- fixtures


class _API:
    """The slice of OpenCode each test actually exercises."""

    def permission_requests(self, directory=None):
        return []

    def reply_permission(self, *args, **kwargs):
        return {}

    def interrupt_session(self, session_id):
        return {}


class _Telegram:
    """A chat-side double that records what was sent."""

    def __init__(self):
        self.sent: list[str] = []
        self.edit_failures = 0
        self.rate_limited = 0
        self.edit_interval = 1.0

    def send(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        return 1

    def redact(self, value):
        return str(value)

    def effective_interval(self):
        return self.edit_interval


def build(tmp_path, api=None, telegram=None, **overrides):
    import bridge as B

    config = Config(
        token="t",
        allowed_chats=frozenset({CHAT}),
        opencode_url="http://x",
        password="p",
        default_dir=str(tmp_path),
        state_file=tmp_path / ".state.json",
        poll_messages=0.01,
        edit_interval=0.0,
        stream_enabled=False,
        prompt_timeout=10,
    )
    for key, value in overrides.items():
        config = config.__class__(**{**config.__dict__, key: value})

    b = B.Bridge.__new__(B.Bridge)
    b.config = config
    b.api = api or _API()
    b.telegram = telegram or _Telegram()
    b.state = State(config.state_file, config.default_dir)
    b.state.load()
    b.policy = Policy()
    b.events = SimpleNamespace(connected=False, frames=0, reconnects=0, last_error="")
    b.router = Router()
    b.work = queue.Queue()
    b.stop_event = threading.Event()
    b.aborts = {}
    b._workers = {}
    b.schedules = ScheduleStore()
    b._requests = {}
    b._perm_cards = {}
    b._backlog = {}
    b.health = B.Health(streaming=False)
    b.fatal = None
    b._lock = threading.Lock()
    b._register_callbacks()
    b.state.set_session(CHAT, "ses_x")
    return b


def message(text):
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": int(CHAT)},
            "from": {"id": int(CHAT), "is_bot": False},
            "date": 1,
            "text": text,
        },
    }


# ================================================================
# A 200 body that is not a JSON object used to kill the calling thread
# ================================================================


class _Resp:
    def __init__(self, body, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class _Session:
    def __init__(self, get_resp=None, post_resp=None):
        self._get = get_resp
        self._post = post_resp

    def get(self, *args, **kwargs):
        return self._get

    def post(self, *args, **kwargs):
        return self._post


@pytest.mark.parametrize("body", [None, [], "around the world", 5])
def test_a_non_object_getupdates_body_is_an_outage_not_a_crash(body):
    """A captive portal answering 200/null used to escape as AttributeError
    through `body.get`, taking the poll thread with it."""
    tg = Telegram("98765")
    tg._http = _Session(get_resp=_Resp(body))
    with pytest.raises(TelegramError):
        tg.get_updates(0, timeout=1)


def test_a_non_object_sendmessage_body_raises_a_telegram_error():
    """`bridge.send` catches TelegramError; AttributeError used to escape
    everything and could park the whole turn queue."""
    tg = Telegram("98765")
    tg._http = _Session(post_resp=_Resp(None))
    with pytest.raises(TelegramError):
        tg._send_chunk(CHAT, "hi")

    tg._http = _Session(post_resp=_Resp({"result": {"message_id": 9}}))
    assert tg._send_chunk(CHAT, "hi") == 9, "a well-formed body still works"


def test_a_non_object_getfile_body_reports_cleanly(tmp_path):
    tg = Telegram("98765")
    tg._http = _Session(get_resp=_Resp(None))
    with pytest.raises(TelegramError):
        tg.download("file_x", tmp_path / "f.bin")


def test_only_a_true_poll_conflict_is_fatal(tmp_path, monkeypatch):
    """A second poller is permanent for this process; a malformed body is an
    outage. Splitting the exception keeps the conflict fatal and the rest
    retryable."""
    bridge = build(tmp_path)

    class Conflicting:
        def get_updates(self, offset, timeout=30):
            bridge.stop_event.set()
            raise TelegramConflictError(
                "Conflict: another instance is polling this bot token."
            )

    bridge.telegram = Conflicting()
    bridge.poll_loop()
    assert "Conflict" in (bridge.fatal or "")

    class Portal:
        def __init__(self):
            self.fail = True
            self.stopper = None

        def get_updates(self, offset, timeout=30):
            if self.fail:
                self.fail = False
                raise TelegramError("getUpdates returned a malformed body (NoneType)")
            if self.stopper is not None:
                self.stopper.set()
            return []

    bridge = build(tmp_path)
    bridge.telegram = Portal()
    bridge.telegram.stopper = bridge.stop_event
    slept: list[float] = []
    monkeypatch.setattr(bridge_module.time, "sleep", slept.append)
    bridge.poll_loop()

    assert slept == [5], "a malformed poll body backs off like an outage"
    assert bridge.fatal is None


# ================================================================
# Lone surrogates must not turn delivery into "the turn failed"
# ================================================================


def test_lone_surrogates_are_replaced_and_emoji_survive():
    from bridgekit.telegram import sanitize

    assert sanitize("a\udc80b") == "a\ufffdb"
    assert sanitize("emoji 😀 stays") == "emoji 😀 stays"


def test_utf16_counting_never_raises():
    from bridgekit.telegram import _utf16_len

    assert isinstance(_utf16_len("\udc80"), int)


def test_a_surrogate_in_a_long_reply_does_not_kill_delivery():
    """The failure needed the split path: over-limit text containing a lone
    surrogate used to raise UnicodeEncodeError, and the blanket handler
    reported the whole turn as failed."""
    payloads: list[str] = []

    class Capture:
        def post(self, url, json=None, **kwargs):
            payloads.append(json["text"])
            return _Resp({"result": {"message_id": 3}})

    tg = Telegram("98765", max_chunk=100)
    tg._http = Capture()
    tg.send(CHAT, "reply " + "x" * 300 + "\udc83")

    assert payloads, "the reply was delivered in chunks"
    assert not any(
        "\ud800" <= character <= "\udfff" for payload in payloads for character in payload
    ), "a lone surrogate may never leave the bridge"


def test_split_message_itself_stays_total():
    """Sanitization lives at the send boundary; splitting must simply never
    raise on whatever the agent produced."""
    chunks = split_message("y" * 5000 + "\udc80", 100)
    assert chunks
    assert "".join(chunks) == "y" * 5000 + "\udc80"


# ================================================================
# The turn dispatcher has no watchdog, so it must never die
# ================================================================


def test_the_turn_dispatcher_survives_a_send_failure(tmp_path):
    """One bad send (a proxy exploding) used to end the dispatcher thread
    permanently: every queued prompt parked forever while /status worked."""
    bridge = build(tmp_path)
    state = {"fail": True}
    real_send = bridge.send

    def flaky(chat_id, text, markup=None):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("middlebox exploded")
        real_send(chat_id, text, markup)

    bridge.send = flaky
    # Force the busy path so no real turn thread is needed for the test.
    bridge._workers[CHAT] = threading.current_thread()

    thread = threading.Thread(target=bridge._worker_loop, daemon=True)
    thread.start()
    bridge.work.put((CHAT, "first", None))
    bridge.work.put((CHAT, "second", None))
    for _ in range(200):
        if bridge.work.unfinished_tasks == 0:
            break
        time.sleep(0.02)
    bridge.stop_event.set()
    thread.join(timeout=3)

    assert bridge.work.unfinished_tasks == 0, "both items were processed once"
    assert not thread.is_alive(), "the dispatcher must still be dispatching"
    assert state["fail"] is False
    assert any("Queued" in text for text in bridge.telegram.sent), (
        "the second message was still queued and acknowledged"
    )


# ================================================================
# Abort intent survives a refused interrupt
# ================================================================


class _InterruptAPI(_API):
    def __init__(self, fail=False):
        self.interrupts: list[str] = []
        self.fail = fail

    def interrupt_session(self, session_id):
        self.interrupts.append(session_id)
        if self.fail:
            raise ApiError(500, "server busy")
        return {}


def test_a_refused_interrupt_keeps_the_abort_intent(tmp_path):
    """The next ordinary message used to wipe the flag, quietly abandoning
    the abort the user had just asked for."""
    bridge = build(tmp_path, api=_InterruptAPI(fail=True))
    bridge._workers[CHAT] = threading.current_thread()

    line = bridge_module.updates.stop_turn(bridge, CHAT)
    assert line.startswith("! Could not interrupt")
    assert bridge.abort_flag(CHAT).is_set()

    # A busy chat keeps its abort intent; the pump still honours the flag.
    bridge._handle_update(message("hold on, let it finish"))
    assert bridge.abort_flag(CHAT).is_set(), (
        "an ordinary message must not cancel a standing abort"
    )

    # Once nothing runs any more the stale intent can go.
    with bridge._lock:
        bridge._workers.pop(CHAT)
    bridge._handle_update(message("anyone there?"))
    assert not bridge.abort_flag(CHAT).is_set()


def test_typed_abort_shares_the_reaction_stop_path(tmp_path):
    """/abort used to answer "Interrupt sent." unconditionally and leave the
    pump out of the loop."""
    api = _InterruptAPI()
    bridge = build(tmp_path, api=api)
    bridge._workers[CHAT] = threading.current_thread()
    ctx = bridge.context(CHAT)

    reply = commands.run(ctx, "/abort")
    assert reply == "Stopping - the turn is being interrupted."
    assert api.interrupts == ["ses_x"]
    assert bridge.abort_flag(CHAT).is_set()

    # The failure path reports through the command too, and keeps retrying.
    bridge.abort_flag(CHAT).clear()
    api.fail = True
    reply = commands.run(ctx, "/abort")
    assert reply.startswith("! Could not interrupt")
    assert bridge.abort_flag(CHAT).is_set(), "the flag stays armed for the pump"

    # And the 👎 reaction reports the same outcome as the typed command
    # would on the same turn.
    bridge.abort_flag(CHAT).clear()
    api.fail = False
    bridge._handle_update(
        {
            "update_id": 2,
            "message_reaction": {
                "chat": {"id": int(CHAT)},
                "message_id": 5,
                "user": {"id": int(CHAT)},
                "date": 1,
                "new_reaction": [{"emoji": "👎"}],
                "old_reaction": [],
            },
        }
    )
    assert bridge.telegram.sent and bridge.telegram.sent[-1] == (
        "Stopping - the turn is being interrupted."
    ), "the reaction path must report like the command"


def test_abort_on_an_idle_chat_leaves_nothing_armed(tmp_path):
    bridge = build(tmp_path)
    ctx = bridge.context(CHAT)
    reply = commands.run(ctx, "/abort")
    assert reply == "Nothing is running."
    assert not bridge.abort_flag(CHAT).is_set()


# ================================================================
# The watcher must retry a failed auto-answer and evict only what resolved
# ================================================================


class _RetryAPI(_API):
    def __init__(self):
        self.attempts = 0
        self.permissions = [{"id": "perm_r", "sessionID": "ses_x", "action": "read"}]
        self.replies: list[str] = []

    def permission_requests(self, directory=None):
        return self.permissions

    def reply_permission(self, session_id, request_id, decision, message=None):
        self.attempts += 1
        if self.attempts <= 2:
            raise ApiError(502, "bad gateway")
        self.permissions = [p for p in self.permissions if p["id"] != request_id]
        self.replies.append(request_id)
        return {}


def test_a_failed_auto_answer_is_retried_and_told_once(tmp_path):
    """Marking the request seen even on a failed answer left the agent blocked
    forever; today the tick retries until the server answers."""
    api = _RetryAPI()
    bridge = build(tmp_path, api=api, permission_poll=0.02)
    bridge.policy.set_mode("read")

    thread = threading.Thread(target=bridge._permission_loop, daemon=True)
    thread.start()
    for _ in range(100):
        if api.attempts >= 3 and bridge._requests.get("perm_r") is None:
            break
        time.sleep(0.05)
    bridge.stop_event.set()
    thread.join(timeout=2)

    assert api.attempts >= 3, "the answer must be retried until it lands"
    assert api.replies == ["perm_r"]
    assert "perm_r" not in bridge._requests, "an answered request leaves the cache"

    told = [text for text in bridge.telegram.sent if "Could not" in text]
    assert told, "the first failure must be visible in the chat"
    assert len(told) == 1, "a short outage must not spam one line per poll"


class _FailingListAPI(_API):
    def permission_requests(self, directory=None):
        raise ApiError(502, "proxy said no")


def test_a_failed_tick_never_touches_the_request_cache(tmp_path):
    """Reconciliation must not run when a chat poll failed: otherwise one
    chat's outage evicts every other chat's pending requests."""
    api = _FailingListAPI()
    bridge = build(tmp_path, api=api, permission_poll=0.02)
    request = {"id": "perm_keep", "sessionID": "ses_x", "action": "read"}
    bridge._requests["perm_keep"] = request
    bridge._perm_cards[(CHAT, 7)] = "perm_keep"

    thread = threading.Thread(target=bridge._permission_loop, daemon=True)
    thread.start()
    time.sleep(0.3)
    bridge.stop_event.set()
    thread.join(timeout=2)

    assert "perm_keep" in bridge._requests
    assert bridge._perm_cards.get((CHAT, 7)) == "perm_keep"


def test_reconciliation_drops_only_resolved_requests():
    bridge = SimpleNamespace(_requests={}, _perm_cards={})
    bridge._requests = {
        "perm_live": {"id": "perm_live", "sessionID": "ses_x", "action": "read"},
        "perm_stale": {"id": "perm_stale", "sessionID": "ses_x", "action": "bash"},
    }
    bridge._perm_cards = {(CHAT, 5): "perm_live", (CHAT, 6): "perm_stale"}

    watchers.reconcile_requests(bridge, {"perm_live"})

    assert "perm_live" in bridge._requests, "still pending must stay cached"
    assert "perm_stale" not in bridge._requests, "resolved-elsewhere goes"
    assert bridge._perm_cards.get((CHAT, 5)) == "perm_live"
    assert (CHAT, 6) not in bridge._perm_cards, "a stale card must not keep a dead id"


# ================================================================
# Attachments arriving in the same second
# ================================================================


def test_two_attachments_in_one_second_never_collide(tmp_path, monkeypatch):
    class _Downloads:
        def download(self, file_id, destination):
            destination.write_bytes(b"first" if file_id == "f1" else b"second")

    monkeypatch.setattr(attachments, "time", SimpleNamespace(time=lambda: 1_750_000_000.0))
    one = attachments.store(_Downloads(), "f1", "photo.jpg", "photo", tmp_path, "")
    two = attachments.store(_Downloads(), "f2", "photo.jpg", "photo", tmp_path, "")

    assert one.path != two.path, "same-second attachments are distinct files"
    assert one.path.read_bytes() == b"first", "the first was silently overwritten"
    assert two.path.read_bytes() == b"second"


# ================================================================
# uninstall.sh parsed flags positionally
# ================================================================


def test_uninstall_accepts_the_flags_in_any_position():
    script = ROOT / "uninstall.sh"
    text = script.read_text()
    assert "--purge) PURGE=1" in text
    assert "--purge-server) PURGE_SERVER=1" in text
    assert '"${2:-}" = "--purge-server"' not in text, (
        "the position-dependent flag check must be gone"
    )
    assert "Flags may be given in any order." in text
    subprocess.run(["bash", "-n", str(script)], check=True)
