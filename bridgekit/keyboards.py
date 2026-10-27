"""Inline keyboards and callback routing.

Buttons save typing on a phone, but they cannot carry arbitrary payloads: the
callback data travels inside a Telegram message and is capped at 64 bytes. So
buttons carry a compact ``action:argument`` string, and the bridge maps the
action to a handler. Anything unrecognised is answered and ignored rather than
executed blindly.
"""

from __future__ import annotations

from collections.abc import Callable

MAX_CALLBACK = 64
SEP = ":"

# A switch button per session beats any number of typed /use arguments, but
# the keyboard still has to fit a phone screen.
MAX_SESSION_BUTTONS = 12


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """Build a reply markup from rows of (label, callback_data) pairs."""
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data[:MAX_CALLBACK]} for label, data in row]
            for row in rows
            if row
        ]
    }


def encode(action: str, argument: str = "") -> str:
    payload = f"{action}{SEP}{argument}" if argument else action
    return payload[:MAX_CALLBACK]


def decode(data: str) -> tuple[str, str]:
    if SEP not in data:
        return data, ""
    action, _, argument = data.partition(SEP)
    return action, argument


class Router:
    """Maps callback actions to callables."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[str, str], None]] = {}

    def on(
        self, action: str
    ) -> Callable[[Callable[[str, str], None]], Callable[[str, str], None]]:
        def decorate(func: Callable[[str, str], None]) -> Callable[[str, str], None]:
            self._handlers[action] = func
            return func

        return decorate

    def resolve(self, action: str) -> Callable[[str, str], None] | None:
        return self._handlers.get(action)

    def actions(self) -> list[str]:
        return sorted(self._handlers)


# --- Built-in keyboards -------------------------------------------------------

# The pinned bottom keyboard. Telegram mirrors these labels back as plain
# text, so labels must map to a home: the bridge canonicalises them into the
# slash commands they stand for before running anything.
MENU_LABELS = {
    "📋 Status": "status",
    "🗂 Sessions": "sessions",
    "✨ New": "new",
    "🛑 Abort": "abort",
    "❓ Help": "help",
}

MENU_ROWS = [
    ["📋 Status", "🗂 Sessions", "✨ New"],
    ["🛑 Abort", "❓ Help"],
]


def reply_menu_markup() -> dict:
    """The persistent main menu, attached once per chat at startup."""
    return {
        "keyboard": [[{"text": label} for label in row] for row in MENU_ROWS],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Ask, or / for commands",
    }


def from_menu(text: str) -> str:
    """Turn a menu label into the command it stands for."""
    command = MENU_LABELS.get(text)
    return f"/{command}" if command else text


def session_list_markup(sessions: list, active_id: str | None = None) -> dict | None:
    """A switch button per recent session.

    Buttons carry the id suffix, which the ``use`` callback resolves.
    Suffixes collide rarely, and Telegram's 64-byte callback budget allows
    the full id when they would.
    """
    rows: list[list[tuple[str, str]]] = []
    used: set[str] = set()
    for session in sessions[:MAX_SESSION_BUTTONS]:
        session_id = session.get("id") or ""
        if not session_id:
            continue
        suffix = session_id[-8:]
        if suffix in used:
            suffix = session_id
        used.add(suffix)
        title = (session.get("title") or "").strip()[:40] or "Untitled"
        mark = "➜ " if session_id == active_id else ""
        rows.append([(f"{mark}{title}", encode("use", suffix))])
    return keyboard(rows) if rows else None


def permission_buttons(request_id: str) -> dict:
    """Approve / always-approve / reject, unless the payload would truncate.

    Telegram caps callback data at 64 bytes and :func:`encode` truncates
    silently: with a long request id the "always allow" button decoded to a
    cut-off id, answered "no longer pending", and left the agent blocked with
    a dead button on screen. When the round-trip no longer returns the id,
    the button is omitted; Allow once and Reject remain.
    """
    rows = [
        ("Allow once", encode("perm_yes", request_id)),
        ("Reject", encode("perm_no", request_id)),
    ]
    always = encode("perm_always", request_id)
    if decode(always) == ("perm_always", request_id):
        rows.insert(1, ("Always allow", always))
    return keyboard([rows])


def session_buttons(session_id: str) -> dict:
    short = session_id[-8:]
    return keyboard(
        [
            [
                ("Abort", encode("abort")),
                ("Info", encode("info")),
                ("Compact", encode("compact")),
            ],
            [("Focus this session", encode("use", short))],
        ]
    )


def help_buttons() -> dict:
    return keyboard(
        [
            [
                ("Status", encode("cmd", "status")),
                ("Sessions", encode("cmd", "sessions")),
                ("Help", encode("cmd", "help")),
            ]
        ]
    )
