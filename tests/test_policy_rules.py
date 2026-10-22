"""Per-action permission rules.

The point of a rule is to let someone trust one class of action without
trusting everything, so the tests care most about precedence: a rule must beat
the mode, and the guard must beat the rule.
"""

from __future__ import annotations

import pytest

from bridgekit.commands import REGISTRY, CommandError, run
from bridgekit.policy import ALL, ASK, Policy

EDIT = {"id": "p1", "sessionID": "s", "action": "edit", "resources": ["a.py"]}
BASH = {"id": "p2", "sessionID": "s", "action": "bash", "resources": ["ls -la"]}
READ_REQ = {"id": "p3", "sessionID": "s", "action": "read", "resources": ["a.py"]}
CHAT = "42"


def test_policy_command_is_registered():
    assert "policy" in REGISTRY


# ---------------------------------------------------------------- precedence


def test_rule_overrides_the_mode():
    policy = Policy(mode=ASK)
    policy.set_rule(CHAT, "edit", "always")
    assert policy.decide(EDIT, CHAT) == "always"


def test_rule_asking_overrides_permissive_mode():
    policy = Policy(mode=ALL)
    policy.set_rule(CHAT, "edit", "ask")
    assert policy.decide(EDIT, CHAT) is None


def test_mode_still_applies_without_a_rule():
    policy = Policy(mode=ALL)
    policy.set_rule(CHAT, "edit", "always")
    assert policy.decide(BASH, CHAT) == "always"


def test_guard_beats_an_allow_rule():
    """The one precedence that must never bend."""
    policy = Policy(mode=ALL)
    policy.set_rule(CHAT, "bash", "always")
    dangerous = {"action": "bash", "resources": ["rm -rf /"]}
    assert policy.decide(dangerous, CHAT) == "reject"


def test_guard_beats_a_deny_rule_too():
    policy = Policy(mode=ALL)
    policy.set_rule(CHAT, "bash", "reject")
    assert policy.decide(BASH, CHAT) == "reject"


def test_rules_are_scoped_to_one_chat():
    policy = Policy(mode=ASK)
    policy.set_rule(CHAT, "edit", "always")
    assert policy.decide(EDIT, "99") is None
    assert policy.decide(EDIT, CHAT) == "always"


def test_wildcard_rule_catches_everything():
    policy = Policy(mode=ASK)
    policy.set_rule(CHAT, "*", "once")
    assert policy.decide(EDIT, CHAT) == "once"
    assert policy.decide(BASH, CHAT) == "once"


def test_specific_rule_beats_the_wildcard():
    policy = Policy(mode=ASK)
    policy.set_rule(CHAT, "*", "once")
    policy.set_rule(CHAT, "bash", "ask")
    assert policy.decide(BASH, CHAT) is None
    assert policy.decide(EDIT, CHAT) == "once"


def test_action_matching_is_case_insensitive():
    policy = Policy()
    policy.set_rule(CHAT, "EDIT", "always")
    assert policy.decide(EDIT, CHAT) == "always"


def test_deny_rule_rejects_without_asking():
    policy = Policy(mode=ASK)
    policy.set_rule(CHAT, "bash", "reject")
    assert policy.decide(BASH, CHAT) == "reject"


# ------------------------------------------------------------------ commands


def test_policy_lists_examples_when_empty(ctx):
    reply = run(ctx, "/policy")
    assert "No per-action rules" in reply
    assert "/policy always edit" in reply


def test_policy_sets_a_rule(ctx):
    assert "edit: always allowed" in run(ctx, "/policy always edit")
    assert ctx.policy.decide(EDIT, ctx.chat_id) == "always"


def test_policy_allow_maps_to_once(ctx):
    run(ctx, "/policy allow read")
    assert ctx.policy.decide(READ_REQ, ctx.chat_id) == "once"


def test_policy_deny_maps_to_reject(ctx):
    run(ctx, "/policy deny bash")
    assert ctx.policy.decide(BASH, ctx.chat_id) == "reject"


def test_policy_shows_existing_rules(ctx):
    run(ctx, "/policy always edit")
    reply = run(ctx, "/policy")
    assert "edit: always" in reply
    assert "override mode" in reply


def test_policy_clear_removes_one_rule(ctx):
    run(ctx, "/policy always edit")
    run(ctx, "/policy allow read")
    run(ctx, "/policy clear edit")
    reply = run(ctx, "/policy")
    assert "edit:" not in reply
    assert "read: once" in reply


def test_policy_reset_clears_everything(ctx):
    run(ctx, "/policy always edit")
    run(ctx, "/policy allow read")
    run(ctx, "/policy reset")
    assert "No per-action rules" in run(ctx, "/policy")


def test_policy_requires_an_action(ctx):
    with pytest.raises(CommandError, match="Common actions"):
        run(ctx, "/policy always")


def test_policy_rejects_a_bad_verb(ctx):
    with pytest.raises(CommandError, match="Usage"):
        run(ctx, "/policy maybe edit")


def test_policy_clear_needs_a_known_action(ctx):
    with pytest.raises(CommandError, match="No rule"):
        run(ctx, "/policy clear bash")


def test_policy_clear_lists_what_exists(ctx):
    with pytest.raises(CommandError, match="Current rules"):
        run(ctx, "/policy clear")


def test_wildcard_is_documented(ctx):
    run(ctx, "/policy deny *")
    assert ctx.policy.decide(EDIT, ctx.chat_id) == "reject"


def test_status_surfaces_rules(ctx):
    run(ctx, "/policy always edit")
    assert "edit: always" in run(ctx, "/policy")
