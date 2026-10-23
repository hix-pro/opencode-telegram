"""Session, model and agent commands."""

from __future__ import annotations

from .. import commands as core
from .. import keyboards
from ..api import OpenCode
from ..config import ConfigError
from ..formatting import ago, basename, bullet_list, cost_line, session_line, short_id


@core.command(
    "new",
    "Start a fresh session, optionally in another directory",
    usage="/new [directory] [title]",
    group="Session",
)
def cmd_new(ctx: core.Context) -> str:
    args, rest = _split_target(ctx.args, ctx.rest)
    directory = ctx.target_dir(args or None)
    session = ctx.api.create_session(directory, title=rest or None)
    ctx.state.set_session(ctx.chat_id, session["id"], directory=directory)
    suffix = f"\nTitle: {rest}" if rest else ""
    return f"Started {session['id']}\nDir: {directory}{suffix}"


def _split_target(args: str, rest: str) -> tuple[str, str]:
    """First token is an optional directory, remainder is the title."""
    parts = args.split(maxsplit=1)
    directory = parts[0] if parts else ""
    title = parts[1] if len(parts) > 1 else ""
    if rest:
        title = f"{title}\n{rest}".strip()
    return directory, title


@core.command(
    "sessions",
    "List recent sessions here: /sessions [n] [search]",
    usage="/sessions [limit] [search]",
    group="Session",
)
def cmd_sessions(ctx: core.Context) -> str:
    parts = ctx.args.split(maxsplit=1)
    limit = 10
    search = None
    if parts:
        if parts[0].isdigit():
            limit = min(int(parts[0]), 50)
            search = parts[1] if len(parts) > 1 else None
        else:
            search = parts[0]
    sessions = ctx.api.list_sessions(directory=ctx.directory, limit=limit, search=search)
    if not sessions:
        return "No sessions here yet. Start one with /new."
    lines = [
        session_line(s, active=s.get("id") == ctx.session, fallback_dir=ctx.directory)
        for s in sessions
    ]
    header = f"{len(sessions)} session(s) in {basename(ctx.directory)}"
    footer = "\nSwitch with /use <id>, #1, or a unique prefix."
    # The tap-friendly route: one button per session resolves through the
    # same use-callback the live message's Focus button uses.
    ctx.markup = keyboards.session_list_markup(sessions, active_id=ctx.session)
    return header + "\n" + "\n".join(lines) + footer


@core.command(
    "use",
    "Switch to another session: /use <id|#n|prefix>",
    usage="/use <id>",
    group="Session",
)
def cmd_use(ctx: core.Context) -> str:
    if not ctx.args:
        raise core.CommandError("Usage: /use <id>, /use #3, or /use <id-prefix>")
    session = core.resolve_session(ctx, ctx.args)
    directory = session.get("directory") or ctx.directory
    try:
        ctx.config.resolve_dir(directory)
    except ConfigError as exc:
        raise core.CommandError(str(exc)) from exc
    ctx.state.set_session(ctx.chat_id, session["id"], directory=directory)
    title = session.get("title") or "untitled"
    return f"Now in {session['id']}\n{title}\nDir: {directory}"


@core.command(
    "macro",
    "Standing prompts: /macro name prompt - then run as /name",
    usage="/macro [name [prompt]]",
    group="Help",
)
def cmd_macro(ctx: core.Context) -> str:
    if ctx.rest:
        # The body was silently cut at the first newline: the recorded
        # macro looked complete and ran half a prompt every time.
        raise core.CommandError(
            "A macro body is one line - run it with extra arguments instead."
        )
    trimmed = (ctx.args or "").strip()
    if not trimmed:
        macros = ctx.macros
        if not macros:
            return (
                "No macros yet. Record one with:\n"
                "/macro deploy deploy the app to staging\n"
                "…then just type /deploy to run it."
            )
        lines = [f"/{name} - {template[:80]}" for name, template in sorted(macros.items())]
        return "Macros - run by typing /<name>:\n" + "\n".join(lines)

    name, _, template = trimmed.partition(" ")
    name = name.lower().lstrip("/")
    template = template.strip()
    if not core.MACRO_NAME.fullmatch(name):
        raise core.CommandError(
            "Macro names use letters, digits and underscores, starting with a letter."
        )
    if name in core.REGISTRY or name in core.DISPATCH_ALIASES:
        raise core.CommandError(f"/{name} is built in; a macro cannot shadow it.")
    if not template:
        if name in ctx.macros:
            del ctx.macros[name]
            ctx.state.save_macros(ctx.macros)
            return f"Macro /{name} deleted."
        raise core.CommandError(f"No macro /{name}. List what exists with /macro.")
    if template.startswith("/"):
        raise core.CommandError(
            "A macro body is a prompt for the agent; slash commands do not fit."
        )
    ctx.macros[name] = template
    ctx.state.save_macros(ctx.macros)
    return f"Macro /{name} recorded - run it by typing /{name}."


@core.command("info", "Details for the active session", group="Session")
def cmd_info(ctx: core.Context) -> str:
    session = ctx.api.get_session(ctx.require_session())
    time_info = session.get("time") or {}
    rows = [
        ("ID", session.get("id")),
        ("Title", session.get("title") or "untitled"),
        ("Directory", session.get("directory") or ctx.directory),
        ("Agent", (session.get("agent") or "-")),
        (
            "Parent",
            short_id(session.get("parentID")) if session.get("parentID") else "-",
        ),
        ("Created", ago(time_info.get("created"))),
        ("Updated", ago(time_info.get("updated"))),
        ("Tokens", cost_line(session.get("tokens") or session.get("cost"))),
        (
            "Cost",
            f"${session.get('cost', 0):.4f}"
            if isinstance(session.get("cost"), (int, float))
            else "-",
        ),
    ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"{label.ljust(width)}  {value}" for label, value in rows)


@core.command("title", "Rename the active session", usage="/title <new title>")
def cmd_title(ctx: core.Context) -> str:
    if not ctx.args:
        raise core.CommandError("Usage: /title <new title>")
    title = ctx.args.strip()
    if len(title) > core.MAX_TITLE:
        raise core.CommandError(
            f"Title is {len(title)} characters; the limit is {core.MAX_TITLE}."
        )
    ctx.api.update_session(ctx.require_session(), title=title)
    return f"Renamed to: {title}"


@core.command(
    "del", "Delete a session and its children", usage="/del <id|#n>", group="Session"
)
def cmd_del(ctx: core.Context) -> str:
    if not ctx.args:
        raise core.CommandError("Usage: /del <id> or /del #3")
    session = core.resolve_session(ctx, ctx.args)
    ctx.api.delete_session(session["id"])
    if session["id"] == ctx.session:
        ctx.state.set_session(ctx.chat_id, None)
        return f"Deleted {session['id']}. It was the active session; use /new to continue."
    return f"Deleted {session['id']} ({session.get('title') or 'untitled'})"


@core.command("fork", "Branch the active session", group="Session")
def cmd_fork(ctx: core.Context) -> str:
    forked = ctx.api.fork_session(ctx.require_session())
    ctx.state.set_session(ctx.chat_id, forked["id"])
    return f"Forked into {forked['id']}"


@core.command("compact", "Summarise old turns to free context", group="Session")
def cmd_compact(ctx: core.Context) -> str:
    ctx.api.compact_session(ctx.require_session())
    return "Compaction requested. It finishes at the end of the current turn."


@core.command("abort", "Stop the running turn", group="Session")
def cmd_abort(ctx: core.Context) -> str:
    session = ctx.require_session()
    ctx.api.interrupt_session(session)
    return "Interrupt sent."


@core.command("revert", "Roll back a turn: /revert [go|drop]", usage="/revert [go|drop]")
def cmd_revert(ctx: core.Context) -> str:
    session = ctx.require_session()
    action = (ctx.args or "").lower()
    if action == "go":
        ctx.api.revert_commit(session)
        return "Revert committed."
    if action == "drop":
        ctx.api.revert_clear(session)
        return "Staged revert cleared."
    ctx.api.revert_stage(session)
    return "Staged a revert of the last turn. /revert go to keep it, /revert drop to cancel."


@core.command(
    "model",
    "Show or switch model: /model [provider/model#variant]",
    usage="/model [provider/model]",
    group="Model",
)
def cmd_model(ctx: core.Context) -> str:
    if not ctx.args:
        session = ctx.api.get_session(ctx.require_session())
        return f"Model: {session.get('model') or 'inherited from config'}"
    try:
        OpenCode._model_ref(ctx.args)
    except ValueError as exc:
        # Fail with the message, not a generic "Internal error".
        raise core.CommandError(str(exc)) from exc
    ctx.api.switch_model(ctx.require_session(), ctx.args)
    return f"Model set to {ctx.args}"


@core.command(
    "agent", "Show or switch agent: /agent [name]", usage="/agent [name]", group="Model"
)
def cmd_agent(ctx: core.Context) -> str:
    if not ctx.args:
        agents = ctx.api.agents(ctx.directory)
        names = [a.get("id", "?") for a in agents if not a.get("hidden")]
        return "Agents: " + ", ".join(names)
    ctx.api.switch_agent(ctx.require_session(), ctx.args)
    return f"Agent set to {ctx.args}"


@core.command("models", "List available models", group="Model")
def cmd_models(ctx: core.Context) -> str:
    models = ctx.api.models(ctx.directory)
    rows = []
    for model in models:
        provider = model.get("providerID") or model.get("provider") or "?"
        model_id = model.get("id") or model.get("modelID") or "?"
        rows.append(f"{provider}/{model_id}")
    return f"{len(rows)} models. Switch with /model provider/model\n" + bullet_list(
        sorted(set(rows))[:60]
    )


@core.command("agents", "Describe the configured agents", group="Model")
def cmd_agents(ctx: core.Context) -> str:
    rows = []
    for agent in ctx.api.agents(ctx.directory):
        if agent.get("hidden"):
            continue
        rows.append(f"{agent.get('id')}: {(agent.get('description') or '').strip()[:70]}")
    return bullet_list(rows, "No agents configured.")


@core.command("skills", "List skills available here", group="Model")
def cmd_skills(ctx: core.Context) -> str:
    skills = ctx.api.skills(ctx.directory)
    rows = []
    for skill in skills:
        if isinstance(skill, str):
            rows.append(skill)
        else:
            rows.append(
                f"{skill.get('id') or skill.get('name')}: "
                f"{(skill.get('description') or '').strip()[:60]}"
            )
    return bullet_list(rows, "No skills here.")


@core.command("mcp", "Show MCP server status", group="Model")
def cmd_mcp(ctx: core.Context) -> str:
    rows = []
    for server in ctx.api.mcp_servers(ctx.directory):
        name = server.get("id") or server.get("name") or "?"
        rows.append(f"{name}: {server.get('status', 'unknown')}")
    return bullet_list(rows, "No MCP servers configured.")
