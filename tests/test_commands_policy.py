"""Policy, search, cost, and health commands."""

from __future__ import annotations

import pytest

from bridgekit.commands import REGISTRY, CommandError, run
from bridgekit.policy import Policy

PERM = {
    "id": "perm_1",
    "sessionID": "ses_aaaaaaaaaaaaaaaa",
    "action": "read",
    "resources": ["a.py"],
}


def test_new_commands_are_registered():
    for name in ("auto", "guard", "watch", "search", "cost", "alert", "health"):
        assert name in REGISTRY


# ---------------------------------------------------------------------- /auto


def test_auto_reports_the_current_mode(ctx):
    reply = run(ctx, "/auto")
    assert "Auto-approval: ask" in reply


def test_auto_switches_mode(ctx):
    assert "read" in run(ctx, "/auto read")
    assert ctx.policy.mode == "read"


def test_auto_accepts_friendly_aliases(ctx):
    run(ctx, "/auto off")
    assert ctx.policy.mode == "ask"
    run(ctx, "/auto full")
    assert ctx.policy.mode == "all"


def test_auto_rejects_nonsense(ctx):
    with pytest.raises(CommandError, match="Usage"):
        run(ctx, "/auto whatever")


def test_auto_persists_per_chat(ctx):
    run(ctx, "/auto read")
    assert ctx.state.pref("42", "auto") == "read"
    assert ctx.state.pref("99", "auto") is None


# --------------------------------------------------------------------- /guard


def test_guard_lists_patterns(ctx):
    reply = run(ctx, "/guard")
    assert "rm" in reply


def test_guard_add_and_remove(ctx):
    run(ctx, "/guard add \\bterraform\\s+destroy\\b")
    assert ctx.policy.blocked_command("terraform destroy") is not None
    run(ctx, "/guard rm \\bterraform\\s+destroy\\b")
    assert ctx.policy.blocked_command("terraform destroy") is None


def test_guard_tests_without_running_anything(ctx):
    assert "BLOCKED" in run(ctx, "/guard test rm -rf /")
    assert "Allowed" in run(ctx, "/guard test ls -la")


def test_guard_add_requires_a_pattern(ctx):
    with pytest.raises(CommandError, match="Usage"):
        run(ctx, "/guard add")


def test_guard_unknown_action(ctx):
    with pytest.raises(CommandError, match="Usage"):
        run(ctx, "/guard frobnicate")


# --------------------------------------------------------------------- /watch


def test_watch_toggles(ctx):
    run(ctx, "/watch on")
    assert ctx.state.pref("42", "watch") is True
    run(ctx, "/watch off")
    assert ctx.state.pref("42", "watch") is False


def test_watch_toggles_when_given_no_argument(ctx):
    """Live progress defaults on, so the first bare /watch turns it off."""
    first = run(ctx, "/watch")
    second = run(ctx, "/watch")
    assert "off" in first and "on" in second


# -------------------------------------------------------------------- /search


def test_search_lists_matches(ctx):
    reply = run(ctx, "/search bridge")
    assert "match(es)" in reply
    assert any("list_sessions" == call[0] for call in ctx.api.calls)


def test_search_handles_no_results(ctx):
    ctx.api.sessions = []
    assert "Nothing matching" in run(ctx, "/search zzz")


def test_search_finds_text_inside_a_conversation(ctx):
    """Titles alone used to be searched, so body text was invisible."""
    ctx.api.sessions = []
    ctx.api.search_session_text = lambda q, directory=None, sessions=10: [
        ("ses_abcdefghijklmnop", "the answer involves turbulence and drag")
    ]
    reply = run(ctx, "/search turbulence")
    assert "turbulence" in reply
    assert "Switch with /use" in reply


def test_search_requires_a_query(ctx):
    with pytest.raises(CommandError, match="Usage"):
        run(ctx, "/search")


# ---------------------------------------------------------------------- /cost


def test_cost_reports_usage(ctx):
    ctx.state.set_session("42", "ses_aaaaaaaaaaaaaaaa")
    reply = run(ctx, "/cost")
    assert "Session cost" in reply
    assert "Tokens" in reply


def test_cost_needs_a_session(ctx):
    with pytest.raises(CommandError, match="No active session"):
        run(ctx, "/cost")


def test_alert_sets_and_clears(ctx):
    run(ctx, "/alert 1.50")
    assert ctx.state.pref("42", "cost_alert") == 1.5
    run(ctx, "/alert off")
    assert ctx.state.pref("42", "cost_alert") == 0.0


def test_alert_accepts_a_dollar_prefix(ctx):
    run(ctx, "/alert $2")
    assert ctx.state.pref("42", "cost_alert") == 2.0


def test_alert_rejects_junk(ctx):
    with pytest.raises(CommandError, match="Usage"):
        run(ctx, "/alert lots")


def test_alert_reports_when_unset(ctx):
    assert "off" in run(ctx, "/alert")


# -------------------------------------------------------------------- /health


def test_health_reports_the_stream(ctx):
    ctx.health = {
        "started_at": 0,
        "turns": 3,
        "errors": 0,
        "events": {"connected": True, "frames": 42, "reconnects": 1},
    }
    reply = run(ctx, "/health")
    assert "connected" in reply
    assert "42 frames" in reply


def test_health_shows_reconnecting(ctx):
    ctx.health = {"events": {"connected": False, "reconnects": 5}}
    assert "reconnecting" in run(ctx, "/health")


def test_health_copes_with_no_data(ctx):
    ctx.health = {}
    assert "not started" in run(ctx, "/health")


# --------------------------------------------------------------------- /status


def test_status_includes_the_policy(ctx):
    reply = run(ctx, "/status")
    assert "Auto" in reply
    assert "Stream" in reply


def test_default_context_has_a_policy(ctx):
    assert isinstance(ctx.policy, Policy)
