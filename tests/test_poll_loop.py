"""The poll loop, driven end to end.

Every new Telegram surface (messages, reactions, edits) funnels through
one loop, and the old audit lesson applies to tests too: a fake that only
exercises the handler never proves the loop delivers updates in the shape
Telegram actually produces - several, in one batch, each carrying its own
update_id. So this drives poll_loop itself through realistic batches.
"""

from __future__ import annotations

from test_delivery import CHAT, FakeTelegram, build_bridge

MSG = {"update_id": 1, "message": {"chat": {"id": int(CHAT)}, "text": "hello there"}}

NEUTRAL_REACTION = {
    "update_id": 2,
    "message_reaction": {
        "chat": {"id": int(CHAT)},
        "message_id": 5,
        "user": {"id": int(CHAT)},
        "date": 1,
        "new_reaction": [{"emoji": "❤️"}],
        "old_reaction": [],
    },
}

EDIT = {
    "update_id": 9,
    "edited_message": {
        "chat": {"id": int(CHAT)},
        "message_id": 9,
        "from": {"id": int(CHAT), "is_bot": False},
        "text": "make the header red instead",
    },
}


class BatchTelegram(FakeTelegram):
    """Serves canned batches, records offsets, stops the loop when spent."""

    def __init__(self, batches):
        super().__init__()
        self.batches = list(batches)
        self.offsets: list[int] = []
        self.stopper = None  # set to bridge.stop_event by the test

    def get_updates(self, offset, timeout=30):
        self.offsets.append(offset)
        if self.batches:
            return self.batches.pop(0)
        if self.stopper is not None:
            self.stopper.set()
        return []


def test_poll_loop_processes_a_mixed_batch(tmp_path):
    """One getUpdates batch can carry any mix of update types."""
    bridge = build_bridge(tmp_path, BatchTelegram([[MSG, NEUTRAL_REACTION]]))
    bridge.telegram.stopper = bridge.stop_event
    bridge.poll_loop()

    # The prompt reached the work queue; the neutral reaction stayed silent.
    chat_id, text, files = bridge.work.get(timeout=1)
    assert (chat_id, text) == (CHAT, "hello there")
    assert bridge.telegram.sent == []

    # Offsets progress per batch and the state records where we got to.
    assert bridge.telegram.offsets == [0, 3]
    assert bridge.state.load_offset() == 3


def test_poll_loop_survives_updates_after_a_batch(tmp_path):
    """Delivering the batch does not end the loop; spent polls do, via stop."""
    bridge = build_bridge(tmp_path, BatchTelegram([[MSG], [EDIT]]))
    bridge.telegram.stopper = bridge.stop_event
    bridge.poll_loop()

    first, second = bridge.work.get(timeout=1), bridge.work.get(timeout=1)
    assert first[1] == "hello there"
    assert second[1] == "make the header red instead"
    assert bridge.telegram.offsets == [0, 2, 10]
    assert bridge.state.load_offset() == 10


def test_poll_loop_resumes_from_the_saved_offset(tmp_path):
    """A restart must not re-consume an acknowledged update."""
    bridge = build_bridge(tmp_path, BatchTelegram([[MSG]]))
    bridge.telegram.stopper = bridge.stop_event
    bridge.state.save_offset(500)
    bridge.poll_loop()

    assert bridge.telegram.offsets[0] == 500
