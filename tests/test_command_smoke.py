"""Smoke coverage for commands that had no test at all.

These are the ones nobody had ever run: `diff`, `doctor`, `mcp`, `msgs`,
`projects`, `revert`, `run`, `skills`, `stats`, `web`, `worktrees`. A command
nobody has executed is a command that works only in theory.
"""

from __future__ import annotations

import pytest

from bridgekit.api import ApiError
from bridgekit.commands import CommandError, run


@pytest.fixture
def ready(ctx):
    ctx.state.set_session("42", "ses_aaaaaaaaaaaaaaaa")
    return ctx


# ----------------------------------------------------------------- inspect


def test_msgs_renders_recent_turns(ready, ctx):
    ctx.api.messages = lambda sid, limit=20, order="desc": [
        {
            "type": "assistant",
            "time": {"created": 1_700_000_000_000},
            "content": [{"type": "text", "text": "the answer is 42"}],
        }
    ]
    reply = run(ready, "/msgs")
    assert "the answer is 42" in reply
    assert "ASSISTANT" in reply


def test_msgs_skips_reasoning_only_messages(ready, ctx):
    ctx.api.messages = lambda sid, limit=20, order="desc": [
        {
            "type": "assistant",
            "time": {"created": 1_700_000_000_000},
            "content": [{"type": "reasoning", "text": "thinking"}],
        }
    ]
    assert "no text parts" in run(ready, "/msgs")


def test_msgs_handles_an_empty_session(ready, ctx):
    ctx.api.messages = lambda sid, limit=20, order="desc": []
    assert "No messages yet" in run(ready, "/msgs")


def test_msgs_caps_the_count(ready, ctx):
    seen = {}

    def fake(sid, limit=20, order="desc"):
        seen["limit"] = limit
        return []

    ctx.api.messages = fake
    run(ready, "/msgs 900")
    assert seen["limit"] <= 25


def test_stats_summarises_the_aggregate(ready):
    """The endpoint returns one object; a per-session list assumption crashed it."""
    reply = run(ready, "/stats")
    assert "Sessions" in reply and "Prompts" in reply
    assert "1,000" in reply
    assert "$0.2500" in reply
    assert "failed" in reply


def test_stats_handles_no_data(ready, ctx):
    ctx.api.stats = lambda: {}
    assert "No usage recorded" in run(ready, "/stats")


def test_stats_survives_a_partial_payload(ready, ctx):
    ctx.api.stats = lambda: {"sessions": 1}
    assert "Sessions" in run(ready, "/stats")


def test_projects_lists_them(ready, ctx):
    assert "/home/demo" in run(ready, "/projects")


def test_diff_shows_output(ready, ctx):
    ctx.api.vcs_diff = lambda d: "--- a\n+++ b\n-old\n+new"
    reply = run(ready, "/diff")
    assert "```diff" in reply
    assert "+new" in reply


def test_diff_handles_a_clean_tree(ready, ctx):
    ctx.api.vcs_diff = lambda d: ""
    assert "No diff" in run(ready, "/diff")


def test_worktrees_lists_them(ready, ctx):
    ctx.api.worktrees = lambda d: [{"name": "feature", "directory": "/tmp/wt"}]
    assert "feature" in run(ready, "/worktrees")


def test_web_searches(ready, ctx):
    ctx.api.websearch = lambda q, directory=None: {"results": [q]}
    assert "python" in run(ready, "/web python")


def test_web_requires_a_query(ready):
    with pytest.raises(CommandError, match="Usage"):
        run(ready, "/web")


# ------------------------------------------------------------------ model


def test_mcp_lists_servers(ready, ctx):
    ctx.api.mcp_servers = lambda d: [{"id": "github", "status": "connected"}]
    assert "github: connected" in run(ready, "/mcp")


def test_mcp_handles_none_configured(ready, ctx):
    ctx.api.mcp_servers = lambda d: []
    assert "No MCP servers" in run(ready, "/mcp")


def test_skills_lists_them(ready, ctx):
    ctx.api.skills = lambda d: [{"id": "report", "description": "Write a report"}]
    assert "report" in run(ready, "/skills")


def test_skills_accepts_plain_strings(ready, ctx):
    ctx.api.skills = lambda d: ["opencode"]
    assert "opencode" in run(ready, "/skills")


def test_run_executes_a_slash_command(ready, ctx):
    run(ready, "/run review")
    assert any(call[0] == "run_command" for call in ctx.api.calls)


def test_run_lists_available_commands(ready, ctx):
    assert "review" in run(ready, "/run")


# ----------------------------------------------------------------- revert


def test_revert_stages_by_default(ready):
    calls = []
    ready.api.revert_stage = lambda s: calls.append("stage")
    assert "Staged" in run(ready, "/revert")
    assert calls == ["stage"]


def test_revert_go_commits(ready):
    calls = []
    ready.api.revert_commit = lambda s: calls.append("commit")
    assert "committed" in run(ready, "/revert go")
    assert calls == ["commit"]


def test_revert_drop_clears(ready):
    calls = []
    ready.api.revert_clear = lambda s: calls.append("clear")
    assert "cleared" in run(ready, "/revert drop")
    assert calls == ["clear"]


def test_revert_needs_a_session(ctx):
    with pytest.raises(CommandError, match="No active session"):
        run(ctx, "/revert")


# ---------------------------------------------------------------- doctor


def test_doctor_runs_every_check(ready, ctx):
    ctx.health = {
        "events": {"connected": True, "frames": 10, "reconnects": 0},
        "telegram": {"edit_failures": 0, "rate_limited": 0, "edit_interval": 2.0},
    }
    ctx.api.info = lambda: {"version": "2.0.31", "pid": 5}
    reply = run(ready, "/doctor")
    for label in ("Token set", "Allow-list", "OpenCode server", "State file", "Event stream"):
        assert label in reply
    assert "WARN  Scope" in reply  # unrestricted by default in tests


def test_doctor_reports_a_dead_server(ready, ctx):
    def boom():
        raise ApiError(0, "Could not reach OpenCode: refused")

    ctx.api.info = boom
    reply = run(ready, "/doctor")
    assert "FAIL" in reply or "WARN  OpenCode server" in reply
    assert "refused" in reply


def test_doctor_flags_failed_edits(ready, ctx):
    ctx.health = {
        "events": {"connected": True, "frames": 1, "reconnects": 0},
        "telegram": {"edit_failures": 7, "rate_limited": 3, "edit_interval": 2.0},
    }
    reply = run(ready, "/doctor")
    assert "7 failed edit" in reply


def test_agent_switches(ready, ctx):
    run(ready, "/agent plan")
    assert any(call[0] == "switch_agent" for call in ctx.api.calls)


def test_agent_lists_when_bare(ready):
    assert "build" in run(ready, "/agent")
