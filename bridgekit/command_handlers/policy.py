"""Approval policy, guard, delivery and cost commands."""

from __future__ import annotations

from .. import commands as core
from ..formatting import bullet_list


@core.command(
    "auto",
    "Auto-approval mode: /auto [ask|read|all|off]",
    usage="/auto [ask|read|all]",
    group="Policy",
)
def cmd_auto(ctx: core.Context) -> str:
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
        raise core.CommandError("Usage: /auto ask | /auto read | /auto all")
    ctx.policy.set_mode(requested)
    ctx.state.set_pref(ctx.chat_id, "auto", requested)
    ctx.persist_policy()
    return f"Auto-approval: {requested} ({ctx.policy.describe()})"


@core.command(
    "policy",
    "Per-action approvals: /policy allow|ask|always|deny <action>",
    usage="/policy [allow|ask|always|deny <action>|clear <action>|reset]",
    group="Policy",
)
def cmd_policy(ctx: core.Context) -> str:
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
            raise core.CommandError(f"Usage: /policy clear <action>. Current rules: {known}")
        if target not in rules:
            raise core.CommandError(f"No rule for {target!r}. Current rules: {known}")
        ctx.policy.clear_rule(ctx.chat_id, target)
        ctx.persist_policy()
        return f"Rule for {target} removed."

    verbs = {"allow": "once", "ask": "ask", "always": "always", "deny": "reject"}
    if verb not in verbs:
        raise core.CommandError(
            "Usage: /policy allow|ask|always|deny <action>, or /policy reset"
        )
    if not target:
        raise core.CommandError(
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


@core.command(
    "guard",
    "Inspect or edit denied commands",
    usage="/guard [add <pattern>|rm <pattern>|test <command>]",
    group="Policy",
)
def cmd_guard(ctx: core.Context) -> str:
    if not ctx.args:
        return f"{len(ctx.policy.deny)} denied patterns:\n{ctx.policy.deny_summary(20)}"

    action, _, rest = ctx.args.partition(" ")
    rest = rest.strip()
    action = action.lower()

    if action == "add":
        if not rest:
            raise core.CommandError("Usage: /guard add <regex>")
        ctx.policy.add_deny(rest)
        ctx.persist_policy()
        return f"Denied. Commands matching {rest!r} will be rejected."

    if action in ("rm", "remove"):
        if not rest:
            raise core.CommandError("Usage: /guard rm <regex>")
        ctx.policy.remove_deny(rest)
        ctx.persist_policy()
        return f"Removed {rest!r}."

    if action == "test":
        if not rest:
            raise core.CommandError("Usage: /guard test <command>")
        blocked = ctx.policy.blocked_command(rest)
        if blocked:
            return f"BLOCKED by {blocked!r}"
        return "Allowed by the guard. Note this does not run anything."

    raise core.CommandError("Usage: /guard [add <pattern>|rm <pattern>|test <command>]")


@core.command(
    "watch", "Toggle live progress for the next turn", usage="/watch [on|off]", group="Policy"
)
def cmd_watch(ctx: core.Context) -> str:
    current = bool(ctx.state.pref(ctx.chat_id, "watch", True))
    if ctx.args:
        current = ctx.args.strip().lower() in ("on", "true", "1", "yes")
    else:
        current = not current
    ctx.state.set_pref(ctx.chat_id, "watch", current)
    return f"Live progress: {'on' if current else 'off'}"


@core.command(
    "delivery",
    "What happens if you message during a long turn: /delivery [queue|steer]",
    usage="/delivery [queue|steer]",
    group="Policy",
)
def cmd_delivery(ctx: core.Context) -> str:
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
        raise core.CommandError("Usage: /delivery queue or /delivery steer")
    ctx.state.set_pref(ctx.chat_id, "delivery", requested)
    return f"Delivery set to {requested}."


@core.command("cost", "Spend and token usage", group="Policy")
def cmd_cost(ctx: core.Context) -> str:
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


@core.command(
    "alert",
    "Warn when a session passes a spend level: /alert [usd]",
    usage="/alert [0.25|off]",
    group="Policy",
)
def cmd_alert(ctx: core.Context) -> str:
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
        raise core.CommandError("Usage: /alert 0.25 or /alert off") from exc
    if amount < 0:
        # Negative thresholds accepted silently made every turn cost-negative
        # and, since progress.cost is never negative, alerts never fired.
        raise core.CommandError("The threshold is a non-negative amount, or /alert off.")
    ctx.state.set_pref(ctx.chat_id, "cost_alert", amount)
    return f"Cost alert set to ${amount:.2f}."
