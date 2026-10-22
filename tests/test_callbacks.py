"""Inline-button callback handling.

Buttons are the one input path that bypasses the normal command parser, and it
is the one a user can hit by accident at 2am. These tests pin down what each
button does and, just as importantly, what a stranger's tap is allowed to do.
"""

from __future__ import annotations

import queue
import threading

import pytest

from bridgekit import keyboards
from bridgekit.config import Config
from bridgekit.keyboards import Router
from bridgekit.policy import Policy
from bridgekit.schedule import ScheduleStore
from bridgekit.state import State

CHAT = "42"


class FakeTelegram:
    def __init__(self):
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.answered: list[tuple[str, str]] = []
        self.markups: list = []

    def send(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        self.markups.append(reply_markup)
        return 1

    def edit(self, chat_id, message_id, text, reply_markup=None, force=False):
        self.edits.append(text)
        return True

    def get_updates(self, offset, timeout=30):
        return []

    def answer_callback(self, callback_id, text=""):
        self.answered.append((callback_id, text))

    def effective_interval(self):
        return 2.0

    # health counters surfaced by /health and /status
    edit_failures = 0
    rate_limited = 0


class FakeAPI:
    def __init__(self):
        self.replies: list[dict] = []
        self.interrupted: list[str] = []
        self.compacted: list[str] = []
        self.sessions = [
            {"id": "ses_aaaaaaaaaaaa1111", "title": "One", "directory": "/tmp"},
            {"id": "ses_bbbbbbbbbbbb2222", "title": "Two", "directory": "/tmp"},
        ]

    def reply_permission(self, session_id, request_id, decision, message=None):
        self.replies.append({"session": session_id, "id": request_id, "decision": decision})
        return {}

    def interrupt_session(self, session_id):
        self.interrupted.append(session_id)
        return {}

    def compact_session(self, session_id):
        self.compacted.append(session_id)
        return {}

    def list_sessions(self, directory=None, limit=20, search=None):
        return self.sessions

    def get_session(self, session_id):
        for session in self.sessions:
            if session["id"] == session_id:
                return session
        return {"id": session_id, "title": "t", "time": {}}

    def permission_requests(self, directory=None):
        return []

    def messages(self, session_id, limit=20, order="desc"):
        return []

    def send_prompt(self, session_id, text, files=None, delivery=None):
        return None

    def info(self):
        return {"version": "2.0.31", "pid": 1}

    def create_session(self, directory, title=None, model=None, agent=None):
        return {"id": "ses_new0000000000000"}


class StubEvents:
    connected = True
    frames = 0
    reconnects = 0

    def listen(self, session_id, callback):
        pass

    def unlisten(self, session_id):
        pass

    def stop(self):
        pass


@pytest.fixture
def bridge(tmp_path):
    import bridge as B

    config = Config(
        token="t",
        allowed_chats=frozenset({CHAT}),
        opencode_url="http://x",
        password="p",
        default_dir=str(tmp_path),
        state_file=tmp_path / ".state.json",
    )
    instance = B.Bridge.__new__(B.Bridge)
    instance.config = config
    instance.api = FakeAPI()
    instance.telegram = FakeTelegram()
    instance.state = State(config.state_file, config.default_dir)
    instance.state.load()
    instance.policy = Policy()
    instance.events = StubEvents()
    instance.router = Router()
    instance.work = queue.Queue()
    instance.stop_event = threading.Event()
    instance.aborts = {}
    instance._workers = {}
    instance._requests = {}
    instance._backlog = {}
    instance.schedules = ScheduleStore()
    instance.health = B.Health(streaming=False)
    instance._lock = threading.Lock()
    instance._register_callbacks()
    return instance


def press(bridge, data: str, chat_id: str = CHAT):
    """Simulate a user tapping a button."""
    bridge._handle_callback(
        {
            "id": "cb1",
            "data": data,
            "from": {"id": chat_id},
            "message": {"chat": {"id": chat_id}},
        }
    )


def with_permission(bridge):
    request = {
        "id": "perm_xyz",
        "sessionID": "ses_aaaaaaaaaaaa1111",
        "action": "bash",
        "resources": ["ls"],
    }
    bridge.api.sessions = [dict(bridge.api.sessions[0])]
    bridge._pending_override = request
    return request


# ------------------------------------------------------------- registration


def test_every_button_action_is_handled(bridge):
    expected = {
        "cmd",
        "abort",
        "info",
        "compact",
        "use",
        "perm_yes",
        "perm_always",
        "perm_no",
    }
    assert expected <= set(bridge.router.actions())


def test_shipped_keyboards_map_to_real_actions(bridge):
    """Every button we can send must have a handler behind it."""
    from bridgekit.keyboards import help_buttons, permission_buttons, session_buttons

    markups = [
        permission_buttons("perm_1"),
        session_buttons("ses_aaaaaaaaaaaa1111"),
        help_buttons(),
    ]
    for markup in markups:
        for row in markup["inline_keyboard"]:
            for button in row:
                action, _ = keyboards.decode(button["callback_data"])
                assert bridge.router.resolve(action), f"no handler for {action}"


# ------------------------------------------------------------- permissions


def _grant(bridge, decision_action, request):
    """Answer through the path the watcher uses, with a known request."""
    bridge._reply_permission = lambda chat, rid, decision, req=None: bridge.api.replies.append(
        {"id": rid, "decision": decision}
    )
    press(bridge, keyboards.encode(decision_action, request["id"]))
    return bridge.api.replies[-1]


def test_allow_once(bridge):
    request = with_permission(bridge)
    assert _grant(bridge, "perm_yes", request)["decision"] == "once"


def test_always_allow(bridge):
    request = with_permission(bridge)
    assert _grant(bridge, "perm_always", request)["decision"] == "always"


def test_reject(bridge):
    request = with_permission(bridge)
    assert _grant(bridge, "perm_no", request)["decision"] == "reject"


def test_callback_is_always_acknowledged(bridge):
    press(bridge, "perm_yes:perm_xyz")
    assert bridge.telegram.answered and bridge.telegram.answered[0][0] == "cb1"


# ------------------------------------------------------------------ control


def test_abort_button_stops_the_turn(bridge):
    bridge.state.set_session(CHAT, "ses_aaaaaaaaaaaa1111")
    press(bridge, "abort")
    assert bridge.api.interrupted == ["ses_aaaaaaaaaaaa1111"]
    assert bridge.abort_flag(CHAT).is_set()


def test_abort_button_without_a_session_is_harmless(bridge):
    press(bridge, "abort")
    assert bridge.api.interrupted == []


def test_compact_button_compacts(bridge):
    bridge.state.set_session(CHAT, "ses_aaaaaaaaaaaa1111")
    press(bridge, "compact")
    assert bridge.api.compacted == ["ses_aaaaaaaaaaaa1111"]


def test_info_button_answers_with_details(bridge):
    bridge.state.set_session(CHAT, "ses_aaaaaaaaaaaa1111")
    press(bridge, "info")
    assert bridge.telegram.sent
    assert "ses_aaaa" in bridge.telegram.sent[-1]


def test_use_button_switches_by_id_suffix(bridge):
    bridge.state.set_session(CHAT, "ses_aaaaaaaaaaaa1111")
    press(bridge, keyboards.encode("use", "2222"))
    assert bridge.state.session(CHAT) == "ses_bbbbbbbbbbbb2222"


def test_use_button_with_no_match_changes_nothing(bridge):
    bridge.state.set_session(CHAT, "ses_aaaaaaaaaaaa1111")
    press(bridge, keyboards.encode("use", "zzzz"))
    assert bridge.state.session(CHAT) == "ses_aaaaaaaaaaaa1111"


def test_cmd_button_runs_the_command(bridge):
    press(bridge, keyboards.encode("cmd", "status"))
    assert bridge.telegram.sent
    assert "OpenCode" in bridge.telegram.sent[-1]


def test_cmd_button_with_a_bad_command_reports_instead_of_raising(bridge):
    press(bridge, keyboards.encode("cmd", "nonsense"))
    assert bridge.telegram.sent
    assert bridge.telegram.sent[-1].startswith("!")


# ------------------------------------------------------------------ security


def test_strangers_cannot_press_buttons(bridge):
    bridge.state.set_session("99", "ses_aaaaaaaaaaaa1111")
    press(bridge, "abort", chat_id="99")
    assert bridge.api.interrupted == []
    assert bridge.telegram.answered == []


def test_unknown_button_data_is_ignored(bridge):
    press(bridge, "something_malicious:payload")
    assert bridge.api.replies == []
    assert bridge.api.interrupted == []
    assert bridge.telegram.answered


def test_a_failing_handler_does_not_escape(bridge):
    def explode(chat_id, argument):
        raise RuntimeError("boom")

    bridge.router.on("boom")(explode)
    press(bridge, "boom:x")  # must not raise out of _handle_callback
    assert bridge.telegram.answered
