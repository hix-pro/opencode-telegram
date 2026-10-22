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


def permission_buttons(request_id: str) -> dict:
    return keyboard(
        [
            [
                ("Allow once", encode("perm_yes", request_id)),
                ("Always allow", encode("perm_always", request_id)),
                ("Reject", encode("perm_no", request_id)),
            ]
        ]
    )


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
