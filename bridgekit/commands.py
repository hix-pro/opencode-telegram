"""Command registry and handlers.

Everything the TUI can do over its HTTP API is reachable from here. Handlers
take a :class:`Context` and return the text to send back, so they stay pure
with respect to Telegram: the caller does the sending, chunking, and error
formatting.

Adding a command is one ``@command(...)`` decorated function.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import localfs
from .api import (
    PERMISSION_ALWAYS,
    PERMISSION_ONCE,
    PERMISSION_REJECT,
    ApiError,
    OpenCode,
)
from .config import Config, ConfigError
from .formatting import (
    ago,
    basename,
    bullet_list,
    code_block,
    cost_line,
    head_tail,
    permission_line,
    session_line,
    short_id,
    truncate,
)
from .localfs import PathError
from .policy import Policy
from .schedule import ScheduleStore
from .schedule import parse as parse_spec
from .state import State
from .telegram import Telegram


class CommandError(RuntimeError):
    """A user-facing problem: bad arguments, missing session, denied path."""


@dataclass
class Context:
    api: OpenCode
    state: State
    config: Config
    telegram: Telegram
    chat_id: str
    args: str = ""
    rest: str = ""
    policy: Policy | None = None
    health: dict = field(default_factory=dict)
    # Lets the bridge drop a cached permission request when a command answers
    # it, so a stale button cannot still resolve.
    on_permission_answered: Callable[[str], None] = lambda request_id: None
    schedules: ScheduleStore | None = None

    # -------------------------------------------------------------- shortcuts

    def __post_init__(self) -> None:
        if self.policy is None:
            self.policy = Policy()

    @property
    def directory(self) -> str:
        return self.state.directory(self.chat_id)

    @property
    def session(self) -> str | None:
        return self.state.session(self.chat_id)

    def require_session(self) -> str:
        session = self.session
        if not session:
            raise CommandError(
                "No active session. Start one with /new, or pick one with /sessions."
            )
        return session

    def check_path(self, path: str) -> str:
        """Validate a file path against the working directory and allow-list.

        Returns the resolved absolute path for messages, but callers pass the
        original path to the API: the fs endpoints resolve relative to the
        session directory, so an absolute path would be re-interpreted.
        """
        try:
            return self.config.check_path(self.directory, path)
        except ConfigError as exc:
            raise CommandError(str(exc)) from exc

    def persist_schedules(self) -> None:
        """Schedules live in the state file so they survive a restart."""
        if self.schedules is not None:
            self.state.save_schedules(self.schedules.to_list())

    def persist_policy(self) -> None:
        """Write the policy so it survives a restart.

        Rules used to live only in memory, so /policy and /guard silently
        reset on the next boot and /auto came back as whatever the config said.
        """
        # Snapshot every chat: the saved shape is keyed by chat id, and a
        # per-chat snapshot would restore as if the action were a chat name.
        self.state.save_policy(self.policy.snapshot())

    def target_dir(self, raw: str | None) -> str:
        """Resolve a directory argument against the allow-list."""
        try:
            return self.config.resolve_dir(raw or self.directory)
        except ConfigError as exc:
            raise CommandError(str(exc)) from exc


# --------------------------------------------------------------------- registry


@dataclass
class Command:
    name: str
    summary: str
    usage: str = ""
    handler: Callable[[Context], str] = lambda ctx: ""
    needs_session: bool = False
    group: str = "Session"

    def help_line(self) -> str:
        return f"/{self.name} - {self.summary}"


REGISTRY: dict[str, Command] = {}

# Longest a /sh command may run before the bridge stops waiting.
SHELL_TIMEOUT = 60

# How long to wait before concluding the server killed the command.
SHELL_PROBE = 3.0

# Bounds for the shell-backed /find.
FIND_DEPTH = 6
FIND_LIMIT = 40
FIND_TIMEOUT = 20

# Session titles show up in every listing, so keep them readable.
MAX_TITLE = 200

log = logging.getLogger("bridge.commands")


def command(
    name: str,
    summary: str,
    *,
    usage: str = "",
    needs_session: bool = False,
    group: str = "Session",
) -> Callable[[Callable[[Context], str]], Callable[[Context], str]]:
    def decorate(func: Callable[[Context], str]) -> Callable[[Context], str]:
        REGISTRY[name] = Command(
            name, summary, usage or f"/{name}", func, needs_session, group
        )
        return func

    return decorate


# ------------------------------------------------------------------ resolution


def parse(text: str) -> tuple[str, str, str] | None:
    """Split a message into (command, args, rest-of-lines).

    Returns None when the message is not a command, which means the caller
    should treat it as a prompt for the agent.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    lines = stripped.split("\n", 1)
    head = lines[0].split()[0] if lines[0].split() else ""
    # Telegram appends @botname in groups; ignore it.
    name = head[1:].split("@", 1)[0].lower()
    if not name:
        return None

    args = lines[0][len(head) :].strip()
    rest = lines[1] if len(lines) > 1 else ""
    return name, args, rest


def resolve_session(ctx: Context, ref: str) -> dict:
    """Resolve a session reference: exact id, unique prefix, or #index."""
    sessions = ctx.api.list_sessions(directory=ctx.directory, limit=25)

    for session in sessions:
        if session.get("id") == ref:
            return session

    if ref.startswith("#"):
        index = _positive_int(ref[1:], "index")
        if 1 <= index <= len(sessions):
            return sessions[index - 1]
        raise CommandError(
            f"No session #{index}. /sessions lists {len(sessions)} in this directory."
        )

    matches = [s for s in sessions if (s.get("id") or "").startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        listed = "\n".join(session_line(s) for s in matches[:6])
        raise CommandError(f"Ambiguous prefix {ref!r}:\n{listed}")
    raise CommandError(f"No session matching {ref!r} in {basename(ctx.directory)}.")


def _positive_int(raw: str, label: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise CommandError(f"{label.capitalize()} must be a number, got {raw!r}") from exc
    if value < 1:
        raise CommandError(f"{label.capitalize()} must be 1 or more, got {value}")
    return value


def _pending(ctx: Context) -> list[dict]:
    try:
        return ctx.api.permission_requests(directory=ctx.directory)
    except ApiError:
        return []


# --------------------------------------------------------------------- session


@command(
    "new",
    "Start a fresh session, optionally in another directory",
    usage="/new [directory] [title]",
    group="Session",
)
def cmd_new(ctx: Context) -> str:
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


@command(
    "sessions",
    "List recent sessions here: /sessions [n] [search]",
    usage="/sessions [limit] [search]",
    group="Session",
)
def cmd_sessions(ctx: Context) -> str:
    parts = ctx.args.split(maxsplit=1)
    limit = 10
    search = None
    if parts:
        if parts[0].lstrip("-").isdigit() and not parts[0].lstrip("-"):
            raise CommandError("Usage: /sessions [limit] [search]")
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
    return header + "\n" + "\n".join(lines) + footer


@command(
    "use",
    "Switch to another session: /use <id|#n|prefix>",
    usage="/use <id>",
    group="Session",
)
def cmd_use(ctx: Context) -> str:
    if not ctx.args:
        raise CommandError("Usage: /use <id>, /use #3, or /use <id-prefix>")
    session = resolve_session(ctx, ctx.args)
    directory = session.get("directory") or ctx.directory
    try:
        ctx.config.resolve_dir(directory)
    except ConfigError as exc:
        raise CommandError(str(exc)) from exc
    ctx.state.set_session(ctx.chat_id, session["id"], directory=directory)
    title = session.get("title") or "untitled"
    return f"Now in {session['id']}\n{title}\nDir: {directory}"


@command("info", "Details for the active session", group="Session")
def cmd_info(ctx: Context) -> str:
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


@command("title", "Rename the active session", usage="/title <new title>")
def cmd_title(ctx: Context) -> str:
    if not ctx.args:
        raise CommandError("Usage: /title <new title>")
    title = ctx.args.strip()
    if len(title) > MAX_TITLE:
        raise CommandError(f"Title is {len(title)} characters; the limit is {MAX_TITLE}.")
    ctx.api.update_session(ctx.require_session(), title=title)
    return f"Renamed to: {title}"


@command("del", "Delete a session and its children", usage="/del <id|#n>", group="Session")
def cmd_del(ctx: Context) -> str:
    if not ctx.args:
        raise CommandError("Usage: /del <id> or /del #3")
    session = resolve_session(ctx, ctx.args)
    ctx.api.delete_session(session["id"])
    if session["id"] == ctx.session:
        ctx.state.set_session(ctx.chat_id, None)
        return f"Deleted {session['id']}. It was the active session; use /new to continue."
    return f"Deleted {session['id']} ({session.get('title') or 'untitled'})"


@command("fork", "Branch the active session", group="Session")
def cmd_fork(ctx: Context) -> str:
    forked = ctx.api.fork_session(ctx.require_session())
    ctx.state.set_session(ctx.chat_id, forked["id"])
    return f"Forked into {forked['id']}"


@command("compact", "Summarise old turns to free context", group="Session")
def cmd_compact(ctx: Context) -> str:
    ctx.api.compact_session(ctx.require_session())
    return "Compaction requested. It finishes at the end of the current turn."


@command("abort", "Stop the running turn", group="Session")
def cmd_abort(ctx: Context) -> str:
    ctx.require_session()
    ctx.api.interrupt_session(ctx.session)
    return "Interrupt sent."


@command("revert", "Roll back a turn: /revert [go|drop]", usage="/revert [go|drop]")
def cmd_revert(ctx: Context) -> str:
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


# -------------------------------------------------------------- model / agent


@command(
    "model",
    "Show or switch model: /model [provider/model#variant]",
    usage="/model [provider/model]",
    group="Model",
)
def cmd_model(ctx: Context) -> str:
    if not ctx.args:
        session = ctx.api.get_session(ctx.require_session())
        return f"Model: {session.get('model') or 'inherited from config'}"
    try:
        OpenCode._model_ref(ctx.args)
    except ValueError as exc:
        # Fail with the message, not a generic "Internal error".
        raise CommandError(str(exc)) from exc
    ctx.api.switch_model(ctx.require_session(), ctx.args)
    return f"Model set to {ctx.args}"


@command("agent", "Show or switch agent: /agent [name]", usage="/agent [name]", group="Model")
def cmd_agent(ctx: Context) -> str:
    if not ctx.args:
        agents = ctx.api.agents(ctx.directory)
        names = [a.get("id", "?") for a in agents if not a.get("hidden")]
        return "Agents: " + ", ".join(names)
    ctx.api.switch_agent(ctx.require_session(), ctx.args)
    return f"Agent set to {ctx.args}"


@command("models", "List available models", group="Model")
def cmd_models(ctx: Context) -> str:
    models = ctx.api.models(ctx.directory)
    rows = []
    for model in models:
        provider = model.get("providerID") or model.get("provider") or "?"
        model_id = model.get("id") or model.get("modelID") or "?"
        rows.append(f"{provider}/{model_id}")
    return f"{len(rows)} models. Switch with /model provider/model\n" + bullet_list(
        sorted(set(rows))[:60]
    )


@command("agents", "Describe the configured agents", group="Model")
def cmd_agents(ctx: Context) -> str:
    rows = []
    for agent in ctx.api.agents(ctx.directory):
        if agent.get("hidden"):
            continue
        rows.append(f"{agent.get('id')}: {(agent.get('description') or '').strip()[:70]}")
    return bullet_list(rows, "No agents configured.")


@command("skills", "List skills available here", group="Model")
def cmd_skills(ctx: Context) -> str:
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


@command("mcp", "Show MCP server status", group="Model")
def cmd_mcp(ctx: Context) -> str:
    rows = []
    for server in ctx.api.mcp_servers(ctx.directory):
        name = server.get("id") or server.get("name") or "?"
        rows.append(f"{name}: {server.get('status', 'unknown')}")
    return bullet_list(rows, "No MCP servers configured.")


# ------------------------------------------------------------- files and code


@command("dir", "Show or switch the working directory", usage="/dir [path]", group="Files")
def cmd_dir(ctx: Context) -> str:
    if not ctx.args:
        scope = (
            "anywhere on this machine"
            if ctx.config.unrestricted
            else ("within " + ", ".join(str(r) for r in ctx.config.allowed_roots))
        )
        return f"Dir: {ctx.directory}\nSessions may target {scope}."
    directory = ctx.target_dir(ctx.args)
    ctx.state.set_directory(ctx.chat_id, directory)
    return f"Dir set to {directory}"


@command("projects", "List known projects", group="Files")
def cmd_projects(ctx: Context) -> str:
    rows = []
    for project in ctx.api.projects():
        worktree = project.get("worktree") or project.get("directory") or "?"
        rows.append(f"{project.get('id', '?')[:12]}  {worktree}")
    return bullet_list(rows, "No projects.")


@command("ls", "List a directory", usage="/ls [path]", group="Files")
def cmd_ls(ctx: Context) -> str:
    path = ctx.args or "."
    try:
        entries = localfs.list_dir(ctx.directory, path, ctx.config.allowed_roots)
    except PathError as exc:
        raise CommandError(str(exc)) from exc
    if not entries:
        return f"{path} is empty."
    lines = [f"{entry.name}/" if entry.is_dir else entry.name for entry in entries]
    return code_block("\n".join(lines), limit=ctx.config.max_output)


@command("find", "Search for files by name", usage="/find <query>", group="Files")
def cmd_find(ctx: Context) -> str:
    """Search for files by name, scoped to the working directory."""
    if not ctx.args:
        raise CommandError("Usage: /find <query>")
    try:
        matches = localfs.find(ctx.directory, ctx.args, ctx.config.allowed_roots)
    except PathError as exc:
        raise CommandError(str(exc)) from exc
    if not matches:
        return f"No files matching {ctx.args!r} under {ctx.directory}"
    return bullet_list(matches)


@command("read", "Print a file", usage="/read <path>", group="Files")
def cmd_read(ctx: Context) -> str:
    if not ctx.args:
        raise CommandError("Usage: /read <path>")
    try:
        content = localfs.read_file(ctx.directory, ctx.args, ctx.config.allowed_roots)
    except PathError as exc:
        raise CommandError(str(exc)) from exc
    if not content:
        return "(empty file)"
    return code_block(head_tail(content, ctx.config.max_output), limit=ctx.config.max_output)


@command(
    "write",
    "Write a file. Path on line 1, content below",
    usage="/write <path>\n<content>",
    group="Files",
)
def cmd_write(ctx: Context) -> str:
    if not ctx.args or not ctx.rest:
        raise CommandError("Usage:\n/write notes.md\nfirst line\nsecond line")
    try:
        written = localfs.write_file(
            ctx.directory, ctx.args, ctx.rest, ctx.config.allowed_roots
        )
    except PathError as exc:
        raise CommandError(str(exc)) from exc
    size = len(ctx.rest.encode())
    return f"Wrote {size} bytes to {written}"


@command(
    "sh",
    "Run a shell command here",
    usage="/sh <command>",
    group="Files",
)
def cmd_sh(ctx: Context) -> str:
    """Run a command in the chat's directory and return its output.

    Execution happens on a worker thread with its own deadline, so a runaway
    command cannot freeze command handling or /abort for the whole chat.
    """
    if not ctx.args:
        raise CommandError("Usage: /sh <command>")

    outcome: dict[str, Any] = {"output": "", "status": "completed"}

    def run() -> None:
        try:
            outcome["output"] = run_and_capture(
                ctx, ctx.args, ctx.directory, SHELL_TIMEOUT
            )
        except CommandError as exc:
            # Runs on the worker thread, where raising would vanish into the
            # join; hand it back to the caller instead.
            outcome["error"] = str(exc)
        except ApiError as exc:
            outcome["status"] = "error"
            outcome["output"] = exc.message
        except Exception:
            log.exception("Shell command failed")
            outcome["status"] = "error"
            outcome["output"] = "internal error running the command"

    worker = threading.Thread(target=run, name="shell", daemon=True)
    worker.start()
    # Bounded join: the caller returns rather than hanging the poll thread.
    worker.join(timeout=SHELL_TIMEOUT + 5)

    if outcome.get("error"):
        raise CommandError(outcome["error"])

    if worker.is_alive():
        return (
            f"$ {ctx.args}\n[still running after {SHELL_TIMEOUT}s - "
            "it will finish in the background]"
        )

    body = outcome["output"] or "(no output)"
    header = f"$ {ctx.args}"
    return (
        header
        + "\n"
        + code_block(head_tail(body, ctx.config.max_output), limit=ctx.config.max_output)
    )


@command("perms", "List pending permission requests", group="Permissions")
def cmd_perms(ctx: Context) -> str:
    requests = _pending(ctx)
    if not requests:
        return "Nothing waiting for approval."
    lines = [permission_line(r) for r in requests]
    footer = "\nReply with /yes <id|#n|all>, /no <id|#n|all>, or /yes always <id>."
    return f"{len(requests)} pending:\n" + "\n".join(lines) + footer


@command(
    "yes",
    "Approve: /yes [id|#n|all] [always]",
    usage="/yes [id|all] [always]",
    group="Permissions",
)
def cmd_yes(ctx: Context) -> str:
    return _answer(ctx, PERMISSION_ONCE)


@command("no", "Reject a permission request", usage="/no <id|#n|all>", group="Permissions")
def cmd_no(ctx: Context) -> str:
    return _answer(ctx, PERMISSION_REJECT)


def _answer(ctx: Context, base_decision: str) -> str:
    parts = ctx.args.split()
    decision = base_decision
    if "always" in parts:
        parts.remove("always")
        decision = PERMISSION_ALWAYS
    requests = _pending(ctx)
    if not requests:
        return "Nothing waiting for approval."

    if not parts or parts[0] == "all":
        targets = requests
    elif parts[0].startswith("#"):
        targets = [_pick(requests, _positive_int(parts[0][1:], "index"))]
    else:
        targets = [_match_request(requests, parts[0])]

    done = []
    for request in targets:
        session_id = request.get("sessionID") or ""
        ctx.api.reply_permission(session_id, request["id"], decision)
        ctx.on_permission_answered(str(request["id"]))
        done.append(short_id(request.get("id")))
    verb = {
        PERMISSION_ONCE: "approved",
        PERMISSION_ALWAYS: "always allowed",
        PERMISSION_REJECT: "rejected",
    }[decision]
    return f"{verb}: {', '.join(done)}"


def _pick(requests: list[dict], index: int) -> dict:
    if not 1 <= index <= len(requests):
        raise CommandError(f"No request #{index}; {len(requests)} pending.")
    return requests[index - 1]


def _match_request(requests: list[dict], ref: str) -> dict:
    for request in requests:
        if request.get("id") == ref:
            return request
    matches = [r for r in requests if (r.get("id") or "").startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CommandError(f"Ambiguous prefix {ref!r}; use /perms to list them.")
    raise CommandError(f"No pending request matching {ref!r}.")


# ---------------------------------------------------------------- inspection


@command("msgs", "Recent messages: /msgs [n]", usage="/msgs [n]", group="Inspect")
def cmd_msgs(ctx: Context) -> str:
    limit = _positive_int(ctx.args, "count") if ctx.args else 6
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


@command("stats", "Aggregate usage across all sessions", group="Inspect")
def cmd_stats(ctx: Context) -> str:
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


@command("export", "Dump the active session as JSON", group="Inspect")
def cmd_export(ctx: Context) -> str:
    import json as _json

    payload = ctx.api.export_session(ctx.require_session())
    text = _json.dumps(payload, indent=2, default=str)
    return code_block(
        head_tail(text, ctx.config.max_output), "json", limit=ctx.config.max_output
    )


@command("git", "Working tree status here", group="Inspect")
def cmd_git(ctx: Context) -> str:
    entries = ctx.api.vcs_status(ctx.directory)
    if not entries:
        return "Not a git repository, or the tree is clean."
    lines = []
    for entry in entries:
        lines.append(f"{entry.get('state', '?'):8} {entry.get('path', '?')}")
    return code_block("\n".join(lines), limit=ctx.config.max_output)


@command("diff", "Diff for this directory", group="Inspect")
def cmd_diff(ctx: Context) -> str:
    text = ctx.api.vcs_diff(ctx.directory)
    if not text.strip():
        return "No diff."
    return code_block(
        head_tail(text, ctx.config.max_output), "diff", limit=ctx.config.max_output
    )


@command(
    "worktrees",
    "List, create, or remove git worktrees",
    usage="/worktrees [new <name>|rm <name>]",
    group="Files",
)
def cmd_worktrees(ctx: Context) -> str:
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
            raise CommandError("Usage: /worktrees new <name>")
        if any(w.get("name") == name for w in existing):
            raise CommandError(f"A worktree named {name!r} already exists.")
        created = ctx.api.create_worktree(ctx.directory, name)
        where = (created or {}).get("directory") or f"{ctx.directory}/.worktrees/{name}"
        return f"Created worktree {name} at {where}"

    if action in ("rm", "remove"):
        if not name:
            raise CommandError("Usage: /worktrees rm <name>")
        if not any(w.get("name") == name for w in existing):
            raise CommandError(f"No worktree named {name!r}.")
        ctx.api.remove_worktree(ctx.directory, name)
        return f"Removed worktree {name}"

    raise CommandError("Usage: /worktrees [new <name>|rm <name>]")


@command("web", "Search the web", usage="/web <query>", group="Inspect")
def cmd_web(ctx: Context) -> str:
    if not ctx.args:
        raise CommandError("Usage: /web <query>")
    result = ctx.api.websearch(ctx.args, directory=ctx.directory)
    return code_block(
        truncate(str(result), ctx.config.max_output), limit=ctx.config.max_output
    )


@command(
    "run",
    "Run a slash command defined in config",
    usage="/run <name> [args]",
    group="Inspect",
)
def cmd_run(ctx: Context) -> str:
    if not ctx.args:
        available = ctx.api.commands(ctx.directory)
        rows = [c.get("name", "?") for c in available]
        return "Slash commands: " + (", ".join(rows) or "(none)")
    parts = ctx.args.split(maxsplit=1)
    ctx.api.run_command(ctx.require_session(), parts[0], parts[1] if len(parts) > 1 else "")
    return f"Ran /{parts[0]}. Watch the reply for the result."


# ----------------------------------------------------------- policy and guard


@command(
    "auto",
    "Auto-approval mode: /auto [ask|read|all|off]",
    usage="/auto [ask|read|all]",
    group="Policy",
)
def cmd_auto(ctx: Context) -> str:
    """Control how much the bridge answers permissions on your behalf."""
    if not ctx.args:
        mode = ctx.state.pref(ctx.chat_id, "auto", ctx.config.approval_mode)
        return (
            f"Auto-approval: {mode} ({ctx.policy.describe()})\n"
            f"Modes: ask, read, all. Denied patterns still block: {ctx.policy.deny_summary()}"
        )
    requested = ctx.args.strip().lower()
    requested = {"off": "ask", "on": "read", "full": "all"}.get(requested, requested)
    if requested not in ("ask", "read", "all"):
        raise CommandError("Usage: /auto ask | /auto read | /auto all")
    ctx.policy.set_mode(requested)
    ctx.state.set_pref(ctx.chat_id, "auto", requested)
    ctx.persist_policy()
    return f"Auto-approval: {requested} ({ctx.policy.describe()})"


@command(
    "policy",
    "Per-action approvals: /policy allow|ask|always|deny <action>",
    usage="/policy [allow|ask|always|deny <action>|clear <action>|reset]",
    group="Policy",
)
def cmd_policy(ctx: Context) -> str:
    """Rules beat the coarse /auto mode for the action they name."""
    rules = ctx.policy.rules_for(ctx.chat_id)

    if not ctx.args:
        if not rules:
            return (
                f"No per-action rules. Mode is {ctx.policy.mode} "
                f"({ctx.policy.describe()}).\n"
                "Examples:\n"
                "  /policy always edit    trust edits, still ask about commands\n"
                "  /policy allow read     approve reads\n"
                "  /policy deny bash      never approve commands automatically\n"
                "  /policy reset          drop all rules"
            )
        rows = [f"{action}: {decision}" for action, decision in sorted(rules.items())]
        return (
            f"Per-action rules (override mode {ctx.policy.mode}):\n"
            + bullet_list(rows)
            + "\n\nThe guard still rejects denied commands whatever these say."
        )

    verb, _, target = ctx.args.partition(" ")
    verb = verb.lower()
    target = target.strip().lower()

    if verb == "reset":
        ctx.policy.clear_rules(ctx.chat_id)
        ctx.persist_policy()
        return "Per-action rules cleared. The mode applies again."

    if verb in ("clear", "rm", "remove"):
        known = ", ".join(sorted(rules)) or "none"
        if not target:
            raise CommandError(f"Usage: /policy clear <action>. Current rules: {known}")
        if target not in rules:
            raise CommandError(f"No rule for {target!r}. Current rules: {known}")
        ctx.policy.clear_rule(ctx.chat_id, target)
        ctx.persist_policy()
        return f"Rule for {target} removed."

    verbs = {"allow": "once", "ask": "ask", "always": "always", "deny": "reject"}
    if verb not in verbs:
        raise CommandError("Usage: /policy allow|ask|always|deny <action>, or /policy reset")
    if not target:
        raise CommandError(
            f"Usage: /policy {verb} <action>\n"
            "Common actions: read, edit, write, bash, glob, grep, webfetch. "
            "Use * to match everything."
        )

    ctx.policy.set_rule(ctx.chat_id, target, verbs[verb])
    ctx.persist_policy()
    label = {
        "once": "approved once",
        "ask": "ask every time",
        "always": "always allowed",
        "reject": "always rejected",
    }[verbs[verb]]
    return f"{target}: {label}"


@command(
    "guard",
    "Inspect or edit denied commands",
    usage="/guard [add <pattern>|rm <pattern>|test <command>]",
    group="Policy",
)
def cmd_guard(ctx: Context) -> str:
    if not ctx.args:
        return f"{len(ctx.policy.deny)} denied patterns:\n{ctx.policy.deny_summary(20)}"

    action, _, rest = ctx.args.partition(" ")
    rest = rest.strip()
    action = action.lower()

    if action == "add":
        if not rest:
            raise CommandError("Usage: /guard add <regex>")
        ctx.policy.add_deny(rest)
        ctx.persist_policy()
        return f"Denied. Commands matching {rest!r} will be rejected."

    if action in ("rm", "remove"):
        if not rest:
            raise CommandError("Usage: /guard rm <regex>")
        ctx.policy.remove_deny(rest)
        ctx.persist_policy()
        return f"Removed {rest!r}."

    if action == "test":
        if not rest:
            raise CommandError("Usage: /guard test <command>")
        blocked = ctx.policy.blocked_command(rest)
        if blocked:
            return f"BLOCKED by {blocked!r}"
        return "Allowed by the guard. Note this does not run anything."

    raise CommandError("Usage: /guard [add <pattern>|rm <pattern>|test <command>]")


@command(
    "watch", "Toggle live progress for the next turn", usage="/watch [on|off]", group="Policy"
)
def cmd_watch(ctx: Context) -> str:
    current = bool(ctx.state.pref(ctx.chat_id, "watch", True))
    if ctx.args:
        current = ctx.args.strip().lower() in ("on", "true", "1", "yes")
    else:
        current = not current
    ctx.state.set_pref(ctx.chat_id, "watch", current)
    return f"Live progress: {'on' if current else 'off'}"


@command(
    "delivery",
    "What happens if you message during a long turn: /delivery [queue|steer]",
    usage="/delivery [queue|steer]",
    group="Policy",
)
def cmd_delivery(ctx: Context) -> str:
    """Queue waits the agent's turn; steer cancels the running one and redirects it."""
    if not ctx.args:
        current = ctx.state.pref(ctx.chat_id, "delivery", ctx.config.prompt_delivery)
        return (
            f"Delivery: {current}\n"
            "queue - your message waits until the current turn finishes\n"
            "steer - your message cancels the running turn and redirects it\n"
            "Use /abort to stop a turn on purpose."
        )
    requested = ctx.args.strip().lower()
    if requested not in ("queue", "steer"):
        raise CommandError("Usage: /delivery queue or /delivery steer")
    ctx.state.set_pref(ctx.chat_id, "delivery", requested)
    return f"Delivery set to {requested}."


@command("cost", "Spend and token usage", group="Policy")
def cmd_cost(ctx: Context) -> str:
    session = ctx.api.get_session(ctx.require_session())
    tokens = session.get("tokens") or {}
    total = (tokens.get("input", 0) or 0) + (tokens.get("output", 0) or 0)
    cache = tokens.get("cache") or {}
    cost = session.get("cost", 0) or 0
    inbound = tokens.get("input", 0)
    outbound = tokens.get("output", 0)
    lines = [
        f"Session cost  ${cost:.4f}",
        f"Tokens        {total:,} ({inbound:,} in / {outbound:,} out)",
    ]
    if cache.get("read"):
        lines.append(f"Cache read    {cache['read']:,}")
    alert = ctx.state.pref(ctx.chat_id, "cost_alert", ctx.config.cost_alert)
    if alert:
        lines.append(
            f"Alert at      ${alert:.2f} ({'reached' if cost >= alert else 'not yet'})"
        )
    return "\n".join(lines)


@command(
    "alert",
    "Warn when a session passes a spend level: /alert [usd]",
    usage="/alert [0.25|off]",
    group="Policy",
)
def cmd_alert(ctx: Context) -> str:
    raw = ctx.args.strip().lower()
    if not raw:
        current = ctx.state.pref(ctx.chat_id, "cost_alert", ctx.config.cost_alert)
        return f"Cost alert: {'off' if not current else f'${current:.2f}'}"
    if raw in ("off", "none", "0"):
        ctx.state.set_pref(ctx.chat_id, "cost_alert", 0.0)
        return "Cost alert off."
    try:
        amount = float(raw.lstrip("$"))
    except ValueError as exc:
        raise CommandError("Usage: /alert 0.25 or /alert off") from exc
    ctx.state.set_pref(ctx.chat_id, "cost_alert", amount)
    return f"Cost alert set to ${amount:.2f}."


# ----------------------------------------------------------------------- search


@command(
    "search",
    "Find past sessions by title: /search <text>",
    usage="/search <text>",
    group="Inspect",
)
def cmd_search(ctx: Context) -> str:
    """Search titles first, then the text inside recent conversations.

    The server's own search only looks at titles, so a conversation whose
    title does not contain the query was previously invisible.
    """
    if not ctx.args:
        raise CommandError("Usage: /search <text>")

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



# ----------------------------------------------------------------------- help


@command(
    "schedule",
    "Run something on a timer: /schedule <every 30m|hourly|daily 09:00> <prompt>",
    usage="/schedule [list|rm <id>|every 30m <prompt>]",
    group="Help",
)
def cmd_schedule(ctx: Context) -> str:
    """Recurring prompts, run through the same queue as a message."""
    schedules = ctx.schedules

    if not ctx.args or ctx.args.strip().lower() in ("list", "ls"):
        rows = [
            f"{s.id}  {s.description}  {s.prompt[:40]}"
            for s in schedules.for_chat(ctx.chat_id)
        ]
        return bullet_list(rows, "No schedules. Try: /schedule every 30m check the build")

    action, _, rest = ctx.args.partition(" ")
    rest = rest.strip()

    if action in ("rm", "remove", "del"):
        if not rest:
            raise CommandError("Usage: /schedule rm <id>")
        # Ids get copy-pasted with trailing punctuation from /schedule list.
        ref = rest.strip().strip(".,:;")
        try:
            removed = schedules.remove(ref)
        except KeyError:
            raise CommandError(
                f"No schedule matching {rest!r}. /schedule list shows them."
            ) from None
        ctx.persist_schedules()
        return f"Removed {removed.id} ({removed.description})"

    # The spec may be several words ("every 2 hours", "daily 09:00"), so find
    # the shortest prefix that parses and treat the remainder as the prompt.
    words = ctx.args.split()
    for count in range(1, len(words) + 1):
        spec = " ".join(words[:count])
        try:
            parse_spec(spec)
        except ValueError:
            continue
        prompt = " ".join(words[count:]).strip()
        if not prompt:
            raise CommandError("What should it do? Try: /schedule every 30m check the build")
        schedule = schedules.add(ctx.chat_id, spec, prompt)
        ctx.persist_schedules()
        return (
            f"Scheduled {schedule.id}: {schedule.description}\n\n\"{schedule.prompt}\""
        )

    raise CommandError(
        f"Bad schedule {action!r}. Use 'every 30m', 'hourly', or 'daily 09:00'."
    )
    ctx.persist_schedules()
    return f"Scheduled {schedule.id}: {schedule.description}\n\n\"{schedule.prompt}\""



@command("status", "Server, session, and bridge state", group="Help")
def cmd_status(ctx: Context) -> str:
    info = ctx.api.info()
    session_id = ctx.session or "(none)"
    pending = len(_pending(ctx))
    scope = (
        "any directory"
        if ctx.config.unrestricted
        else ", ".join(str(root) for root in ctx.config.allowed_roots)
    )
    mode = ctx.state.pref(ctx.chat_id, "auto", ctx.config.approval_mode)
    lines = [
        f"OpenCode {info.get('version', '?')} (pid {info.get('pid', '?')})",
        f"URL      {ctx.config.opencode_url}",
        f"Dir      {ctx.directory}",
        f"Session  {session_id}",
        f"Pending  {pending} permission request(s)",
        f"Auto     {mode} - {ctx.policy.describe()}",
        f"Stream   {'on' if ctx.config.stream_enabled else 'off'}",
        f"Delivery {ctx.state.pref(ctx.chat_id, 'delivery', ctx.config.prompt_delivery)}",
        f"Turn cap {ctx.config.prompt_timeout // 60}m (heads-up at "
        f"{ctx.config.heads_up_after // 60}m)",
        f"Scope    {scope}",
    ]
    return "\n".join(lines)


@command("health", "Check the bridge and the event stream", group="Help")
def cmd_health(ctx: Context) -> str:
    health = ctx.health or {}
    info = ctx.api.info()

    def age(key: str) -> str:
        value = health.get(key)
        return f"{time.time() - value:.0f}s ago" if value else "never"

    rows = [
        ("Server", f"up, version {info.get('version', '?')}"),
        ("Events", _stream_line(health)),
        ("Edits", _edit_line(health)),
        ("Turns", health.get("turns", 0)),
        ("Errors", health.get("errors", 0)),
        ("Last turn", age("last_turn_at")),
        ("Uptime", _uptime(health)),
        ("Watcher", "alive" if health.get("permissions_alive", True) else "DEAD"),
    ]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"{label.ljust(width)}  {value}" for label, value in rows)


def run_and_capture(ctx: Context, command: str, cwd: str, timeout: int) -> str:
    """Run a shell command in *cwd* and return everything it printed.

    The server gives no completion signal for ``POST /api/shell``: the output
    endpoint reports only what has been written so far, the command vanishes
    from the shell list the moment it exits, and whatever it printed in that
    final moment is then gone. Waiting on a status field that never arrives
    meant every command blocked for the full timeout and still reported
    "[running]".

    So the command prints a unique sentinel as its last act. Poll until the
    sentinel appears and the tail cannot be lost, then strip it.
    """
    token = "__bridge_done_" + uuid.uuid4().hex[:12]
    started = time.time()
    # A subshell, not a brace group: `exit 7` inside `{ }` would terminate the
    # shell before the sentinel is printed and the caller would wait forever.
    wrapped = f"( {command} ; ) 2>&1; printf '\\n{token} %s\\n' $?"

    handle = ctx.api.run_shell(wrapped, cwd=cwd, directory=cwd)
    shell_id = handle.get("id")
    deadline = time.time() + timeout
    output = ""

    while True:
        try:
            output = ctx.api.shell_output(shell_id, directory=cwd).get("output", "")
        except ApiError as exc:
            raise CommandError(f"Could not read command output: {exc.message}") from exc

        if token in output:
            body, _, tail = output.partition(f"\n{token}")
            body = body.rstrip("\n")
            code = tail.strip().split(" ", 1)[-1] if " " in tail.strip() else ""
            return f"{body}\n[exit {code}]" if code.isdigit() else body

        # The server reaps a shell that runs too long: no sentinel ever
        # appears, and nothing is left to wait for. Give it a moment, then say
        # so rather than blocking for the full timeout.
        if token not in output and time.time() - started >= SHELL_PROBE:
            if output.strip():
                return output.rstrip("\n")
            raise CommandError(
                "The server stopped that command after about a second, so its "
                "output cannot be captured.\n"
                "Send it as a message instead - the agent can run anything, "
                "however long it takes - or use a command that finishes "
                "immediately."
            )

        if time.time() >= deadline:
            return output.rstrip("\n") or "(no output)"
        time.sleep(0.15)


def _uptime(health: dict) -> str:
    started = health.get("started_at")
    if not started:
        return "n/a"
    return f"{time.time() - started:.0f}s"


def _edit_line(health: dict) -> str:
    """Edit reliability, because a failed final edit once ate a whole reply."""
    stats = health.get("telegram")
    if not stats:
        return "unknown"
    interval = stats.get("edit_interval") or 0
    parts = []
    if interval:
        parts.append(f"every {interval:.1f}s")
    if stats.get("edit_failures"):
        parts.append(f"{stats['edit_failures']} failed")
    if stats.get("rate_limited"):
        parts.append(f"{stats['rate_limited']} throttled")
    return ", ".join(parts) or "healthy"


def _stream_line(health: dict) -> str:
    events = health.get("events") or {}
    if not events:
        return "not started"
    if events.get("connected"):
        return f"connected, {events.get('frames', 0)} frames"
    return f"reconnecting ({events.get('reconnects', 0)} times)"


@command(
    "selftest",
    "Run end-to-end checks against the live server",
    usage="/selftest",
    group="Help",
)
def cmd_selftest(ctx: Context) -> str:
    """Exercise the real paths, not a fake.

    Everything else in the suite runs against a stub, and a stub will happily
    accept a wrong content type or an ignored query parameter - which is how
    three real defects survived a green test run.
    """
    from . import selftest as selftest_module

    directory = ctx.target_dir(None)
    try:
        report = selftest_module.run(ctx.api, directory)
    except ApiError as exc:
        return f"! Self-test could not start: {exc.message}"

    header = f"Self-test in {directory}\n"
    return header + report.render()



@command("doctor", "Check the bridge end to end", group="Help")
def cmd_doctor(ctx: Context) -> str:
    """Self-check: config, connectivity, disk, threads, and delivery health.

    Written for the moment something looks wrong and the user cannot see a
    terminal, so every line says pass or fail with the reason.
    """
    checks: list[tuple[str, bool, str]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok, detail))

    # Configuration
    check("Token set", bool(ctx.config.token), "TELEGRAM_BOT_TOKEN")
    check(
        "Allow-list",
        bool(ctx.config.allowed_chats),
        f"{len(ctx.config.allowed_chats)} chat(s)",
    )
    check("This chat allowed", ctx.chat_id in ctx.config.allowed_chats)
    check("Working dir", True, ctx.directory)
    if ctx.config.unrestricted:
        check("Scope", False, "unrestricted: sessions may target any directory")
    else:
        check("Scope", True, ", ".join(str(r) for r in ctx.config.allowed_roots))

    # Server
    try:
        info = ctx.api.info()
        version = info.get("version", "?")
        check("OpenCode server", True, f"v{version} pid {info.get('pid', '?')}")
    except ApiError as exc:
        check("OpenCode server", False, exc.message[:70])

    # Session
    session = ctx.session
    if session:
        try:
            ctx.api.get_session(session)
            check("Active session", True, session)
        except ApiError:
            check("Active session", False, f"{session[:14]}... unreachable")
    else:
        check("Active session", False, "none; use /new")

    # State on disk
    state_path = ctx.config.state_file
    try:
        readable = state_path.exists() and state_path.stat().st_size >= 0
        mode = oct(state_path.stat().st_mode & 0o777) if state_path.exists() else "-"
        check("State file", readable, f"{mode} {state_path.name}")
    except OSError as exc:
        check("State file", False, str(exc)[:60])

    # Attachments cache
    cache = ctx.config.attachments_dir
    try:
        files = list(cache.iterdir()) if cache.exists() else []
        usage = sum(f.stat().st_size for f in files if f.is_file())
        check(
            "Attachment cache", len(files) <= 100, f"{len(files)} file(s), {usage // 1024} KB"
        )
    except OSError:
        check("Attachment cache", True, "not created yet")

    # Threads and delivery
    health = ctx.health or {}
    events = health.get("events") or {}
    check(
        "Event stream",
        bool(events.get("connected")),
        f"connected, {events.get('frames', 0)} frames, "
        f"{events.get('reconnects', 0)} reconnect(s)",
    )
    check(
        "Permission watcher",
        bool(health.get("permissions_alive", True)),
        "polling for approvals",
    )
    telegram = health.get("telegram")
    if telegram:
        check(
            "Delivery",
            telegram.get("edit_failures", 0) == 0,
            f"{telegram.get('edit_failures', 0)} failed edit(s), "
            f"{telegram.get('rate_limited', 0)} throttled",
        )

    # Turn timing
    check(
        "Turn limits",
        True,
        f"cap {ctx.config.prompt_timeout // 60}m, heads-up "
        f"{ctx.config.heads_up_after // 60}m, delivery "
        f"{ctx.state.pref(ctx.chat_id, 'delivery', ctx.config.prompt_delivery)}",
    )

    failures = sum(1 for _, ok, _ in checks if not ok)
    lines = [
        f"{'PASS' if ok else 'WARN'}  {label}" + (f"  -  {detail}" if detail else "")
        for label, ok, detail in checks
    ]
    lines.append("")
    lines.append("All good." if not failures else f"{failures} warning(s) above.")
    return "\n".join(lines)


def _help_text() -> str:
    groups: dict[str, list[Command]] = {}
    for cmd in REGISTRY.values():
        groups.setdefault(cmd.group, []).append(cmd)

    lines = ["Send any message to talk to the agent.", ""]
    for group in sorted(groups):
        lines.append(f"-- {group} --")
        lines.extend(
            f"{cmd.help_line()}" for cmd in sorted(groups[group], key=lambda c: c.name)
        )
        lines.append("")
    lines.append("Examples:")
    lines.append("  /new ~/Desktop/project Start here")
    lines.append("  /sessions             then /use #2")
    lines.append("  /model opencode/claude-haiku-5-5")
    lines.append("  /auto read            auto-approve read-only work")
    lines.append("  /sh git status")
    lines.append("  /write notes.md\\nhello")
    lines.append("Send a photo or file and it is attached to your next message.")
    return "\n".join(lines).strip()


@command("help", "Show this message", group="Help")
def cmd_help(ctx: Context) -> str:
    return _help_text()


# ------------------------------------------------------------------- dispatch


def run(ctx: Context, text: str) -> str | None:
    """Execute a command, or return None if the text was not a command.

    The caller treats None as "this is a prompt for the agent".
    """
    parsed = parse(text)
    if not parsed:
        return None

    name, args, rest = parsed
    if name in ("start", "help_"):
        return _help_text()

    cmd = REGISTRY.get(name)
    if not cmd:
        suggestion = _suggest(name)
        hint = f" Did you mean /{suggestion}?" if suggestion else ""
        raise CommandError(f"Unknown command /{name}.{hint} Send /help for the list.")

    ctx.args, ctx.rest = args, rest
    return cmd.handler(ctx)


def _suggest(name: str) -> str | None:
    """Closest command name by edit distance, if it is close enough."""
    best, best_score = None, 3
    for candidate in REGISTRY:
        score = _distance(name, candidate)
        if score < best_score:
            best, best_score = candidate, score
    return best


def _distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]
