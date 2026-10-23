"""Approval listing and answering commands."""

from __future__ import annotations

from .. import commands as core
from ..api import PERMISSION_ALWAYS, PERMISSION_ONCE, PERMISSION_REJECT
from ..formatting import permission_line, short_id


@core.command("perms", "List pending permission requests", group="Permissions")
def cmd_perms(ctx: core.Context) -> str:
    requests = core._pending(ctx)
    if not requests:
        return "Nothing waiting for approval."
    lines = [permission_line(r) for r in requests]
    footer = "\nReply with /yes <id|#n|all>, /no <id|#n|all>, or /yes always <id>."
    return f"{len(requests)} pending:\n" + "\n".join(lines) + footer


@core.command(
    "yes",
    "Approve: /yes [id|#n|all] [always]",
    usage="/yes [id|all] [always]",
    group="Permissions",
)
def cmd_yes(ctx: core.Context) -> str:
    return _answer(ctx, PERMISSION_ONCE)


@core.command(
    "no", "Reject a permission request", usage="/no <id|#n|all>", group="Permissions"
)
def cmd_no(ctx: core.Context) -> str:
    return _answer(ctx, PERMISSION_REJECT)


def _answer(ctx: core.Context, base_decision: str) -> str:
    parts = ctx.args.split()
    decision = base_decision
    if "always" in parts:
        parts.remove("always")
        decision = PERMISSION_ALWAYS
    requests = core._pending(ctx)
    if not requests:
        return "Nothing waiting for approval."

    if not parts or parts[0] == "all":
        targets = requests
    elif parts[0].startswith("#"):
        targets = [_pick(requests, core._positive_int(parts[0][1:], "index"))]
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
        raise core.CommandError(f"No request #{index}; {len(requests)} pending.")
    return requests[index - 1]


def _match_request(requests: list[dict], ref: str) -> dict:
    for request in requests:
        if request.get("id") == ref:
            return request
    matches = [r for r in requests if (r.get("id") or "").startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise core.CommandError(f"Ambiguous prefix {ref!r}; use /perms to list them.")
    raise core.CommandError(f"No pending request matching {ref!r}.")
