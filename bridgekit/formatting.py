"""Render helpers.

Everything here is designed to stay readable in a Telegram bubble on a phone:
fenced code blocks for machine output, short lines, relative timestamps.
"""

from __future__ import annotations

import time
from collections.abc import Iterable


def truncate(text: str, limit: int = 3500, marker: str = "\n... (truncated)") -> str:
    """Trim to *limit*, keeping the head. Machine output is most useful early."""
    if len(text) <= limit:
        return text
    if limit <= len(marker):
        # The marker alone would exceed the limit.
        return text[: max(0, limit)]
    return text[: max(0, limit - len(marker))] + marker


def tail(text: str, limit: int = 3500, marker: str = "\n... (earlier text trimmed)") -> str:
    """Trim to *limit*, keeping the **end**. The mirror of :func:`truncate`.

    A streaming view wants the newest text: the sentence the agent just wrote
    is the one the reader is waiting for. Keeping the head instead silently
    drops every new sentence once the message passes *limit*, which leaves the
    chat looking wedged while the turn is in fact still progressing.
    """
    if len(text) <= limit:
        return text
    if limit <= len(marker):
        # The marker alone would exceed the limit.
        return text[-max(0, limit) :]
    return marker + text[-(limit - len(marker)) :]


def head_tail(text: str, limit: int = 3500) -> str:
    """Keep both ends, drop the middle. Better for long logs than head-only."""
    if len(text) <= limit:
        return text
    marker = "\n\n... (middle omitted) ...\n\n"
    budget = max(0, limit - len(marker))
    half = budget // 2
    if half == 0:
        # text[-0:] is the entire string, which defeats the limit completely.
        return truncate(text, limit)
    return text[:half] + marker + text[-half:]


def code_block(text: str, language: str = "", limit: int = 3500) -> str:
    text = truncate(text, limit)
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{language}\n{text}\n{fence}"


def ago(ms: float | None) -> str:
    """Render a millisecond epoch as a relative age."""
    if not ms:
        return "unknown"
    seconds = max(0, int(time.time() * 1000 - float(ms)) / 1000)
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 86400 * 30:
        return f"{int(seconds // 86400)}d ago"
    return time.strftime("%Y-%m-%d", time.localtime(float(ms) / 1000))


def short_id(value: str | None, length: int = 10) -> str:
    """`ses_eb946e1e5ffe...` -> `ses_eb946e`. Enough to disambiguate by eye."""
    if not value:
        return "-"
    return value[:length]


def basename(path: str | None) -> str:
    if not path:
        return "-"
    return path.rstrip("/").rsplit("/", 1)[-1] or "/"


def cost_line(cost: dict | None) -> str:
    if not cost:
        return "-"
    total = cost.get("input", 0) + cost.get("output", 0)
    dollars = (
        cost.get("cache", {}).get("read", 0) if isinstance(cost.get("cache"), dict) else 0
    )
    parts = [f"in {total:,} tok"]
    if dollars:
        parts.append(f"cache {dollars:,}")
    return " | ".join(parts)


def session_line(session: dict, active: bool = False, fallback_dir: str = "") -> str:
    """One line in a session list.

    Some server responses omit ``directory``; fall back to the chat's working
    directory so the line still says where the session lives.
    """
    marker = "* " if active else "  "
    title = (session.get("title") or "untitled").strip()
    title = title[:44] + ("..." if len(title) > 44 else "")
    updated = (session.get("time") or {}).get("updated")
    where = session.get("directory") or fallback_dir
    bits = [ago(updated), basename(where)]
    return f"{marker}{short_id(session.get('id'))}  {title}  ({', '.join(bits)})"


def bullet_list(items: Iterable[str], empty: str = "(none)") -> str:
    lines = [f"- {item}" for item in items]
    return "\n".join(lines) if lines else empty


def permission_line(request: dict) -> str:
    """Render a pending permission request compactly."""
    action = request.get("action", "?")
    resources = request.get("resources") or []
    first = resources[0] if resources else ""
    if len(resources) > 1:
        first += f" (+{len(resources) - 1} more)"
    detail = first or request.get("message", "")
    if len(detail) > 90:
        detail = detail[:87] + "..."
    return f"{short_id(request.get('id'))}  {action}  {detail}"
