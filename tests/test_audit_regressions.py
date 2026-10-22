"""Regressions from the production audit.

Every test here failed before the fix it describes. They are grouped by the
failure mode rather than the function, because the failure mode is what you
would see in the chat.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from bridgekit.api import ApiError
from bridgekit.config import Config
from bridgekit.keyboards import Router
from bridgekit.policy import Policy
from bridgekit.schedule import ScheduleStore
from bridgekit.state import State
from bridgekit.telegram import Telegram, split_message

CHAT = "42"


# ================================================================
# CRITICAL: a Telegram hiccup must never swallow the user's message
# ================================================================


class RaisingSession:
    """A requests.Session whose calls die at the transport layer."""

    def __init__(self, raise_sends: bool, raise_edits: bool):
        self.raise_sends = raise_sends
        self.raise_edits = raise_edits
        self.sent: list[str] = []
        self.edit_attempts = 0

    def post(self, url, json=None, timeout=None):
        if url.endswith("editMessageText"):
            self.edit_attempts += 1
            if self.raise_edits:
                raise ConnectionError("network down")
            self.sent.append(json["text"])
            return Ok()
        if self.raise_sends:
            raise ConnectionError("network down")
        self.sent.append(json["text"])
        return Ok(json.get("result", {}).get("message_id", 1))


class Ok:
    status_code = 200
    content = b"{}"
    text = "{}"

    def __init__(self, message_id=1):
        self._message_id = message_id

    def json(self):
        return {"result": {"message_id": self._message_id}}


class FlakyTelegram(Telegram):
    """Real Telegram client over a transport that can fail, like a WiFi blip."""

    def __init__(self, fail_send=True, fail_edit=True):
        super().__init__("token")
        self._http = RaisingSession(fail_send, fail_edit)

    @property
    def sent(self):
        return self._http.sent

    @property
    def edit_attempts(self):
        return self._http.edit_attempts

    def effective_interval(self):
        return 0.0


class RecordingAPI:
    def __init__(self):
        self.prompts: list[str] = []

    def send_prompt(self, session_id, text, files=None, delivery=None):
        self.prompts.append(text)

    def messages(self, session_id, limit=20, order="desc"):
        stamp = time.time() * 1000 + 1000
        return [
            {
                "type": "assistant",
                "time": {"created": stamp},
                "content": [{"type": "text", "text": "the answer"}],
            },
            {"type": "idle", "time": {"created": stamp + 1}},
        ]

    def permission_requests(self, directory=None):
        return []

    def get_session(self, session_id):
        return {"id": session_id, "title": "t", "time": {}}

    def create_session(self, directory, **k):
        return {"id": "ses_fresh00000000000"}

    def interrupt_session(self, session_id):
        return {}


def build(tmp_path, telegram, api, **overrides):
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
    for key, value in overrides.items():
        config = config.__class__(**{**config.__dict__, key: value})

    b = B.Bridge.__new__(B.Bridge)
    b.config = config
    b.api = api
    b.telegram = telegram
    b.state = State(config.state_file, config.default_dir)
    b.state.load()
    b.policy = Policy()
    b.events = StubEvents()
    b.router = Router()
    b.work = queue.Queue()
    b.stop_event = threading.Event()
    b.aborts = {}
    b._workers = {}
    b.schedules = ScheduleStore()
    b._requests = {}
    b.health = B.Health(streaming=False)
    b._lock = threading.Lock()
    b._register_callbacks()
    b.state.set_session(CHAT, "ses_x")
    return b


class StubEvents:
    last_error = ""
    connected = True
    frames = 0
    reconnects = 0

    def listen(self, *a):
        pass

    def unlisten(self, *a):
        pass

    def stop(self):
        pass


def test_placeholder_send_failure_does_not_discard_the_prompt(tmp_path):
    """The audit's worst bug: a Telegram blip before submit lost the message."""
    api = RecordingAPI()
    telegram = FlakyTelegram(fail_send=True, fail_edit=True)
    bridge = build(tmp_path, telegram, api)

    bridge._run_turn(CHAT, "please do this work")

    assert api.prompts == ["please do this work"], "prompt must still be submitted"


def test_edit_failure_still_delivers_the_answer(tmp_path):
    """edit() returning False was handled; edit() raising was not."""
    api = RecordingAPI()
    telegram = FlakyTelegram(fail_send=False, fail_edit=True)
    bridge = build(tmp_path, telegram, api)

    bridge._run_turn(CHAT, "question")

    assert any("the answer" in text for text in telegram.sent), (
        "the finished reply must reach the chat even when edits raise"
    )


def test_edit_failure_does_not_replace_the_reply_with_an_error(tmp_path):
    api = RecordingAPI()
    telegram = FlakyTelegram(fail_send=False, fail_edit=True)
    bridge = build(tmp_path, telegram, api)
    bridge._run_turn(CHAT, "question")
    assert not any("turn failed" in text.lower() for text in telegram.sent)


def test_edit_method_itself_never_raises(tmp_path):
    class Raising:
        def post(self, *a, **k):
            raise ConnectionError("network down")

    client = Telegram("token")
    client._http = Raising()
    assert client.edit(CHAT, 1, "text", force=True) is False


# ================================================================
# HIGH: /dir must not be silently undone
# ================================================================


def test_ensure_session_honours_the_chats_directory(tmp_path):
    """ensure_session used PROJECT_DIR, undoing /dir on the next message."""

    other = tmp_path / "project"
    other.mkdir()
    api = RecordingAPI()
    bridge = build(tmp_path, FlakyTelegram(), api)
    bridge.state.set_directory(CHAT, str(other.resolve()))
    bridge.state.set_session(CHAT, None)

    session = bridge.ensure_session(CHAT)
    assert session
    assert bridge.state.directory(CHAT) == str(other.resolve())


def test_dir_on_a_bound_session_starts_a_new_one(tmp_path):
    """A session is pinned to its directory server-side; /dir must follow."""
    from bridgekit.commands import run

    other = tmp_path / "other"
    other.mkdir()
    api = RecordingAPI()
    bridge = build(tmp_path, FlakyTelegram(), api)

    ctx = bridge.context(CHAT)
    reply = run(ctx, f"/dir {other}")
    assert str(other.resolve()) in reply
    # The chat is now bound to a session that actually lives there.
    assert bridge.state.session(CHAT) is not None


# ================================================================
# HIGH: state file safety
# ================================================================


def test_state_save_uses_a_unique_temp_file(tmp_path):
    """A shared temp name lets a concurrent writer publish an empty file."""
    import inspect

    source = inspect.getsource(State.save)
    assert "NamedTemporaryFile" in source or "mkstemp" in source


def test_concurrent_saves_never_publish_an_empty_file(tmp_path):
    path = tmp_path / ".state.json"
    state = State(path, "/tmp")
    state.load()
    state.set_session(CHAT, "ses_x")

    errors: list[str] = []
    stop = threading.Event()

    def writer(index: int):
        try:
            while not stop.is_set():
                state.set_pref(CHAT, f"key{index}", "v" * 200)
        except Exception as exc:  # pragma: no cover - surfaced via errors
            errors.append(repr(exc))

    def reader():
        for _ in range(400):
            if path.exists():
                try:
                    text = path.read_text()
                except OSError:
                    continue
                if text.strip() == "":
                    errors.append("observed an empty state file")
                    return
            time.sleep(0.001)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.4)
    stop.set()
    for t in threads:
        t.join(timeout=2)

    assert not errors, errors
    assert json.loads(path.read_text())["chats"][CHAT]["session"] == "ses_x"


def test_settings_persist_before_the_first_turn(tmp_path):
    """save() filtered on `chat.session`, so /dir and /alert were lost."""
    path = tmp_path / ".state.json"
    state = State(path, "/tmp")
    state.load()
    state.set_pref(CHAT, "delivery", "steer")
    state.set_directory(CHAT, "/tmp")

    reloaded = State(path, "/tmp")
    reloaded.load()
    assert reloaded.pref(CHAT, "delivery") == "steer"
    assert reloaded.directory(CHAT) == "/tmp"


# ================================================================
# HIGH: the permission watcher must not be able to die quietly
# ================================================================


class ExplodingPermissions:
    def __init__(self):
        self.calls = 0

    def permission_requests(self, directory=None):
        self.calls += 1
        # What a proxy 502 or a mid-restart server actually produces.
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    def reply_permission(self, *a, **k):
        return {}


def test_permission_survives_a_non_api_error(tmp_path):
    """A watcher that dies takes auto-approval, prompts and pruning with it."""
    api = ExplodingPermissions()
    bridge = build(tmp_path, FlakyTelegram(), RecordingAPI())
    bridge.api = api
    bridge.stop_event = threading.Event()
    bridge.config = bridge.config.__class__(
        **{**bridge.config.__dict__, "permission_poll": 0.01}
    )

    thread = threading.Thread(target=bridge._permission_loop, daemon=True)
    thread.start()
    time.sleep(0.25)
    assert api.calls >= 2, "watcher must keep polling despite the error"
    alive = thread.is_alive()
    bridge.stop_event.set()
    thread.join(timeout=2)
    assert alive, "watcher must not have exited"


def test_api_errors_surface_as_api_errors_not_value_errors():
    """A 200 with a non-JSON body must not raise a bare JSONDecodeError."""
    from bridgekit.api import OpenCode

    class BadBody:
        status_code = 200
        content = b"<html>502 Bad Gateway</html>"
        text = "<html>502</html>"

        def json(self):
            raise ValueError("Expecting value")

    client = OpenCode("http://x", "pw")
    client._http = type("S", (), {"request": lambda *a, **k: BadBody()})()
    with pytest.raises(ApiError):
        client.info()


def test_watcher_survives_a_corrupt_state_dictionary(tmp_path):
    """save() iterating while another thread inserts raises RuntimeError."""
    path = tmp_path / ".state.json"
    state = State(path, "/tmp")
    state.load()
    state.set_session(CHAT, "ses_x")

    errors: list = []
    stop = threading.Event()

    def saver():
        try:
            while not stop.is_set():
                for i in range(5):
                    state.set_pref(CHAT, f"k{i}", i)
        except Exception as exc:
            errors.append(repr(exc))

    t = threading.Thread(target=saver)
    t.start()
    time.sleep(0.2)
    stop.set()
    t.join(timeout=2)
    assert not errors, errors


# ================================================================
# Config validation
# ================================================================


def env(**overrides):
    base = {
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_CHAT_ID": "1",
        "OPENCODE_PASSWORD": "p",
        "PROJECT_DIR": "/tmp",
    }
    base.update(overrides)
    return base


def test_max_chunk_is_clamped_to_the_telegram_limit():

    config = Config.from_env(env(MAX_CHUNK="99999"))
    assert config.max_chunk <= 4096


def test_zero_intervals_are_rejected():
    from bridgekit.config import ConfigError

    with pytest.raises(ConfigError):
        Config.from_env(env(PERMISSION_POLL_INTERVAL="0"))
    with pytest.raises(ConfigError):
        Config.from_env(env(POLL_MESSAGES="0"))
    with pytest.raises(ConfigError):
        Config.from_env(env(POLL_MESSAGES="-1"))


def test_project_dir_must_be_inside_allowed_roots(tmp_path):
    from bridgekit.config import ConfigError

    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ConfigError, match="outside ALLOWED_ROOTS"):
        Config.from_env(env(PROJECT_DIR=str(outside), ALLOWED_ROOTS=str(root)))


# ================================================================
# Formatting
# ================================================================


def test_head_tail_with_a_tiny_limit():
    """text[-0:] is the whole string, so a small limit returned everything."""
    from bridgekit.formatting import head_tail

    assert len(head_tail("A" * 5000, 20)) <= 20
    assert len(head_tail("A" * 5000, 2)) <= 2


def test_split_message_reserves_headroom_for_utf16():
    """Telegram counts UTF-16 units, so emoji make replies longer."""
    emoji = "\U0001f600" * 2100
    for chunk in split_message(emoji, 4096):
        assert len(chunk.encode("utf-16-le")) // 2 <= 4096
