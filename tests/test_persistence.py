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

    from bridgekit.telegram import Telegram, TelegramError

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

    # The transport death now surfaces as a user-safe TelegramError instead of
    # the raw requests exception, whose text embeds the tokened request URL
    # and used to land in the chat. The partial-file cleanup is unchanged.
    with pytest.raises(TelegramError) as caught:
        client.download("f1", destination)
    assert "token" not in str(caught.value)
    assert not destination.exists(), "partial file must be cleaned up"


def test_save_policy_preserves_the_telegram_offset(tmp_path):
    """save_policy() used to replace the whole dict - and the offset lives there.
    One /policy edit and a restart replayed every retained update as a
    duplicate turn."""
    path = tmp_path / ".state.json"
    state = State(path, "/tmp")
    state.load()
    state.save_offset(4242)
    state.save_policy({"mode": "all", "deny": [], "rules": {}})
    assert state.load_offset() == 4242

    reloaded = State(path, "/tmp")
    reloaded.load()
    assert reloaded.load_offset() == 4242, "a policy edit must not un-acknowledge updates"


def test_an_explicit_offset_in_a_policy_snapshot_wins(tmp_path):
    state = State(tmp_path / ".state.json", "/tmp")
    state.load()
    state.save_offset(10)
    state.save_policy({"mode": "all", "deny": [], "rules": {}, "update_offset": 11})
    assert state.load_offset() == 11


def test_a_cleared_deny_list_stays_cleared_after_a_restart(tmp_path):
    """The guard's default list must not resurrect entries the user cleared."""
    path = tmp_path / ".state.json"
    state = State(path, "/tmp")
    state.load()
    policy = Policy()
    policy.deny = []  # a real setting, not bad data
    policy._compile()
    state.save_policy(policy.snapshot())

    reloaded = State(path, "/tmp")
    reloaded.load()
    restored = Policy()
    restored.restore(reloaded.load_policy())
    assert restored.deny == []
    assert restored.blocked_command("sudo apt update") is None
