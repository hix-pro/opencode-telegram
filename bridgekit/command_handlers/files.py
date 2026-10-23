"""Filesystem and shell commands, including the completion sentinel."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from .. import commands as core
from .. import localfs
from ..api import ApiError
from ..formatting import bullet_list, code_block, head_tail
from ..localfs import PathError

log = core.log


@core.command(
    "dir", "Show or switch the working directory", usage="/dir [path]", group="Files"
)
def cmd_dir(ctx: core.Context) -> str:
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


@core.command("projects", "List known projects", group="Files")
def cmd_projects(ctx: core.Context) -> str:
    rows = []
    for project in ctx.api.projects():
        worktree = project.get("worktree") or project.get("directory") or "?"
        rows.append(f"{project.get('id', '?')[:12]}  {worktree}")
    return bullet_list(rows, "No projects.")


@core.command("ls", "List a directory", usage="/ls [path]", group="Files")
def cmd_ls(ctx: core.Context) -> str:
    path = ctx.args or "."
    try:
        entries = localfs.list_dir(ctx.directory, path, ctx.config.allowed_roots)
    except PathError as exc:
        raise core.CommandError(str(exc)) from exc
    if not entries:
        return f"{path} is empty."
    lines = [f"{entry.name}/" if entry.is_dir else entry.name for entry in entries]
    return code_block("\n".join(lines), limit=ctx.config.max_output)


@core.command("find", "Search for files by name", usage="/find <query>", group="Files")
def cmd_find(ctx: core.Context) -> str:
    """Search for files by name, scoped to the working directory."""
    if not ctx.args:
        raise core.CommandError("Usage: /find <query>")
    try:
        matches = localfs.find(ctx.directory, ctx.args, ctx.config.allowed_roots)
    except PathError as exc:
        raise core.CommandError(str(exc)) from exc
    if not matches:
        return f"No files matching {ctx.args!r} under {ctx.directory}"
    return bullet_list(matches)


@core.command("read", "Print a file", usage="/read <path>", group="Files")
def cmd_read(ctx: core.Context) -> str:
    if not ctx.args:
        raise core.CommandError("Usage: /read <path>")
    try:
        content = localfs.read_file(ctx.directory, ctx.args, ctx.config.allowed_roots)
    except PathError as exc:
        raise core.CommandError(str(exc)) from exc
    if not content:
        return "(empty file)"
    return code_block(head_tail(content, ctx.config.max_output), limit=ctx.config.max_output)


@core.command(
    "write",
    "Write a file. Path on line 1, content below",
    usage="/write <path>\n<content>",
    group="Files",
)
def cmd_write(ctx: core.Context) -> str:
    if not ctx.args or not ctx.rest:
        raise core.CommandError("Usage:\n/write notes.md\nfirst line\nsecond line")
    try:
        written = localfs.write_file(
            ctx.directory, ctx.args, ctx.rest, ctx.config.allowed_roots
        )
    except PathError as exc:
        raise core.CommandError(str(exc)) from exc
    size = len(ctx.rest.encode())
    return f"Wrote {size} bytes to {written}"


@core.command(
    "sh",
    "Run a shell command here",
    usage="/sh <command>",
    group="Files",
)
def cmd_sh(ctx: core.Context) -> str:
    """Run a command in the chat's directory and return its output.

    With a live transport (:meth:`core.Context.respond`) the output is delivered
    as a second message when it is ready. The dispatcher runs on the main
    thread, so joining here held every command and every button - /abort
    for every chat - hostage for up to a minute.
    """
    if not ctx.args:
        raise core.CommandError("Usage: /sh <command>")
    blocked = ctx.policy.blocked_command(ctx.args)
    if blocked:
        raise core.CommandError(
            f"Blocked by the command guard: {blocked}\n"
            "If this is intentional, remove the rule with /guard rm."
        )
    if ctx.respond is not None:
        return _sh_async(ctx)
    return _sh_blocking(ctx)


def _sh_reply(command: str, output: str, limit: int) -> str:
    body = output or "(no output)"
    return f"$ {command}\n" + code_block(head_tail(body, limit), limit=limit)


def _sh_blocking(ctx: core.Context) -> str:
    """Run and join, for callers with no out-of-band transport."""
    outcome: dict[str, Any] = {"output": "", "status": "completed"}

    def run() -> None:
        _capture_into(outcome, ctx)

    worker = threading.Thread(target=run, name="shell", daemon=True)
    worker.start()
    # Bounded join: the caller returns rather than hanging the calling thread.
    worker.join(timeout=core.SHELL_TIMEOUT + 5)

    if outcome.get("error"):
        raise core.CommandError(outcome["error"])

    if worker.is_alive():
        return (
            f"$ {ctx.args}\n[still running after {core.SHELL_TIMEOUT}s - "
            "it will finish in the background]"
        )

    if outcome["status"] == "error" and not outcome["output"]:
        outcome["output"] = "internal error running the command"
    return _sh_reply(ctx.args, outcome["output"], ctx.config.max_output)


def _sh_async(ctx: core.Context) -> str:
    """Start the command and return immediately; deliver the output later."""
    command = ctx.args
    # cmd_sh only routes here with a delivery channel; an explicit error
    # beats a bare assert, which vanishes under `python -O` and trips the
    # security scan (B101) on shipped code.
    if ctx.respond is None:
        raise core.CommandError("No background delivery channel is set for this chat.")
    respond = ctx.respond
    outcome: dict[str, Any] = {"output": "", "status": "completed"}

    def run() -> None:
        _capture_into(outcome, ctx)
        if outcome.get("error"):
            respond(f"! {outcome['error']}")
            return
        respond(_sh_reply(command, outcome["output"], ctx.config.max_output))

    threading.Thread(target=run, name="shell", daemon=True).start()
    return f"Running $ {command} - the output follows here when it finishes."


def _capture_into(outcome: dict[str, Any], ctx: core.Context) -> None:
    """Run the shell command, recording the result or failure for delivery."""
    try:
        outcome["output"] = run_and_capture(ctx, ctx.args, ctx.directory, core.SHELL_TIMEOUT)
    except core.CommandError as exc:
        # Runs on a worker thread, where raising would vanish into the join;
        # hand it back to the caller instead.
        outcome["error"] = str(exc)
    except ApiError as exc:
        outcome["status"] = "error"
        outcome["output"] = exc.message
    except Exception:
        log.exception("Shell command failed")
        outcome["status"] = "error"
        outcome["output"] = "internal error running the command"


def run_and_capture(ctx: core.Context, command: str, cwd: str, timeout: int) -> str:
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
    shell_id = str(handle.get("id") or "")
    if not shell_id:
        # Without an id there is nothing to poll for output; failing loudly
        # beats reporting a command stuck "[running]" forever.
        raise core.CommandError("The server did not return a shell id for the command.")
    deadline = time.time() + timeout
    output = ""

    while True:
        try:
            output = ctx.api.shell_output(shell_id, directory=cwd).get("output", "")
        except ApiError as exc:
            raise core.CommandError(f"Could not read command output: {exc.message}") from exc

        if token in output:
            body, _, tail = output.partition(f"\n{token}")
            body = body.rstrip("\n")
            # The sentinel carries " <code>"; the tail after the token is the
            # code itself. (The old probe looked for a space *inside* the
            # stripped tail - there is none - so [exit N] never appeared.)
            tail = tail.strip()
            code = tail.split(" ", 1)[0] if tail else ""
            return f"{body}\n[exit {code}]" if code.isdigit() else body

        # The server reaps a shell that runs too long: no sentinel ever
        # appears, and nothing is left to wait for. Give it a moment, then say
        # so rather than blocking for the full timeout.
        if token not in output and time.time() - started >= core.SHELL_PROBE:
            if output.strip():
                return output.rstrip("\n")
            raise core.CommandError(
                "The server stopped that command after about a second, so its "
                "output cannot be captured.\n"
                "Send it as a message instead - the agent can run anything, "
                "however long it takes - or use a command that finishes "
                "immediately."
            )

        if time.time() >= deadline:
            return output.rstrip("\n") or "(no output)"
        time.sleep(0.15)
