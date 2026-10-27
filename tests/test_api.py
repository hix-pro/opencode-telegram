"""API client helpers that do not need the network."""

from __future__ import annotations

import pytest

from bridgekit.api import PERMISSION_ALWAYS, PERMISSION_ONCE, ApiError, OpenCode


def test_model_ref_requires_provider_and_id():
    assert OpenCode._model_ref("opencode/claude-haiku-5-5") == {
        "providerID": "opencode",
        "id": "claude-haiku-5-5",
    }


def test_model_ref_parses_a_variant():
    assert OpenCode._model_ref("opencode/claude-haiku-5-5#max") == {
        "providerID": "opencode",
        "id": "claude-haiku-5-5",
        "variant": "max",
    }


def test_model_ref_rejects_a_bare_model_name():
    with pytest.raises(ValueError, match="provider/model"):
        OpenCode._model_ref("claude-haiku-5-5")


def test_model_ref_rejects_an_empty_provider():
    """/model alone used to build {"providerID": "", "id": "model"} and get a
    raw API error back; it is refused with the usage message instead."""
    with pytest.raises(ValueError, match="provider/model"):
        OpenCode._model_ref("/model")


def test_permission_decisions_are_constrained():
    api = OpenCode("http://127.0.0.1:1", "pw")
    with pytest.raises(ValueError, match="Invalid permission decision"):
        api.reply_permission("s", "r", "maybe")


def test_permission_constants_match_the_server_enum():
    assert (PERMISSION_ONCE, PERMISSION_ALWAYS) == ("once", "always")


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_error_message_prefers_the_message_field():
    resp = FakeResponse(404, {"_tag": "FileNotFoundError", "message": "File not found: x"})
    assert OpenCode._error_message(resp) == "File not found: x"


def test_error_message_appends_the_path():
    resp = FakeResponse(404, {"message": "nope", "path": "some/file"})
    assert OpenCode._error_message(resp) == "nope some/file"


def test_error_message_falls_back_to_raw_text():
    resp = FakeResponse(502, None, text="bad gateway")
    assert OpenCode._error_message(resp) == "bad gateway"


def test_latest_assistant_text_joins_text_blocks():
    messages = [
        {
            "type": "assistant",
            "time": {"created": 2000},
            "content": [
                {"type": "reasoning", "text": "thinking..."},
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
            ],
        }
    ]
    assert OpenCode.latest_assistant_text(messages, since=1000) == "hello world"


def test_latest_assistant_text_ignores_reasoning_only_messages():
    messages = [
        {
            "type": "assistant",
            "time": {"created": 2000},
            "content": [{"type": "reasoning", "text": "hmm"}],
        }
    ]
    assert OpenCode.latest_assistant_text(messages, since=1000) is None


def test_latest_assistant_text_respects_the_since_boundary():
    messages = [
        {
            "type": "assistant",
            "time": {"created": 500},
            "content": [{"type": "text", "text": "old turn"}],
        }
    ]
    assert OpenCode.latest_assistant_text(messages, since=1000) is None


def test_connection_failures_become_api_errors():
    api = OpenCode("http://127.0.0.1:1", "pw", timeout=1)
    with pytest.raises(ApiError) as excinfo:
        api.info()
    assert excinfo.value.status == 0


# --- regression: one turn is many assistant messages -------------------------
# A turn routinely produces several assistant messages (narrate, tool, narrate).
# Returning only the first match discarded the rest of the reply.


def _assistant(created, text):
    return {
        "type": "assistant",
        "id": f"msg_{created}",
        "time": {"created": created},
        "content": [{"type": "text", "text": text}],
    }


def test_assistant_text_joins_every_fragment_of_the_turn():
    messages = [
        _assistant(2000, "first"),
        _assistant(3000, "second"),
        _assistant(4000, "third"),
    ]
    assert OpenCode.assistant_text(messages, since=1000) == "first\n\nsecond\n\nthird"


def test_assistant_text_orders_fragments_by_time_not_by_list_order():
    # The bridge asks the server for order="desc"; reading that list in place
    # produced the reply backwards.
    desc = [
        _assistant(4000, "third"),
        _assistant(3000, "second"),
        _assistant(2000, "first"),
    ]
    expected = "first\n\nsecond\n\nthird"
    assert OpenCode.assistant_text(desc, since=1000) == expected
    assert OpenCode.assistant_text(list(reversed(desc)), since=1000) == expected


def test_assistant_text_skips_reasoning_only_messages():
    messages = [
        {
            "type": "assistant",
            "time": {"created": 2000},
            "content": [{"type": "reasoning", "text": "thinking"}],
        },
        _assistant(3000, "the answer"),
    ]
    assert OpenCode.assistant_text(messages, since=1000) == "the answer"


def test_assistant_text_is_none_without_fragments():
    assert OpenCode.assistant_text([{"type": "idle"}], since=1000) is None


def test_latest_assistant_text_still_returns_a_single_fragment():
    messages = [_assistant(2000, "first"), _assistant(4000, "third")]
    assert OpenCode.latest_assistant_text(messages, since=1000) == "third"
