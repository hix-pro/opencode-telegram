"""Final-reply delivery.

The live message is edited in place, which is fast and tidy but has two ways
to fail: Telegram rate-limits edits, and refuses edits of old messages. Both
used to drop the finished reply silently, which from the chat looks like the
bot stopped mid-job. These tests pin the rule that the answer always arrives,
one way or another.
"""

from __future__ import annotations

import queue
import threading
import time

import pytest

import bridgekit.telegram
from bridgekit.config import Config
from bridgekit.keyboards import Router
from bridgekit.policy import Policy
from bridgekit.schedule import ScheduleStore
from bridgekit.state import State
from bridgekit.telegram import Telegram, TelegramError

CHAT = "42"


class FakeTelegram:
    """Records delivery, and can be told to refuse edits."""

    def __init__(self, edit_succeeds: bool = True):
        self.sent: list[str] = []
        self.markups: list = []
        self.edits: list[str] = []
        self.edit_succeeds = edit_succeeds
        self.edit_failures = 0
        self.rate_limited = 0
        self.menu_buttons: list[str] = []

    def send(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        self.markups.append(reply_markup)
        return 1

    def set_my_commands(self, commands):
        """Recorded through sent; the args are what `/help` would show."""
        self._published = list(commands)
        return True

    def set_menu_button(self, chat_id):
        self.menu_buttons.append(chat_id)
        return True

    def edit(self, chat_id, message_id, text, reply_markup=None, force=False):
        if not self.edit_succeeds:
            self.edit_failures += 1
            return False
        self.edits.append(text)
        return True

    def effective_interval(self):
        return 2.0

    def get_updates(self, offset, timeout=30):
        return []

    def answer_callback(self, callback_id, text=""):
        pass


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


class IdleAPI:
    """A turn that finishes immediately."""

    def __init__(self, text="the answer"):
        self.text = text

    def messages(self, session_id, limit=20, order="desc"):
        import time

        stamp = time.time() * 1000 + 1000
        return [
            {
                "type": "assistant",
                "time": {"created": stamp},
                "content": [{"type": "text", "text": self.text}],
            },
            {"type": "idle", "time": {"created": stamp + 1}},
        ]

    def send_prompt(self, session_id, text, files=None, delivery=None):
        return None

    def permission_requests(self, directory=None):
        return []

    def interrupt_session(self, session_id):
        return {}

    def get_session(self, session_id):
        return {"id": session_id, "title": "t", "time": {}}


def build_bridge(tmp_path, telegram):
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
        stream_enabled=True,
        prompt_timeout=10,
    )
    instance = B.Bridge.__new__(B.Bridge)
    instance.config = config
    instance.api = IdleAPI()
    instance.telegram = telegram
    instance.state = State(config.state_file, config.default_dir)
    instance.state.load()
    instance.policy = Policy()
    instance.events = StubEvents()
    instance.router = Router()
    instance.work = queue.Queue()
    instance.stop_event = threading.Event()
    instance.aborts = {}
    instance._requests = {}
    instance._perm_cards = {}
    instance.health = B.Health(streaming=False)
    instance._lock = threading.Lock()
    instance.schedules = ScheduleStore()
    instance.fatal = None
    return instance


def test_reply_arrives_by_edit_when_edits_work(tmp_path):
    tg = FakeTelegram(edit_succeeds=True)
    bridge = build_bridge(tmp_path, tg)
    bridge.state.set_session(CHAT, "ses_test")
    bridge._run_turn(CHAT, "question")
    assert tg.edits and tg.edits[-1].startswith("the answer")
    # Only the live placeholder is sent; the answer replaces it in place.
    assert len(tg.sent) == 1
    assert "working" in tg.sent[0]


def test_reply_is_sent_when_the_edit_is_refused(tmp_path):
    """The exact failure that lost a reply: edit rate-limited at the end."""
    tg = FakeTelegram(edit_succeeds=False)
    bridge = build_bridge(tmp_path, tg)
    bridge.state.set_session(CHAT, "ses_test")
    bridge._run_turn(CHAT, "question")

    assert tg.sent, "the finished reply must still be delivered"
    assert tg.sent[-1].startswith("the answer")


def test_reply_is_sent_when_watching_is_off(tmp_path):
    tg = FakeTelegram(edit_succeeds=True)
    bridge = build_bridge(tmp_path, tg)
    bridge.state.set_session(CHAT, "ses_test")
    bridge.state.set_pref(CHAT, "watch", False)
    bridge._run_turn(CHAT, "question")
    assert tg.sent and tg.sent[-1].startswith("the answer")


def test_aborted_turn_still_delivers_its_final_message(tmp_path):
    tg = FakeTelegram(edit_succeeds=False)
    bridge = build_bridge(tmp_path, tg)
    bridge.state.set_session(CHAT, "ses_test")
    bridge.abort_flag(CHAT).set()
    bridge._run_turn(CHAT, "question")
    assert tg.sent
    assert "interrupted" in tg.sent[-1]


# ------------------------------------------------------------ rate limiting


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)
        self.content = b"{}"

    def json(self):
        return self._payload


class FakeSession:
    """Stands in for requests.Session with a scripted HTTP response."""

    def __init__(self, response):
        self.response = response
        self.posts: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json or {})
        return self.response

    def get(self, *a, **k):
        raise RuntimeError("not used")


def make_client(status, payload):
    client = Telegram("token")
    client._http = FakeSession(_Response(status, payload))
    return client


def test_a_malformed_error_body_does_not_escape():
    """A non-JSON 429 or 400 must not raise out of edit()."""

    class Broken:
        status_code = 429
        text = "<html>nope</html>"
        content = b"<html>"

        def json(self):
            raise ValueError("Expecting value")

    client = Telegram("token")
    client._http = FakeSession(Broken())
    assert client.edit("42", 1, "text", force=True) is False


def test_rate_limited_edit_reports_failure_instead_of_raising():
    client = make_client(429, {"parameters": {"retry_after": 5}})
    assert client.edit("42", 1, "text", force=True) is False
    assert client.rate_limited == 1


def test_rate_limit_widens_the_edit_interval():
    """The server's retry_after is the honest bound: with retry_after=5 the
    spacing widens to about five seconds, not to the old fixed 'twice the
    base interval'."""
    client = make_client(429, {"parameters": {"retry_after": 5}})
    client.edit("42", 1, "text", force=True)
    assert client.effective_interval() >= client.edit_interval + 3


def test_rate_limit_interval_relaxes_once_the_window_passes():
    """The widened spacing is not sticky: once the window the server named
    has closed, the ordinary interval resumes."""
    client = make_client(429, {"parameters": {"retry_after": 0.2}})
    client.edit("42", 1, "text", force=True)
    time.sleep(0.25)
    assert client.effective_interval() == client.edit_interval


def test_a_windowed_edit_never_hits_the_wire():
    """A non-forced edit inside the server's window must not POST: the
    request would only earn another 429 and re-arm the window."""
    client = make_client(429, {"parameters": {"retry_after": 5}})
    client.edit("42", 1, "text", force=True)  # arms the window
    client._http.posts.clear()

    assert client.edit("42", 1, "text") is False
    assert client._http.posts == [], "the windowed edit must not POST"


def test_the_send_path_records_the_penalty_for_edits_too(monkeypatch):
    """A 429 on sendMessage also holds the edits of that chat: the refresh
    stream would have ignored the window the send path paid for."""
    monkeypatch.setattr(bridgekit.telegram.time, "sleep", lambda seconds: None)
    client = make_client(429, {"parameters": {"retry_after": 5}})

    with pytest.raises(TelegramError):
        client._send_chunk("42", "hello")

    assert len(client._http.posts) == 4, "the send retry loop still runs"
    assert client.effective_interval() > client.edit_interval, (
        "the send path must arm the same edit window"
    )


def test_rejected_edit_is_counted():
    client = make_client(400, {"description": "message can't be edited"})
    assert client.edit("42", 1, "text", force=True) is False
    assert client.edit_failures == 1


def test_unchanged_text_is_not_counted_as_a_failure():
    """Telegram rejects a no-op edit with 400; that is not a lost reply."""
    client = make_client(400, {"description": "message is not modified"})
    client.edit("42", 1, "same", force=True)
    assert client.edit_failures == 0


def test_throttled_edit_is_skipped_locally():
    client = make_client(200, {"result": {"message_id": 1}})
    assert client.edit("42", 1, "a") is True
    assert client.edit("42", 1, "b") is False  # too soon
    assert client.edit("42", 1, "c", force=True) is True
    assert len(client._http.posts) == 2


# ------------------------------------------------------------------ send 429s


class SequenceSession:
    """Scripted posts: each call pops the next response off the queue."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = 0

    def post(self, url, json=None, timeout=None):
        self.posts += 1
        return self.responses.pop(0)


def test_send_survives_a_malformed_429_body(monkeypatch):
    """A proxy eating the error body must retry, not escalate to ValueError.

    Before the fix a non-JSON 429 escaped as an unhandled ValueError -
    which, arriving mid-turn, failed the whole turn for want of one retry.
    """
    monkeypatch.setattr("bridgekit.telegram.time.sleep", lambda s: None)
    broken = _Response.__new__(_Response)
    broken.status_code = 429
    broken.text = "<html>rate limited</html>"
    broken.content = b"<html>"

    def broken_json():
        raise ValueError("Expecting value")

    broken.json = broken_json
    ok = _Response(200, {"result": {"message_id": 9}})
    client = Telegram("token")
    client._http = SequenceSession([broken, ok])
    assert client.send("42", "hello") == 9
    assert client._http.posts == 2


def test_send_survives_a_null_retry_parameters(monkeypatch):
    """`"parameters": null` must not crash on None.get() either."""
    monkeypatch.setattr("bridgekit.telegram.time.sleep", lambda s: None)
    client = Telegram("token")
    client._http = SequenceSession([_Response(429, {"parameters": None})] * 4)
    with pytest.raises(TelegramError, match="4 attempts"):
        client.send("42", "hello")


def test_send_still_gives_up_after_four_throttles(monkeypatch):
    """Persistent throttling raises TelegramError the caller can report."""
    monkeypatch.setattr("bridgekit.telegram.time.sleep", lambda s: None)
    client = Telegram("token")
    client._http = SequenceSession([_Response(429, {"parameters": {"retry_after": 1}})] * 4)
    with pytest.raises(TelegramError):
        client.send("42", "hello")
    assert client._http.posts == 4


def test_default_edit_interval_respects_the_telegram_limit():
    """Telegram allows about 30 message edits a minute."""
    config = Config(
        token="t",
        allowed_chats=frozenset({"1"}),
        opencode_url="u",
        password="p",
        default_dir="/tmp",
        state_file="/tmp/s.json",
    )
    assert config.edit_interval >= 2.0
    assert 60 / config.edit_interval <= 31
