"""Command registry and handlers.

Everything the TUI can do over its HTTP API is reachable from here. Handlers
take a :class:`Context` and return the text to send back, so they stay pure
with respect to Telegram: the caller does the sending, chunking, and error
formatting.

The registry, Context and dispatcher live here. Handlers live in
``bridgekit/command_handlers/`` and register themselves when this module imports
that package at the bottom; they read constants through this module, not copies,
so the historic ``bridgekit.commands`` surface keeps working.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .api import (
    ApiError,
    OpenCode,
)
from .config import Config, ConfigError
from .formatting import (
    basename,
    session_line,
)
from .policy import Policy
from .schedule import ScheduleStore
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
    # The approval policy for this chat; every Context has one (see
    # default_factory) so no command handler has to defend against None.
    policy: Policy = field(default_factory=Policy)
    health: dict = field(default_factory=dict)
    # Lets the bridge drop a cached permission request when a command answers
    # it, so a stale button cannot still resolve. Return value is ignored.
    on_permission_answered: Callable[[str], object] = lambda request_id: None
    # Recurring prompts for this chat; always present (same factory rule).
    schedules: ScheduleStore = field(default_factory=ScheduleStore)
    # Standing prompts recorded with /macro, keyed by the name they run as.
    macros: dict[str, str] = field(default_factory=dict)
    # Out-of-band delivery for this chat, used by long-running commands that
    # finish after the dispatcher has moved on. None keeps commands
    # synchronous.
    respond: Callable[[str], None] | None = None
    # Inline keyboard a command wants attached to its reply; None means no
    # markup. Lets /sessions offer its switch buttons without handlers
    # knowing anything about sending.
    markup: dict | None = None
    # A command (a macro expansion) may rewrite the text that should reach
    # the agent when it returns None; None keeps the original message.
    rewrite: str | None = None

    # ------------------------------------------------------------- shortcuts

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

# The commands Telegram autocomplete offers in the input bar. REGISTRY is
# the whole book (49 entries and growing); the phone keyboard only needs
# the ones a person types daily - 49 suggestions scrolling in the bar is
# its own bug. /help stays the complete reference for the rest.
PRIMARY_COMMANDS = frozenset(
    {
        "abort",
        "auto",
        "health",
        "help",
        "model",
        "new",
        "no",
        "perms",
        "revert",
        "schedule",
        "sessions",
        "sh",
        "status",
        "use",
        "yes",
    }
)

# A macro name: letters/digits/underscore, starting with a letter.
MACRO_NAME = re.compile(r"[a-z][a-z0-9_]{0,23}")

# Names run() dispatches to the help card before it ever consults the
# registry: /start is the canonical wake-the-bot greeting and /help_ is the
# naming it desugars to. They are not registry entries, but a macro recorded
# under one used to be orphaned forever - /macro accepted it, and typing the
# name printed the help card instead of running the macro.
DISPATCH_ALIASES = ("start", "help_")

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
    if name in DISPATCH_ALIASES:
        return _help_text()

    cmd = REGISTRY.get(name)
    if not cmd:
        template = ctx.macros.get(name)
        if template:
            # Expand the standing prompt: an explicit {args} slot is filled;
            # otherwise the tail trails the template. A second line flows
            # through either way.
            tail = args
            if rest:
                tail = f"{tail}\n{rest}" if tail else rest
            if "{args}" in template:
                expanded = template.replace("{args}", tail)
                if not expanded.strip():
                    # "/jira" with no tail used to rewrite to "" and queue
                    # the bare command name as the agent's prompt.
                    raise CommandError(
                        f"/{name} is {template!r} - it needs arguments after it."
                    )
            else:
                expanded = f"{template} {tail}" if tail else template
            ctx.rewrite = expanded.strip()
            return None
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


# Imported last: the modules register their handlers against the shared
# registry above. They read constants through this module, not copies, so
# tests that patch bridgekit.commands still affect the live handlers.
from . import command_handlers  # noqa: E402,F401
