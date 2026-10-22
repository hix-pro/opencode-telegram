"""Inline keyboards and callback routing."""

from __future__ import annotations

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
