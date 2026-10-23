"""Authorization boundaries across every way into the bridge.

The allow-list is the only gate between Telegram and a shell. These tests
drive the real update dispatcher, not individual handlers, so a path that
forgets the check fails here: prompts, attachments, buttons and reactions all
have to agree on who is allowed to act.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from test_delivery import CHAT, FakeTelegram, build_bridge

STRANGER = "999999"


class RecordingTelegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.downloads: list[str] = []

    def download(self, file_id, destination):
        self.downloads.append(file_id)
        Path(destination).write_bytes(b"x")
        return Path(destination)


def message(text="hello", chat_id=CHAT, sender=CHAT, **extra):
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": int(chat_id)},
            "from": {"id": int(sender)},
            "text": text,
            **extra,
        },
    }


def document(chat_id=CHAT, sender=CHAT):
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": int(chat_id)},
            "from": {"id": int(sender)},
            "document": {"file_id": "f1", "file_name": "notes.txt", "file_size": 1},
        },
    }


def test_a_stranger_in_an_allowed_chat_cannot_send_a_prompt(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())

    bridge._handle_update(message(sender=STRANGER))

    assert bridge.work.empty()
    assert bridge.telegram.sent == []


def test_a_stranger_in_an_allowed_chat_cannot_upload_an_attachment(tmp_path):
    telegram = RecordingTelegram()
    bridge = build_bridge(tmp_path, telegram)

    bridge._handle_update(document(sender=STRANGER))

    assert telegram.downloads == []
    assert bridge.work.empty()


def test_an_allowed_sender_still_reaches_the_worker(tmp_path):
    bridge = build_bridge(tmp_path, FakeTelegram())

    bridge._handle_update(message(text="real work"))

    chat_id, text, _files = bridge.work.get(timeout=1)
    assert (chat_id, text) == (CHAT, "real work")


def test_a_group_needs_both_the_chat_and_the_user(tmp_path):
    """Allow-listing a group id alone does not trust every member."""
    bridge = build_bridge(tmp_path, FakeTelegram())
    bridge.config = replace(bridge.config, allowed_chats=frozenset({"42", STRANGER}))

    # The user is listed but the chat is not: still a no.
    bridge._handle_update(message(chat_id="777", sender=STRANGER))
    assert bridge.work.empty()

    # Both listed: the same update is accepted.
    bridge._handle_update(message(chat_id="42", sender=STRANGER, text="group work"))
    chat_id, text, _files = bridge.work.get(timeout=1)
    assert (chat_id, text) == ("42", "group work")


def test_the_shared_rule_requires_both_ids():
    """One function is the rule for every update type."""
    from bridgekit.authz import authorize

    allowed = frozenset({"42", "99"})

    assert authorize({"id": 42}, {"id": 42}, allowed) == "42"
    assert authorize({"id": 42}, {"id": 99}, allowed) == "42"
    assert authorize({"id": 777}, {"id": 777}, allowed) == ""
    assert authorize({"id": 42}, {"id": 7}, allowed) == ""
    assert authorize(None, {"id": 42}, allowed) == ""
    # Channel posts have no `from`; the chat id is the actor fallback.
    assert authorize({"id": 42}, None, allowed) == "42"
