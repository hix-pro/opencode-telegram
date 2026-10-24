"""Telegram update ingress: authorization, callbacks and attachments.

The methods live here as functions taking the bridge instance; ``Bridge`` keeps
thin wrappers so tests and callbacks can still patch the historic method names.
"""

from __future__ import annotations

import logging

# This module owns the one sanctioned shell-out in the bridge: STT_COMMAND is
# the operator's own template and the path is shell-quoted.
import shlex
import subprocess  # nosec

from . import attachments, authz, commands, keyboards
from .api import ApiError
from .telegram import TelegramError

log = logging.getLogger("bridge.updates")

# Reactions that steer the bridge without typing. Approve answers one pending
# permission request (or an explicit card); a batch needs /yes all. Stop
# interrupts the running turn. Only a newly *added* reaction acts.
APPROVE_REACTIONS = frozenset({"👍"})
STOP_REACTIONS = frozenset({"👎", "🛑"})


def handle_update(bridge, update: dict) -> None:
    if "callback_query" in update:
        bridge._handle_callback(update["callback_query"])
        return

    if "message_reaction" in update:
        bridge._handle_reaction(update["message_reaction"])
        return

    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    # The chat alone is not enough in a group: any member could otherwise
    # send prompts, /sh or attachments. The shared rule also checks the
    # sender, matching edits, reactions and buttons.
    chat_id = authz.authorize(
        message.get("chat"), message.get("from"), bridge.config.allowed_chats
    )
    if not chat_id:
        return

    if "message" not in update:
        # Telegram reposted an edit (to a user or bot message).
        bridge._handle_edited_message(chat_id, message)
        return

    # A file, caption, or voice note instead of a command.
    found = attachments.extract(message)
    if found:
        bridge._handle_attachment(chat_id, message, *found)
        return

    text = (message.get("text") or "").strip()
    if not text:
        return

    # A tap on the bottom keyboard arrives as its label; run what the
    # label stands for. Unknown labels are not commands and need no
    # translation.
    text = keyboards.from_menu(text)

    # A standing abort intent stays armed while a turn is actually running:
    # clearing on any message used to silently cancel an interrupt whose API
    # call had failed, so the turn the user tried to stop kept running. The
    # flag is wiped only once nothing runs any more-or an explicit /abort.
    abort = bridge.abort_flag(chat_id)
    if abort.is_set() and not text.startswith("/abort") and not bridge._chat_busy(chat_id):
        abort.clear()

    try:
        ctx = bridge.context(chat_id)
        reply = commands.run(ctx, text)
    except commands.CommandError as exc:
        bridge.send(chat_id, f"! {exc}")
        return
    except ApiError as exc:
        log.warning("Command failed: %s", exc)
        bridge.send(chat_id, f"! OpenCode error {exc.status or ''}: {exc.message}".strip())
        return
    except Exception:
        log.exception("Command raised")
        bridge.send(chat_id, "! Internal error; check the bridge log.")
        return

    if reply is None:
        # A macro rewrote the text into the standing prompt it stands
        # for; otherwise the raw text is the prompt. commands.run()
        # refuses to hand back an empty rewrite, but the guard stays:
        # queueing "" would silently swallow the message.
        prompt = ctx.rewrite if ctx.rewrite is not None else text
        if prompt:
            bridge.work.put((chat_id, prompt, None))
        return
    if text.startswith("/help"):
        bridge.send(chat_id, reply, keyboards.help_buttons())
        return
    bridge.send(chat_id, reply, ctx.markup)


def handle_callback(bridge, query: dict) -> None:
    # Buttons approve permissions and abort turns. In a group the chat id
    # can be allow-listed while the person tapping is not, so the shared
    # rule checks both.
    chat_id = authz.authorize(
        (query.get("message") or {}).get("chat"),
        query.get("from"),
        bridge.config.allowed_chats,
    )
    if not chat_id:
        return
    action, argument = keyboards.decode(query.get("data") or "")
    handler = bridge.router.resolve(action)
    bridge.telegram.answer_callback(query.get("id", ""))
    if handler:
        try:
            handler(chat_id, argument)
        except Exception:
            log.exception("Callback %s failed", action)


def handle_edited_message(bridge, chat_id: str, message: dict) -> None:
    """Optionally resend an edited message as a fresh prompt.

    This is opt-in (``EDIT_RESEND=true``).  Editing a message is easy to do
    by accident, and re-running a prompt because its wording changed can
    repeat a side effect.  The chat history still doubles as the queue when
    the feature is enabled.

    The bridge's own edits never count - those are progress updates - and
    slash commands are ignored either way, because they already ran when
    first sent.
    """
    sender = message.get("from") or {}
    if sender.get("is_bot"):
        return
    if not bridge.config.edit_resend:
        return
    text = keyboards.from_menu((message.get("text") or "").strip())
    if not text or text.startswith("/"):
        # Slash commands already ran when first sent - and a menu label's
        # canonical form IS a command, so editing the label "sessions"
        # once queued the bare word "sessions" as a prompt for the agent.
        return
    bridge.send(chat_id, "Resending your edit as a new prompt.")
    bridge.work.put((chat_id, text, None))


def handle_reaction(bridge, reaction: dict) -> None:
    """A reaction carries the two actions people reach for fastest.

    Approve answers one pending permission request; a batch requires an
    explicit ``/yes all`` or a reaction on each card. Stop interrupts the
    running turn. Telegram only delivers these updates because
    :data:`telegram.ALLOWED_UPDATES` includes ``message_reaction``.
    Only allow-listed people in allow-listed chats count, and only a
    newly added reaction acts - removing one must never repeat an
    action.
    """
    chat_id = authz.authorize(
        reaction.get("chat"), reaction.get("user"), bridge.config.allowed_chats
    )
    if not chat_id:
        return
    reactor = str((reaction.get("user") or {}).get("id", "") or chat_id)
    emojis = [str(r.get("emoji") or "") for r in reaction.get("new_reaction") or []]
    if not emojis:
        return
    # The receipt: per-request reactions were physically dead for a
    # production round (a wire-format bug), so every arriving reaction
    # is worth one visible line until proven boring.
    log.info(
        "Reaction update: chat %s user %s message %s adds %s",
        chat_id,
        reactor,
        reaction.get("message_id"),
        emojis,
    )
    approve = any(emoji in APPROVE_REACTIONS for emoji in emojis)
    stop = any(emoji in STOP_REACTIONS for emoji in emojis)
    if not (approve or stop):
        return

    # Reacting on a pending permission card is scoped to that one
    # request: approve it, or reject it, wherever it came from.
    try:
        message_id = int(reaction.get("message_id") or 0)
    except (TypeError, ValueError):
        return
    card_request = bridge._perm_cards.get((chat_id, message_id))
    if card_request:
        bridge._reply_permission(chat_id, card_request, "once" if approve else "reject")
        return

    if approve:
        bridge._approve_pending(chat_id)
    if stop:
        bridge._stop_turn(chat_id)


def approve_pending(bridge, chat_id: str) -> None:
    """Approve one pending request from a chat-wide reaction.

    A reaction is a single gesture with no confirmation step, and the card
    mapping is lost across a restart. Approving a batch from a stray thumbs-up
    therefore approved more than the person meant. More than one request now
    requires an explicit ``/yes all`` or a reaction on the card itself.
    """
    try:
        pending = bridge.api.permission_requests(directory=bridge.state.directory(chat_id))
    except ApiError as exc:
        log.warning("Reaction approval listing failed: %s", exc)
        bridge.send(chat_id, f"! Could not list pending approvals: {exc.message}")
        return
    targets = [request for request in pending or [] if request.get("id")]
    if not targets:
        bridge.send(chat_id, "Nothing waiting for approval.")
        return
    if len(targets) > 1:
        bridge.send(
            chat_id,
            f"{len(targets)} permission requests are pending. "
            "React on a card, or use /yes all to approve them explicitly.",
        )
        return
    request = targets[0]
    bridge._reply_permission(chat_id, str(request["id"]), "once", request)


def stop_turn(bridge, chat_id: str) -> str:
    """Interrupt whatever is running here - the shared /abort.

    Fires only when a turn is actually running: arming the abort flag on
    an idle chat used to kill the *next* turn - often a scheduled job - the
    moment it started, looking like a random cancellation. Returns the
    outcome line so the typed /abort, the keyboard button and the 👎
    reaction all report identically.
    """
    session = bridge.state.session(chat_id)
    if not session:
        return "Nothing is running."
    if not bridge._chat_busy(chat_id):
        return "Nothing is running."
    bridge.abort_flag(chat_id).set()
    try:
        bridge.api.interrupt_session(session)
    except ApiError as exc:
        log.warning("Interrupt failed: %s", exc)
        # The abort flag deliberately stays armed: the turn pump honours it
        # at its next poll (about two seconds away), so a refused interrupt
        # is deferred, not cancelled.
        return f"! Could not interrupt the turn: {exc.message}; the pump aborts it shortly."
    return "Stopping - the turn is being interrupted."


def handle_attachment(
    bridge, chat_id: str, message: dict, file_id: str, name: str, kind: str
) -> None:
    caption = (message.get("caption") or "").strip()
    try:
        if kind == "voice":
            bridge._handle_voice(chat_id, file_id, name, caption)
            return
        stored = attachments.store(
            bridge.telegram, file_id, name, kind, bridge.config.attachments_dir, caption
        )
    except (TelegramError, ValueError, OSError) as exc:
        bridge.send(chat_id, f"! Could not fetch that file: {exc}")
        return

    bridge.work.put((chat_id, caption or stored.caption, [stored.as_payload()]))


def handle_voice(bridge, chat_id: str, file_id: str, name: str, caption: str) -> None:
    stored = attachments.store(
        bridge.telegram, file_id, name, "voice", bridge.config.attachments_dir
    )
    transcript = bridge._transcribe(stored.path)
    if transcript:
        prompt = f"{transcript}\n\n(via voice note)" if not caption else caption
        bridge.work.put((chat_id, prompt, None))
        return

    bridge.send(
        chat_id,
        "Voice note received but no transcription tool is configured.\n"
        "Set STT_COMMAND in .env, for example:\n"
        "  STT_COMMAND=whisper -m base {file}\n"
        "Or reply with a photo or document instead.",
    )


def transcribe(bridge, path) -> str | None:
    command = bridge.config.stt_command
    if not command:
        return None
    try:
        # Accepted audit finding, kept on purpose: the command template
        # is the operator's own .env (shell knowledge they already have)
        # and the substituted path is shell-quoted. The generic "no
        # shell=True" advice makes exact transcription quoting worse.
        result = subprocess.run(  # nosec
            command.format(file=shlex.quote(str(path))),
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Transcription failed: %s", exc)
        return None
    if result.returncode != 0:
        log.warning("Transcription exited %s: %s", result.returncode, result.stderr[:200])
        return None
    return result.stdout.strip() or None
