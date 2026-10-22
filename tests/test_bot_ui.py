"""Bot-cosmetics features: command autocomplete, menu button, reactions.

Three controls that come from Telegram's newer API surface and share one
theme: the bot becomes usable without remembering anything. Autocomplete
publishes the command registry, the Menu button is the command keyboard,
and an emoji reaction is the fastest route to approve or abort.
"""

from __future__ import annotations

from conftest import FakeAPI
from test_delivery import CHAT, FakeTelegram, build_bridge

from bridgekit.api import ApiError
from bridgekit.telegram import ALLOWED_UPDATES, Telegram

# ------------------------------------------------------------- reactions


def reaction(chat_id=CHAT, user=CHAT, emoji="👍", message_id=5, new=None):
    """A Bot API message_reaction update."""
    new_reaction = [{"emoji": emoji}] if new is None else new
    return {
        "message_reaction": {
            "chat": {"id": int(chat_id)},
            "message_id": message_id,
            "user": {"id": int(user)},
            "date": 1,
            "new_reaction": new_reaction,
            "old_reaction": [],
        }
    }


def test_thumbs_up_approves_every_pending_request(tmp_path):
    """The fastest 'yes to all' a phone can give."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI(
        permissions=[
            {"id": "perm_1", "sessionID": "ses_x", "action": "bash", "resources": ["ls"]},
            {"id": "perm_2", "sessionID": "ses_x", "action": "edit", "resources": []},
        ]
    )
    bridge._requests = {}
    bridge._handle_update(reaction())

    approvals = [c for c in bridge.api.calls if c[0] == "reply_permission"]
    assert len(approvals) == 2
    assert {c[1][1] for c in approvals} == {"perm_1", "perm_2"}
    assert all(c[1][2] == "once" for c in approvals)
    assert bridge.api.permissions == [], "no request left pending"
    assert any("approved" in m for m in bridge.telegram.sent)


def test_thumbs_up_with_nothing_pending_says_so(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI()
    bridge._requests = {}
    bridge._handle_update(reaction())
    assert "Nothing waiting" in bridge.telegram.sent[-1]


def test_approval_listing_failure_is_reported(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI()

    def explode(*args, **kwargs):
        raise ApiError(500, "server on fire")

    bridge.api.permission_requests = explode
    bridge._requests = {}
    bridge._handle_update(reaction())
    assert "!" in bridge.telegram.sent[-1] and "on fire" in bridge.telegram.sent[-1]


def test_thumbs_down_interrupts_a_running_turn(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.state.set_session(CHAT, "ses_test")
    bridge._handle_update(reaction(emoji="👎"))
    assert bridge.abort_flag(CHAT).is_set()
    assert "Stopping" in bridge.telegram.sent[-1]


def test_stop_reaction_without_a_session_reports(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._handle_update(reaction(emoji="🛑"))
    assert "Nothing is running" in bridge.telegram.sent[-1]
    assert not bridge.abort_flag(CHAT).is_set()


def test_a_stranger_cannot_steer_by_reaction(tmp_path):
    """Anyone outside the allow-list gets neither reply nor action."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._requests = {}
    bridge._handle_update(reaction(user=999999))
    assert bridge.telegram.sent == []
    assert not bridge.abort_flag(CHAT).is_set()


def test_a_foreign_chat_is_ignored(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._handle_update(reaction(chat_id=777777, user=777777))
    assert bridge.telegram.sent == []


def test_removing_a_reaction_never_repeats(tmp_path):
    """new_reaction empty means 'take it back', not 'run it again'."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._requests = {}
    bridge._handle_update(reaction(new=[]))
    assert bridge.telegram.sent == []


def test_unknown_emoji_is_neutral(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._requests = {}
    bridge._handle_update(reaction(emoji="❤️"))
    assert bridge.telegram.sent == []


def test_ignore_unknown_update_types(tmp_path):
    """An update the bridge does not know must not crash the loop."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._handle_update({"update": {"id": 1}})
    assert bridge.telegram.sent == []


# ---------------------------------------------------------------- publish


def test_autocomplete_lists_the_whole_registry(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._publish_command_list()
    published = bridge.telegram._published
    names = [entry["command"] for entry in published]
    assert "help" in names and "status" in names
    assert names == sorted(names)
    assert all(len(entry["description"]) <= 256 for entry in published)


def test_menu_button_is_set_for_each_allowed_chat(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._publish_command_list()
    assert bridge.telegram.menu_buttons == [CHAT]


def test_set_my_commands_truncates_and_is_best_effort(monkeypatch):
    """Over 100 commands or 256-char descriptions must still fit Telegram."""
    posts = []

    class Session:
        def post(self, url, json=None, timeout=None):
            posts.append((url, json))

            class Resp:
                status_code = 200
                text = "ok"

                def json(self):
                    return {"ok": True}

            return Resp()

    tg = Telegram("token")
    tg._http = Session()
    entries = [
        {"command": name, "description": "d" * 300}
        for name in (f"cmd_{i:05d}" for i in range(150))
    ]
    assert tg.set_my_commands(entries) is True
    url, payload = posts[0]
    assert url.endswith("/setMyCommands")
    assert len(payload["commands"]) == 100
    assert all(len(c["description"]) == 256 for c in payload["commands"])


def test_publish_failures_do_not_raise(monkeypatch):
    """A cosmetic failure must not break startup: it returns False."""
    tg = Telegram("token")

    class Dead:
        def post(self, url, json=None, timeout=None):
            raise ConnectionError("no network")

    tg._http = Dead()
    assert tg.set_my_commands([{"command": "help", "description": "x"}]) is False
    assert tg.set_menu_button("42") is False


def test_get_updates_asks_for_reactions_with_the_batch():
    """Without allowed_updates containing message_reaction, Telegram never
    delivers reactions and the feature looks broken everywhere."""
    calls = []

    class Session:
        def get(self, url, params=None, timeout=None):
            calls.append(params)

            class Resp:
                status_code = 200
                content = b"{}"

                @staticmethod
                def raise_for_status():
                    return None

                def json(self):
                    return {"result": []}

            return Resp()

    tg = Telegram("token")
    tg._http = Session()
    tg.get_updates(7, timeout=1)
    params = calls[0]
    assert "message_reaction" in params["allowed_updates"]
    assert params["allowed_updates"] == tuple(ALLOWED_UPDATES)
    assert params["offset"] == 7
