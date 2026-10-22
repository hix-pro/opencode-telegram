"""The poll loop, driven end to end.

Every new Telegram surface (messages, reactions, edits) funnels through
one loop, and the old audit lesson applies to tests too: a fake that only
exercises the handler never proves the loop delivers updates in the shape
Telegram actually produces - several, in one batch, each carrying its own
update_id. So this drives poll_loop itself through realistic batches.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import requests
from test_delivery import CHAT, FakeTelegram, build_bridge

import bridge as bridge_module

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


class WobbleTelegram(FakeTelegram):
    """Raises HTTP errors before serving batches, like a Telegram outage."""

    def __init__(self, failures):
        super().__init__()
        self.failures = list(failures)
        self.batches = []
        self.offsets: list[int] = []
        self.stopper = None

    def get_updates(self, offset, timeout=30):
        self.offsets.append(offset)
        if self.failures:
            status, retry_after = self.failures.pop(0)
            exc = requests.HTTPError(f"HTTP {status} Error")
            exc.response = SimpleNamespace(
                status_code=status,
                json=lambda: {"parameters": {"retry_after": retry_after}},
            )
            raise exc
        if self.batches:
            return self.batches.pop(0)
        if self.stopper is not None:
            self.stopper.set()
        return []


def test_throttling_and_server_hiccups_are_transient(tmp_path, monkeypatch):
    """429 and 5xx used to kill the bridge, whose restart loop then paid the
    full boot cost and re-announced itself every few seconds mid-outage."""
    slept = []
    monkeypatch.setattr(bridge_module.time, "sleep", slept.append)

    bridge = build_bridge(tmp_path, WobbleTelegram([(429, 7), (500, 0), (503, 0)]))
    bridge.telegram.batches = [[MSG]]
    bridge.telegram.stopper = bridge.stop_event
    bridge.poll_loop()

    assert slept == [7, 5, 5], "Telegram's retry_after wins, everything else sleeps 5"
    assert bridge.fatal is None, "transient failures must not stop the bridge"
    chat_id, text, files = bridge.work.get(timeout=1)
    assert text == "hello there"
    assert bridge.state.load_offset() == 2


def test_a_rejected_token_is_still_fatal(tmp_path):
    """401/403/409 must keep tripping the systemd start limit - retrying a
    bad token forever just fills the journal."""
    bridge = build_bridge(tmp_path, WobbleTelegram([(401, 0)]))
    bridge.poll_loop()
    assert "401" in bridge.fatal


def test_a_mid_batch_crash_keeps_every_acked_update(tmp_path, monkeypatch):
    """The offset used to be saved once per batch: a crash between two
    updates of one batch replayed the earlier ones as duplicate turns."""
    bridge = build_bridge(tmp_path, BatchTelegram([[MSG, NEUTRAL_REACTION]]))

    calls = {"n": 0}
    real_handler = bridge._handle_update

    def dying_handler(update):
        if calls["n"] == 1:  # the reaction: second in the batch
            raise RuntimeError("mid-batch death")
        calls["n"] += 1
        return real_handler(update)

    bridge._handle_update = dying_handler
    with pytest.raises(RuntimeError):
        bridge.poll_loop()

    assert bridge.state.load_offset() == 2, "the message already handled must stay acked"


def test_every_poll_iteration_stamps_the_heartbeat(tmp_path):
    """The heartbeat is the watchdog's only view of a wedged poll thread: it
    must be touched every iteration, success or failure alike (touched BEFORE
    the long poll, so a bridge facing a Telegram outage still beats)."""
    bridge = build_bridge(tmp_path, BatchTelegram([[MSG], []]))
    bridge.telegram.stopper = bridge.stop_event
    bridge.poll_loop()

    heartbeat = tmp_path / ".bridge-heartbeat"
    assert heartbeat.exists(), "the watchdog needs a stamp even without updates"
    assert abs(time.time() - heartbeat.stat().st_mtime) < 30
