"""Settings that have to survive a restart.

Four of these were silent: a rule you set would simply be gone after a reboot,
and a restart could replay a prompt you had already sent.
"""

from __future__ import annotations

import json

import pytest

from bridgekit.policy import Policy
from bridgekit.state import State

# ------------------------------------------------------------- policy rules


def test_mode_rules_and_denies_survive_a_reload(tmp_path):
    path = tmp_path / ".state.json"

    first = State(path, "/tmp")
    first.load()
    policy = Policy(mode="ask")
    policy.set_mode("read")
    policy.set_rule("42", "edit", "always")
    policy.add_deny(r"\bterraform\b")
    first.save_policy(policy.snapshot())

    second = State(path, "/tmp")
    second.load()
    restored = Policy(mode="ask")
    restored.restore(second.load_policy())

    assert restored.mode == "read"
    assert restored.rules_for("42") == {"edit": "always"}
    assert restored.blocked_command("terraform plan") is not None


def test_default_denies_survive_alongside_custom_ones(tmp_path):
    path = tmp_path / ".state.json"
    state = State(path, "/tmp")
    state.load()
    policy = Policy()
    policy.add_deny(r"\bmytool\b")
    state.save_policy(policy.snapshot())

    reloaded = State(path, "/tmp")
    reloaded.load()
    restored = Policy()
    restored.restore(reloaded.load_policy())

    assert restored.blocked_command("mytool run") is not None
    assert restored.blocked_command("rm -rf /") is not None, "defaults must survive"


def test_restore_ignores_garbage(tmp_path):
    policy = Policy()
    policy.restore({"mode": "nonsense", "deny": "not a list", "rules": 42})
    assert policy.mode == "ask"


def test_restore_of_nothing_is_a_no_op():
    policy = Policy(mode="all")
    policy.restore({})
    assert policy.mode == "all"


def test_rules_are_scoped_per_chat(tmp_path):
    path = tmp_path / ".state.json"
    state = State(path, "/tmp")
    state.load()
    policy = Policy()
    policy.set_rule("42", "edit", "always")
    policy.set_rule("43", "edit", "reject")
    state.save_policy(policy.snapshot())

    reloaded = State(path, "/tmp")
    reloaded.load()
    restored = Policy()
    restored.restore(reloaded.load_policy())
    assert restored.rules_for("42")["edit"] == "always"
    assert restored.rules_for("43")["edit"] == "reject"


def test_policy_is_stored_at_the_top_level_not_per_chat(tmp_path):
    path = tmp_path / ".state.json"
    state = State(path, "/tmp")
    state.load()
    state.set_session("42", "ses_x")
    state.save_policy({"mode": "all", "deny": [], "rules": {}})

    payload = json.loads(path.read_text())
    assert payload["policy"]["mode"] == "all"
    assert "policy" not in payload["chats"]["42"]


# --------------------------------------------------------- telegram offset


def test_update_offset_round_trips(tmp_path):
    path = tmp_path / ".state.json"
    state = State(path, "/tmp")
    state.load()
    state.save_offset(1234)
    assert state.load_offset() == 1234


def test_offset_defaults_to_zero(tmp_path):
    state = State(tmp_path / ".state.json", "/tmp")
    state.load()
    assert state.load_offset() == 0


def test_offset_is_survived_by_a_reload(tmp_path):
    path = tmp_path / ".state.json"
    state = State(path, "/tmp")
    state.load()
    state.save_offset(999)

    reloaded = State(path, "/tmp")
    reloaded.load()
    assert reloaded.load_offset() == 999, "a restart must not replay prompts"


def test_saving_the_same_offset_does_not_rewrite(tmp_path):
    path = tmp_path / ".state.json"
    state = State(path, "/tmp")
    state.load()
    state.save_offset(50)
    before = path.stat().st_mtime_ns
    state.save_offset(50)
    assert path.stat().st_mtime_ns == before


def test_a_corrupt_offset_falls_back_to_zero(tmp_path):
    path = tmp_path / ".state.json"
    state = State(path, "/tmp")
    state.load()
    state.save_policy({"update_offset": "not a number"})
    assert state.load_offset() == 0


# ---------------------------------------------------- attachment clean-up


def test_a_failed_download_leaves_no_partial_file(tmp_path):
    """A truncated attachment would be read by the agent as if it were whole."""
    import requests

    from bridgekit.telegram import Telegram

    class MidStream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=1):
            yield b"partial"
            raise requests.exceptions.ConnectionError("stream died")

    client = Telegram("token")

    def fake_get(url, **kwargs):
        if "getFile" in url:
            resp = requests.Response()
            resp.status_code = 200
            resp._content = b'{"ok": true, "result": {"file_path": "a/b.png"}}'
            return resp
        return MidStream()

    client._http = type("S", (), {"get": staticmethod(fake_get)})()
    destination = tmp_path / "b.png"

    with pytest.raises(requests.exceptions.ConnectionError):
        client.download("f1", destination)
    assert not destination.exists(), "partial file must be cleaned up"
