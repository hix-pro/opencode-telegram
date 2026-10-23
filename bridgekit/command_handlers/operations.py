"""Schedules, health, self-test and doctor commands."""

from __future__ import annotations

import time

from .. import commands as core
from ..api import ApiError
from ..formatting import bullet_list, truncate
from ..schedule import parse as parse_spec


@core.command(
    "schedule",
    "Run something on a timer: /schedule <every 30m|hourly|daily 09:00> <prompt>",
    usage="/schedule [list|rm <id>|every 30m <prompt>]",
    group="Help",
)
def cmd_schedule(ctx: core.Context) -> str:
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
            raise core.CommandError("Usage: /schedule rm <id>")
        # Ids get copy-pasted with trailing punctuation from /schedule list.
        ref = rest.strip().strip(".,:;")
        try:
            removed = schedules.remove(ref)
        except KeyError:
            raise core.CommandError(
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
            raise core.CommandError(
                "What should it do? Try: /schedule every 30m check the build"
            )
        schedule = schedules.add(ctx.chat_id, spec, prompt)
        ctx.persist_schedules()
        return f'Scheduled {schedule.id}: {schedule.description}\n\n"{schedule.prompt}"'

    raise core.CommandError(
        f"Bad schedule {action!r}. Use 'every 30m', 'hourly', or 'daily 09:00'."
    )


@core.command("status", "Server, session, and bridge state", group="Help")
def cmd_status(ctx: core.Context) -> str:
    info = ctx.api.info()
    session_id = ctx.session or "(none)"
    pending = len(core._pending(ctx))
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


@core.command("health", "Check the bridge and the event stream", group="Help")
def cmd_health(ctx: core.Context) -> str:
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
    line = f"reconnecting ({events.get('reconnects', 0)} times)"
    reason = (events.get("last_error") or "").strip()
    if reason:
        line += f" - {truncate(reason, 70)}"
    return line


@core.command(
    "selftest",
    "Run end-to-end checks against the live server",
    usage="/selftest",
    group="Help",
)
def cmd_selftest(ctx: core.Context) -> str:
    """Exercise the real paths, not a fake.

    Everything else in the suite runs against a stub, and a stub will happily
    accept a wrong content type or an ignored query parameter - which is how
    three real defects survived a green test run.
    """
    from bridgekit import selftest as selftest_module

    directory = ctx.target_dir(None)
    try:
        report = selftest_module.run(ctx.api, directory)
    except ApiError as exc:
        return f"! Self-test could not start: {exc.message}"

    header = f"Self-test in {directory}\n"
    return header + report.render()


@core.command("doctor", "Check the bridge end to end", group="Help")
def cmd_doctor(ctx: core.Context) -> str:
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
