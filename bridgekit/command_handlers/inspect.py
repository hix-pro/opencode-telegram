"""History, git, web and search commands."""

from __future__ import annotations

from .. import commands as core
from ..api import ApiError
from ..formatting import (
    ago,
    bullet_list,
    code_block,
    head_tail,
    session_line,
    short_id,
    truncate,
)


@core.command("msgs", "Recent messages: /msgs [n]", usage="/msgs [n]", group="Inspect")
def cmd_msgs(ctx: core.Context) -> str:
    limit = core._positive_int(ctx.args, "count") if ctx.args else 6
    messages = ctx.api.messages(ctx.require_session(), limit=min(limit, 25), order="desc")
    if not messages:
        return "No messages yet."
    blocks = []
    for message in reversed(messages):
        kind = message.get("type", "?")
        text = "".join(
            block.get("text", "")
            for block in message.get("content", [])
            if block.get("type") == "text"
        ).strip()
        if not text:
            continue
        label = kind.upper()
        stamp = ago((message.get("time") or {}).get("created"))
        blocks.append(f"[{stamp}] {label}\n{truncate(text, 800)}")
    return truncate("\n\n".join(blocks), ctx.config.max_output) or "(no text parts)"


@core.command("stats", "Aggregate usage across all sessions", group="Inspect")
def cmd_stats(ctx: core.Context) -> str:
    """The server returns one aggregate object, not a per-session list."""
    data = ctx.api.stats()
    if not data:
        return "No usage recorded."

    tokens = data.get("tokens") or {}
    cache = tokens.get("cache") or {}
    totals = ((data.get("tools") or {}).get("totals")) or {}

    def num(value) -> str:
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return "-"

    rows = [
        ("Sessions", num(data.get("sessions"))),
        ("Prompts", num(data.get("prompts"))),
        ("Steps", num(data.get("steps"))),
        ("Tokens in", num(tokens.get("input"))),
        ("Tokens out", num(tokens.get("output"))),
        ("Reasoning", num(tokens.get("reasoning"))),
        ("Cache read", num(cache.get("read"))),
        ("Cost", f"${float(data.get('cost') or 0):.4f}"),
    ]
    if totals:
        failed = totals.get("failed") or 0
        rows.append(("Tool calls", f"{num(totals.get('calls'))} ({num(failed)} failed)"))
    if data.get("activeDays"):
        rows.append(("Active days", f"{data['activeDays']} (streak {data.get('streak', 0)})"))

    width = max(len(label) for label, _ in rows)
    body = "\n".join(f"{label.ljust(width)}  {value}" for label, value in rows)

    activity = data.get("activity") or []
    if activity:
        recent = sorted(activity, key=lambda a: a.get("date", ""))[-8:]
        days = "  ".join(f"{a.get('date', '?')[5:]}:{num(a.get('steps'))}" for a in recent)
        body += f"\n\nRecent steps  {days}"

    return body


@core.command("export", "Dump the active session as JSON", group="Inspect")
def cmd_export(ctx: core.Context) -> str:
    import json as _json

    payload = ctx.api.export_session(ctx.require_session())
    text = _json.dumps(payload, indent=2, default=str)
    return code_block(
        head_tail(text, ctx.config.max_output), "json", limit=ctx.config.max_output
    )


@core.command("git", "Working tree status here", group="Inspect")
def cmd_git(ctx: core.Context) -> str:
    entries = ctx.api.vcs_status(ctx.directory)
    if not entries:
        return "Not a git repository, or the tree is clean."
    lines = []
    for entry in entries:
        lines.append(f"{entry.get('state', '?'):8} {entry.get('path', '?')}")
    return code_block("\n".join(lines), limit=ctx.config.max_output)


@core.command("diff", "Diff for this directory", group="Inspect")
def cmd_diff(ctx: core.Context) -> str:
    text = ctx.api.vcs_diff(ctx.directory)
    if not text.strip():
        return "No diff."
    return code_block(
        head_tail(text, ctx.config.max_output), "diff", limit=ctx.config.max_output
    )


@core.command(
    "worktrees",
    "List, create, or remove git worktrees",
    usage="/worktrees [new <name>|rm <name>]",
    group="Files",
)
def cmd_worktrees(ctx: core.Context) -> str:
    """Worktrees let a long job run without disturbing the working tree.

    Each one is a separate checkout, so a session pointed at it edits different
    files from the session in the main directory.
    """
    existing = ctx.api.worktrees(ctx.directory)

    if not ctx.args:
        rows = [f"{w.get('name', '?')}  {w.get('directory', '?')}" for w in existing]
        return bullet_list(rows, "No worktrees here.") + (
            "\n\nCreate one with /worktrees new <name>, remove with /worktrees rm <name>."
        )

    action, _, name = ctx.args.partition(" ")
    name = name.strip()

    if action == "new":
        if not name:
            raise core.CommandError("Usage: /worktrees new <name>")
        if any(w.get("name") == name for w in existing):
            raise core.CommandError(f"A worktree named {name!r} already exists.")
        created = ctx.api.create_worktree(ctx.directory, name)
        where = (created or {}).get("directory") or f"{ctx.directory}/.worktrees/{name}"
        return f"Created worktree {name} at {where}"

    if action in ("rm", "remove"):
        if not name:
            raise core.CommandError("Usage: /worktrees rm <name>")
        if not any(w.get("name") == name for w in existing):
            raise core.CommandError(f"No worktree named {name!r}.")
        ctx.api.remove_worktree(ctx.directory, name)
        return f"Removed worktree {name}"

    raise core.CommandError("Usage: /worktrees [new <name>|rm <name>]")


@core.command("web", "Search the web", usage="/web <query>", group="Inspect")
def cmd_web(ctx: core.Context) -> str:
    if not ctx.args:
        raise core.CommandError("Usage: /web <query>")
    result = ctx.api.websearch(ctx.args, directory=ctx.directory)
    return code_block(
        truncate(str(result), ctx.config.max_output), limit=ctx.config.max_output
    )


@core.command(
    "run",
    "Run a slash command defined in config",
    usage="/run <name> [args]",
    group="Inspect",
)
def cmd_run(ctx: core.Context) -> str:
    if not ctx.args:
        available = ctx.api.commands(ctx.directory)
        rows = [c.get("name", "?") for c in available]
        return "Slash commands: " + (", ".join(rows) or "(none)")
    parts = ctx.args.split(maxsplit=1)
    ctx.api.run_command(ctx.require_session(), parts[0], parts[1] if len(parts) > 1 else "")
    return f"Ran /{parts[0]}. Watch the reply for the result."


@core.command(
    "search",
    "Find past sessions by title: /search <text>",
    usage="/search <text>",
    group="Inspect",
)
def cmd_search(ctx: core.Context) -> str:
    """Search titles first, then the text inside recent conversations.

    The server's own search only looks at titles, so a conversation whose
    title does not contain the query was previously invisible.
    """
    if not ctx.args:
        raise core.CommandError("Usage: /search <text>")

    sessions = ctx.api.list_sessions(search=ctx.args, limit=25)
    lines = [
        session_line(s, active=s.get("id") == ctx.session, fallback_dir=ctx.directory)
        for s in sessions
    ]
    header = f"{len(lines)} title match(es):\n" + "\n".join(lines) if lines else ""

    try:
        inside = ctx.api.search_session_text(ctx.args, directory=ctx.directory)
    except ApiError:
        inside = []
    content_lines = [f"{short_id(sid)}  ...{snippet}..." for sid, snippet in inside]

    if not header and not content_lines:
        return f"Nothing matching {ctx.args!r} in recent conversations."

    parts = []
    if header:
        parts.append(header)
    if content_lines:
        parts.append(f"found in the text of {len(content_lines)}:")
        parts.append(bullet_list(content_lines))
    parts.append("Switch with /use <id>.")
    return "\n\n".join(parts)
