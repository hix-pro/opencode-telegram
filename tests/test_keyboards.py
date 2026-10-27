"""Inline keyboards and callback routing."""

from __future__ import annotations

import pytest

from bridgekit import keyboards


def test_encode_decode_round_trip():
    assert keyboards.decode(keyboards.encode("perm_yes", "req-123")) == (
        "perm_yes",
        "req-123",
    )


def test_encode_without_an_argument():
    assert keyboards.encode("abort") == "abort"
    assert keyboards.decode("abort") == ("abort", "")


def test_encode_respects_telegram_limits():
    payload = keyboards.encode("use", "x" * 500)
    assert len(payload) <= keyboards.MAX_CALLBACK


def test_keyboard_shape():
    markup = keyboards.keyboard([[("A", "a"), ("B", "b")], [("C", "c")]])
    rows = markup["inline_keyboard"]
    assert len(rows) == 2
    assert rows[0][0] == {"text": "A", "callback_data": "a"}
    assert len(rows[0]) == 2


def test_empty_rows_are_dropped():
    assert keyboards.keyboard([[], [("A", "a")]])["inline_keyboard"] == [
        [{"text": "A", "callback_data": "a"}]
    ]


def test_permission_buttons_cover_all_three_answers():
    markup = keyboards.permission_buttons("req-1")
    actions = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert actions == ["perm_yes:req-1", "perm_always:req-1", "perm_no:req-1"]


def test_a_long_request_id_drops_the_dead_always_button():
    """'perm_always:' + a long id used to be truncated at Telegram's 64-byte
    callback cap, leaving a button that resolved to "no longer pending" while
    the agent stayed blocked. It is omitted instead; the fixed-action buttons
    remain (still truncated for ids beyond ~55 chars - the card also prints
    the id, so /yes <id> keeps working)."""
    long_id = "r" * 60
    markup = keyboards.permission_buttons(long_id)
    actions = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert not any(a.startswith("perm_always") for a in actions)
    assert any(a.startswith("perm_yes") for a in actions)
    assert any(a.startswith("perm_no") for a in actions)


@pytest.mark.parametrize(
    "length,always_expected",
    [(52, True), (53, False)],  # "perm_always:" (12) + id must fit 64 bytes
)
def test_the_always_button_fits_exactly_up_to_the_payload_limit(length, always_expected):
    markup = keyboards.permission_buttons("r" * length)
    actions = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert any(a.startswith("perm_always") for a in actions) is always_expected


def test_help_buttons():
    markup = keyboards.help_buttons()
    labels = [b["text"] for row in markup["inline_keyboard"] for b in row]
    assert labels == ["Status", "Sessions", "Help"]


def test_router_dispatches():
    router = keyboards.Router()
    seen = []

    @router.on("ping")
    def _ping(chat_id, argument):
        seen.append((chat_id, argument))

    assert router.resolve("ping") is not None
    router.resolve("ping")("42", "x")
    assert seen == [("42", "x")]


def test_router_returns_none_for_unknown_actions():
    assert keyboards.Router().resolve("nope") is None


def test_router_lists_its_actions():
    router = keyboards.Router()

    @router.on("a")
    def _a(chat_id, argument):
        pass

    @router.on("b")
    def _b(chat_id, argument):
        pass

    assert router.actions() == ["a", "b"]


def test_every_menu_label_maps_to_a_real_command():
    """A tap on the persistent keyboard desugars to /command via
    MENU_LABELS; one that does not exist in REGISTRY queues the literal
    label word as an agent prompt - silent, and baffling."""
    from bridgekit import commands

    missing = {
        label
        for label in keyboards.MENU_LABELS.values()
        if label not in commands.REGISTRY and label not in commands.DISPATCH_ALIASES
    }
    assert not missing, f"menu labels point at commands that do not exist: {missing}"
