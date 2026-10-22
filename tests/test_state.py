"""Per-chat state persistence and the legacy migration."""

from __future__ import annotations

import json
import time

from bridgekit.state import State


def test_roundtrip_preserves_session_and_directory(tmp_path):
    path = tmp_path / ".state.json"
    first = State(path, "/home/demo")
    first.load()
    first.set_session("42", "ses_abc", directory="/home/demo/Desktop")

    second = State(path, "/home/demo")
    second.load()
    assert second.session("42") == "ses_abc"
    assert second.directory("42") == "/home/demo/Desktop"


def test_unknown_chat_gets_the_default_directory(tmp_path):
    state = State(tmp_path / ".state.json", "/home/demo")
    state.load()
    assert state.session("99") is None
    assert state.directory("99") == "/home/demo"


def test_chats_are_independent(tmp_path):
    state = State(tmp_path / ".state.json", "/home/demo")
    state.load()
    state.set_session("42", "ses_one")
    state.set_session("43", "ses_two")
    assert state.session("42") == "ses_one"
    assert state.session("43") == "ses_two"


def test_legacy_session_file_is_migrated(tmp_path):
    (tmp_path / ".session.json").write_text(json.dumps({"sessionID": "ses_legacy"}))

    state = State(tmp_path / ".state.json", "/home/demo")
    state.load()
    assert (tmp_path / ".state.json").exists()

    legacy = state.take_legacy()
    assert legacy is not None and legacy.session == "ses_legacy"
    assert state.session("42") is None  # not bound until the caller adopts it


def test_take_legacy_returns_none_when_absent(tmp_path):
    state = State(tmp_path / ".state.json", "/home/demo")
    state.load()
    assert state.take_legacy() is None


def test_owner_of_finds_the_chat_holding_a_session(tmp_path):
    state = State(tmp_path / ".state.json", "/home/demo")
    state.load()
    state.set_session("42", "ses_one")
    state.set_session("43", "ses_two")
    assert state.owner_of("ses_two") == "43"
    assert state.owner_of("ses_missing") is None


def test_owner_of_ignores_the_legacy_placeholder(tmp_path):
    state = State(tmp_path / ".state.json", "/home/demo")
    state.load()
    (tmp_path / ".session.json").write_text(json.dumps({"sessionID": "ses_legacy"}))
    state2 = State(tmp_path / ".state.json", "/home/demo")
    state2.load()
    assert state2.owner_of("ses_legacy") is None


def test_corrupt_state_file_does_not_crash(tmp_path):
    path = tmp_path / ".state.json"
    path.write_text("{not json")
    state = State(path, "/home/demo")
    state.load()
    assert state.session("42") is None


def test_busy_marker_is_persisted_for_crash_detection(tmp_path):
    """A turn in flight must survive a crash so the next boot can report it."""
    state = State(tmp_path / ".state.json", "/home/demo")
    state.load()
    state.set_session("42", "ses_one")
    state.set_busy("42", "ses_one", started=1.0)

    stored = json.loads((tmp_path / ".state.json").read_text())
    assert stored["chats"]["42"]["busy"]["session"] == "ses_one"

    state.clear_busy("42")
    stored = json.loads((tmp_path / ".state.json").read_text())
    assert stored["chats"]["42"]["busy"] is None


def test_busy_marker_survives_a_reload(tmp_path):
    state = State(tmp_path / ".state.json", "/home/demo")
    state.load()
    state.set_session("42", "ses_one")
    state.set_busy("42", "ses_one", started=1.0)

    reloaded = State(tmp_path / ".state.json", "/home/demo")
    reloaded.load()
    assert reloaded.busy("42")["session"] == "ses_one"


def test_orphaned_turns_are_reported(tmp_path):
    state = State(tmp_path / ".state.json", "/home/demo")
    state.load()
    state.set_session("42", "ses_one")
    state.set_busy("42", "ses_one", started=time.time() - 900)

    orphans = state.orphaned_turns()
    assert len(orphans) == 1
    assert orphans[0][0] == "42"
    assert orphans[0][1]["session"] == "ses_one"


def test_writes_are_atomic(tmp_path):
    state = State(tmp_path / ".state.json", "/home/demo")
    state.load()
    state.set_session("42", "ses_one")
    assert not list(tmp_path.glob("*.tmp"))


def test_a_non_dict_state_file_falls_back_to_fresh_state(tmp_path):
    """A hand-edited file could hold an array or a bare string; that must
    behave like a missing file, not crash every boot. The garbage is left
    alone until the next real save replaces it atomically."""
    path = tmp_path / ".state.json"
    path.write_text(json.dumps([1, 2, 3]))
    state = State(path, "/home/demo")
    state.load()
    assert state.session("99") is None  # nothing raised, nothing migrated
    assert state.load_offset() == 0


def test_a_policy_field_of_the_wrong_type_loads_as_empty(tmp_path):
    path = tmp_path / ".state.json"
    path.write_text(json.dumps({"version": 2, "policy": "garbage"}))
    state = State(path, "/home/demo")
    state.load()
    assert state.load_policy() == {}
    assert state.load_offset() == 0


def test_chats_of_the_wrong_type_are_ignored(tmp_path):
    path = tmp_path / ".state.json"
    path.write_text(json.dumps({"version": 2, "chats": ["nope"], "policy": {}}))
    state = State(path, "/home/demo")
    state.load()
    assert state.session("42") is None


def test_garbage_chat_entries_are_skipped(tmp_path):
    path = tmp_path / ".state.json"
    path.write_text(json.dumps({"version": 2, "chats": {"42": "nope", "43": {}}}))
    state = State(path, "/home/demo")
    state.load()
    assert state.directory("43") == "/home/demo"  # present + defaulted
    assert state.chats() == ["43"]


def test_stale_legacy_mirror_is_removed_when_sessions_end(tmp_path):
    state = State(tmp_path / ".state.json", "/home/demo")
    state.load()
    state.set_session("42", "ses_one")
    state.set_session("43", "ses_two")
    mirror = tmp_path / ".session.json"
    assert json.loads(mirror.read_text())["sessionID"] == "ses_two"

    state.set_session("42", None)
    assert mirror.exists()
    state.set_session("43", None)
    # A mirror pointing at a deleted session is worse than no mirror.
    assert not mirror.exists()
