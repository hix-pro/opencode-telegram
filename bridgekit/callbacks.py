"""Inline-keyboard callback registration and execution."""

from __future__ import annotations

import logging

from . import commands
from .api import ApiError
from .config import ConfigError

log = logging.getLogger("bridge.callbacks")


def register_callbacks(bridge) -> None:
    router = bridge.router

    def permission_handler(decision: str):
        def handler(chat_id: str, request_id: str) -> None:
            bridge._reply_permission(chat_id, request_id, decision)

        return handler

    router.on("perm_yes")(permission_handler("once"))
    router.on("perm_always")(permission_handler("always"))
    router.on("perm_no")(permission_handler("reject"))

    @router.on("cmd")
    def _run_command(chat_id: str, argument: str) -> None:
        bridge._run_command_text(chat_id, "/" + argument)

    @router.on("abort")
    def _abort(chat_id: str, argument: str) -> None:
        # Same semantics as typed /abort: stop when something is running,
        # say so otherwise, and never leave a stale abort flag armed.
        bridge._stop_turn(chat_id)

    @router.on("info")
    def _info(chat_id: str, argument: str) -> None:
        bridge._run_command_text(chat_id, "/info")

    @router.on("compact")
    def _compact(chat_id: str, argument: str) -> None:
        session = bridge.state.session(chat_id)
        if not session:
            return
        try:
            bridge.api.compact_session(session)
        except ApiError as exc:
            log.warning("Compact failed: %s", exc)

    @router.on("use")
    def _use(chat_id: str, argument: str) -> None:
        """Switch using the id suffix carried on the button."""
        if not argument:
            return
        try:
            sessions = bridge.api.list_sessions(
                directory=bridge.state.directory(chat_id), limit=50
            )
        except ApiError:
            return
        matches = [s for s in sessions if (s.get("id") or "").endswith(argument)]
        if not matches:
            # The list is a recent window and pages shift; silence here
            # looks exactly like a dead button.
            bridge.send(chat_id, "! That session is no longer in the recent list.")
            return
        session = matches[0]
        directory = session.get("directory") or bridge.state.directory(chat_id)
        try:
            # The button path used to skip the containment check the
            # typed /use does: a directory deleted after the button was
            # rendered would switch the chat onto a session that fails
            # on every prompt.
            bridge.config.resolve_dir(directory)
        except ConfigError as exc:
            bridge.send(chat_id, f"! Cannot switch: {exc}")
            return
        bridge.state.set_session(chat_id, session["id"], directory=directory)
        # Never a silent switch: the user tapped something, and
        # Telegram shows nothing on its own.
        title = (session.get("title") or "").strip()[:40]
        suffix = session["id"][-8:]
        bridge.send(
            chat_id,
            f'Switched to "{title}" ({suffix}).'
            if title
            else f"Switched to session {suffix}.",
        )


def run_command_text(bridge, chat_id: str, text: str) -> None:
    """Run a command on behalf of a button press."""
    try:
        ctx = bridge.context(chat_id)
        reply = commands.run(ctx, text)
    except commands.CommandError as exc:
        bridge.send(chat_id, f"! {exc}")
        return
    except ApiError as exc:
        bridge.send(chat_id, f"! {exc.message}")
        return
    except Exception:
        log.exception("Callback command failed")
        bridge.send(chat_id, "! Internal error; check the bridge log.")
        return
    if reply:
        bridge.send(chat_id, reply, ctx.markup)
