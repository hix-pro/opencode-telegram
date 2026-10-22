"""Bot-cosmetics features: command autocomplete, menu button, reactions.

Three controls that come from Telegram's newer API surface and share one
theme: the bot becomes usable without remembering anything. Autocomplete
publishes the command registry, the Menu button is the command keyboard,
and an emoji reaction is the fastest route to approve or abort.
"""

from __future__ import annotations

import json
import tempfile

import pytest
from conftest import FakeAPI
from test_delivery import CHAT, FakeTelegram, build_bridge

from bridgekit import commands as C
from bridgekit import keyboards
from bridgekit.api import ApiError
from bridgekit.telegram import Telegram

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


# --------------------------------------------------- per-request reactions

PERM_1 = {"id": "perm_1", "sessionID": "ses_x", "action": "bash", "resources": ["ls"]}
PERM_2 = {"id": "perm_2", "sessionID": "ses_x", "action": "edit", "resources": []}


def test_a_permission_card_is_remembered_by_message_id(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())

    assert bridge._notify_permission(CHAT, "perm_9", {"action": "bash", "resources": ["ls"]})
    assert bridge._perm_cards == {(CHAT, 1): "perm_9"}


def test_reacting_on_a_card_approves_just_that_request(tmp_path):
    """Scope, not spray: the other pending request stays untouched."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI(permissions=[dict(PERM_1), dict(PERM_2)])
    bridge._requests = {"perm_1": dict(PERM_1)}
    bridge._perm_cards[(CHAT, 5)] = "perm_1"

    bridge._handle_update(reaction(message_id=5))

    assert bridge.api.permissions == [PERM_2], "only the card's request was answered"
    assert (CHAT, 5) not in bridge._perm_cards, "answered cards are forgotten"


def test_stop_reaction_on_a_card_rejects_it(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI(permissions=[dict(PERM_1)])
    bridge._requests = {"perm_1": dict(PERM_1)}
    bridge._perm_cards[(CHAT, 5)] = "perm_1"

    bridge._handle_update(reaction(message_id=5, emoji="👎"))

    decisions = [c[1][2] for c in bridge.api.calls if c[0] == "reply_permission"]
    assert decisions == ["reject"]
    assert bridge._perm_cards == {}


def test_a_reaction_off_the_card_still_means_yes_to_all(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI(permissions=[dict(PERM_1), dict(PERM_2)])
    bridge._perm_cards[(CHAT, 5)] = "perm_1"

    bridge._handle_update(reaction(message_id=99))

    assert bridge.api.permissions == [], "both requests approved"
    assert bridge._perm_cards == {}, "nothing is pending, so no card remains"


def test_card_mappings_resolve_with_their_requests(tmp_path):
    """A request answered elsewhere must not keep a mapped card."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._requests = {"perm_9": {}}
    bridge._perm_cards = {(CHAT, 1): "perm_9", (CHAT, 2): "ghost"}

    bridge._prune_seen([])

    assert bridge._perm_cards == {(CHAT, 1): "perm_9"}


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
    import threading

    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.state.set_session(CHAT, "ses_test")
    # "Running" is now a real fact: a live turn thread, not just any session.
    busy = threading.Event()
    bridge._workers = {CHAT: threading.Thread(target=busy.wait, daemon=True)}
    bridge._workers[CHAT].start()
    try:
        bridge._handle_update(reaction(emoji="👎"))
        assert bridge.abort_flag(CHAT).is_set()
        assert "Stopping" in bridge.telegram.sent[-1]
    finally:
        busy.set()


def test_stop_reaction_on_an_idle_chat_does_not_arm_the_flag(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.state.set_session(CHAT, "ses_test")
    bridge._workers = {}
    bridge._handle_update(reaction(emoji="👎"))
    assert "Nothing is running" in bridge.telegram.sent[-1]
    assert not bridge.abort_flag(CHAT).is_set()


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


# ----------------------------------------------------------------- sessions


def test_sessions_offers_a_switch_button_per_session(tmp_path):
    """/sessions renders the tap-friendly route; no more typed ids."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI()

    ctx = bridge.context(CHAT)
    reply = C.run(ctx, "/sessions")

    assert "2 session(s)" in reply
    rows = ctx.markup["inline_keyboard"]
    assert [row[0]["text"] for row in rows] == ["First session", "Second session"]
    assert [row[0]["callback_data"] for row in rows] == ["use:aaaaaaaa", "use:bbbbbbbb"]


def test_a_pressed_session_button_switches_and_reports(tmp_path):
    """The button route ends in the same switch a typed /use does."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI()
    bridge._register_callbacks()
    bridge._handle_callback(
        {
            "id": "cb1",
            "message": {"chat": {"id": int(CHAT)}, "message_id": 3},
            "data": "use:bbbbbbbb",
        }
    )

    assert bridge.state.session(CHAT) == "ses_bbbbbbbbbbbbbbbb"
    assert any("Switched" in sent for sent in bridge.telegram.sent)


def test_active_session_is_marked_in_the_button_list(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI()
    bridge.state.set_session(CHAT, "ses_bbbbbbbbbbbbbbbb")

    ctx = bridge.context(CHAT)
    C.run(ctx, "/sessions")

    texts = [row[0]["text"] for row in ctx.markup["inline_keyboard"]]
    assert texts == ["First session", "➜ Second session"]


def test_sessions_with_no_matches_leaves_no_markup(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI(sessions=[])

    ctx = bridge.context(CHAT)
    reply = C.run(ctx, "/sessions")

    assert ctx.markup is None
    assert "No sessions here yet" in reply


def test_session_buttons_with_unlisted_titles(tmp_path):
    """Untitled sessions still show a tappable row."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI(
        sessions=[
            {"id": "ses_cccccccccccccccc", "directory": tempfile.gettempdir(), "title": ""}
        ]
    )

    ctx = bridge.context(CHAT)
    C.run(ctx, "/sessions")

    assert [row[0]["text"] for row in ctx.markup["inline_keyboard"]] == ["Untitled"]


def test_duplicate_suffixes_fall_back_to_full_ids(tmp_path):
    """Two ids sharing their last 8 chars cannot share a callback."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI(
        sessions=[
            {"id": "ses_11111111111zzzzzzzz", "title": "A"},
            {"id": "ses_22222222222zzzzzzzz", "title": "B"},
        ]
    )

    ctx = bridge.context(CHAT)
    C.run(ctx, "/sessions")

    rows = ctx.markup["inline_keyboard"]
    datas = {row[0]["callback_data"] for row in rows}
    assert datas == {"use:zzzzzzzz", "use:ses_22222222222zzzzzzzz"}
    # The property that matters: no two buttons share a callback.
    assert len(datas) == 2


def test_sessions_reply_arrives_with_its_markup(tmp_path):
    """The markup channel travels all the way through the update handler."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI()
    bridge._handle_update(
        {"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": "/sessions"}}
    )

    assert len(bridge.telegram.markups) == 1
    assert bridge.telegram.markups[0] is not None


# -------------------------------------------------------------------- menu


def test_menu_labels_map_to_their_commands():
    assert keyboards.from_menu("📋 Status") == "/status"
    assert keyboards.from_menu("🗂 Sessions") == "/sessions"
    assert keyboards.from_menu("plain prompt") == "plain prompt"


def test_tapping_status_on_the_menu_runs_status_not_a_prompt(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI()
    bridge._handle_update(
        {"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": "📋 Status"}}
    )

    assert bridge.work.empty()
    assert bridge.telegram.sent, "the status command answered"


def test_tapping_sessions_on_the_menu_shows_switch_buttons(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.api = FakeAPI()
    bridge._handle_update(
        {"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": "🗂 Sessions"}}
    )

    assert bridge.work.empty()
    assert bridge.telegram.markups[-1] is not None


def test_reply_menu_is_persistent_and_covers_every_label():
    markup = keyboards.reply_menu_markup()

    assert markup["is_persistent"] is True and markup["resize_keyboard"] is True
    labels = [button["text"] for row in markup["keyboard"] for button in row]
    assert set(labels) == set(keyboards.MENU_LABELS)


def test_startup_mounts_the_menu_on_every_allowed_chat(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._mount_menu()

    assert any("Bridge is live" in sent for sent in bridge.telegram.sent)
    assert all(markup is not None for markup in bridge.telegram.markups)


# ------------------------------------------------------------------ edits


def edited(text=None, user=CHAT, chat_id=CHAT, is_bot=False):
    """A Bot API edited_message update."""
    return {
        "edited_message": {
            "chat": {"id": int(chat_id)},
            "message_id": 9,
            "from": {"id": int(user), "is_bot": is_bot},
            "date": 1,
            "edit_date": 2,
            "text": text,
        }
    }


def test_editing_a_sent_message_resends_it_as_a_prompt(tmp_path):
    """The chat history doubles as the prompt queue."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._handle_update(edited("make the header red instead"))
    assert bridge.work.get(timeout=1) == (CHAT, "make the header red instead", None)
    assert "Resending" in bridge.telegram.sent[-1]


def test_edits_without_text_do_nothing(tmp_path):
    """A caption-only edit has no words to resend."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._handle_update(edited(text=None))
    assert bridge.work.empty() and bridge.telegram.sent == []


def test_a_slash_command_edit_never_reruns(tmp_path):
    """Commands ran once when sent; re-running on rewording is a surprise."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._handle_update(edited("/new  corrected typo"))
    assert bridge.work.empty() and bridge.telegram.sent == []


def test_the_bridge_editing_itself_never_reprompts(tmp_path):
    """Progress-message edits come back as edited_message; ignore them."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._handle_update(edited("make the header red instead", is_bot=True))
    assert bridge.work.empty() and bridge.telegram.sent == []


def test_a_strangers_edit_is_ignored(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._handle_update(edited("sneaky prompt", user=999999))
    assert bridge.work.empty() and bridge.telegram.sent == []


def test_an_edit_from_a_foreign_chat_is_ignored(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._handle_update(edited("sneaky prompt", chat_id=777777, user=777777))
    assert bridge.work.empty() and bridge.telegram.sent == []


# ---------------------------------------------------------------- publish


def test_autocomplete_lists_the_daily_commands(tmp_path):
    """The input bar gets the curated list, not the whole 48-entry book."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge._publish_command_list()
    published = bridge.telegram._published
    names = [entry["command"] for entry in published]
    assert names == sorted(C.PRIMARY_COMMANDS)
    assert 10 <= len(names) <= 20, "a flood defeats the point; a stub is invisible"
    assert "help" in names and "yes" in names
    assert all(len(entry["description"]) <= 256 for entry in published)
    # A typo in the curated set would silently drop a command from
    # autocomplete; the suite must catch that the day it happens.
    assert set(C.PRIMARY_COMMANDS) <= set(C.REGISTRY), (
        "PRIMARY_COMMANDS names commands that do not exist in REGISTRY: "
        + ", ".join(sorted(set(C.PRIMARY_COMMANDS) - set(C.REGISTRY)))
    )


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


def test_get_updates_asks_for_reactions_on_the_wire():
    """Without allowed_updates containing message_reaction - and delivered as
    ONE JSON-serialized parameter, not repeated keys - Telegram never
    delivers reactions. Repeated keys read only the first pair ("message"),
    which JSON-decodes to nothing and leaves the default set silently."""
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
    assert params["allowed_updates"] == json.dumps(
        ["message", "edited_message", "callback_query", "message_reaction"]
    )
    assert params["offset"] == 7


# ------------------------------------------------------------------ macros


def test_macro_set_persists_to_state(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    ctx = bridge.context(CHAT)

    reply = C.run(ctx, "/macro deploy deploy the app to staging")

    assert "recorded" in reply
    assert ctx.macros["deploy"] == "deploy the app to staging"
    assert bridge.state.macros["deploy"] == "deploy the app to staging"


def test_macro_list_shows_what_exists(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    ctx = bridge.context(CHAT)
    C.run(ctx, "/macro deploy ship it")

    reply = C.run(ctx, "/macro")

    assert "/deploy" in reply and "ship it" in reply


def test_macro_list_with_none_recorded(tmp_path):
    ctx = build_bridge(tmp_path, FakeTelegram()).context(CHAT)
    assert "No macros yet" in C.run(ctx, "/macro")


def test_macro_delete(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    ctx = bridge.context(CHAT)
    C.run(ctx, "/macro deploy ship it")

    reply = C.run(ctx, "/macro deploy ")

    assert "deleted" in reply
    assert "deploy" not in bridge.state.macros


def test_macro_cannot_shadow_a_builtin(tmp_path):
    ctx = build_bridge(tmp_path, FakeTelegram()).context(CHAT)
    with pytest.raises(C.CommandError):
        C.run(ctx, "/macro help do things")


def test_macro_body_cannot_be_a_slash_command(tmp_path):
    ctx = build_bridge(tmp_path, FakeTelegram()).context(CHAT)
    with pytest.raises(C.CommandError):
        C.run(ctx, "/macro deploy /revert")


def test_macro_name_must_be_a_name(tmp_path):
    ctx = build_bridge(tmp_path, FakeTelegram()).context(CHAT)
    with pytest.raises(C.CommandError):
        C.run(ctx, "/macro 9bad text")


def test_typing_a_macro_sends_the_expanded_prompt(tmp_path):
    """The macro path ends in the work queue, like any prompt."""
    import json

    from bridgekit.state import VERSION

    bridge = build_bridge(tmp_path, FakeTelegram())
    (tmp_path / ".state.json").write_text(
        json.dumps(
            {
                "version": VERSION,
                "policy": {},
                "schedules": [],
                "macros": {"deploy": "Ship it {args}"},
                "chats": {},
            }
        )
    )
    bridge.state.load()

    bridge._handle_update(
        {"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": "/deploy staging"}}
    )

    assert bridge.work.get(timeout=1) == (CHAT, "Ship it staging", None)
    assert "Unknown command" not in " ".join(bridge.telegram.sent)


def test_macro_without_an_args_slot_appends_the_tail(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.state.save_macros({"deploy": "Ship it"})

    bridge._handle_update(
        {"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": "/deploy to prod now"}}
    )

    assert bridge.work.get(timeout=1) == (CHAT, "Ship it to prod now", None)


def test_a_bare_macro_runs_this_standing_prompt(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.state.save_macros({"deploy": "Ship it"})

    bridge._handle_update(
        {"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": "/deploy"}}
    )

    assert bridge.work.get(timeout=1) == (CHAT, "Ship it", None)


def test_unknown_slash_without_a_macro_still_errors(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())

    bridge._handle_update(
        {"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": "/definitelynotacmd"}}
    )

    assert any("Unknown command" in sent for sent in bridge.telegram.sent)
    assert bridge.work.empty()


def test_macro_cannot_shadow_the_start_greeting(tmp_path):
    """run() answers /start with help before looking any registry entry up,
    so a macro recorded under 'start' was accepted and then orphaned."""
    ctx = build_bridge(tmp_path, FakeTelegram()).context(CHAT)
    with pytest.raises(C.CommandError):
        C.run(ctx, "/macro start stand standup notes")


def test_an_args_only_macro_without_arguments_errors_out(tmp_path):
    """'jira {args}' typed bare used to rewrite to '' and queue the literal
    '/jira' as the agent's prompt."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.state.save_macros({"jira": "{args}"})

    bridge._handle_update(
        {"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": "/jira"}}
    )

    assert bridge.work.empty(), "nothing must reach the agent"
    assert any("'jira'" in text or "needs arguments" in text for text in bridge.telegram.sent)


def test_macro_expansion_still_works_with_a_provided_argument(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.state.save_macros({"jira": "{args}"})

    bridge._handle_update(
        {"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": "/jira triage MY-42"}}
    )

    assert bridge.work.get(timeout=1) == (CHAT, "triage MY-42", None)


def test_a_multiline_macro_body_is_rejected(tmp_path):
    """Everything after the first line was silently thrown away; half a
    prompt looked recorded and ran half-empty every time."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    ctx = bridge.context(CHAT)

    with pytest.raises(C.CommandError):
        C.run(ctx, "/macro deploy ship the thing\nnow")
    assert "deploy" not in bridge.state.macros
