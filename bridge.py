#!/usr/bin/env python3
"""Telegram bridge for OpenCode.

Architecture
------------
Four background concerns, so nothing the agent does can block the chat:

* **main** - long-polls Telegram and answers commands inline. Commands that
  steer a running turn (``/abort``, ``/yes``, ``/no`` and the inline buttons)
  must stay here rather than in the worker.
* **worker** - one turn at a time: submits a prompt, streams the reply back
  into a single message that is edited in place, and pushes the final text.
* **events** - one SSE connection to ``/api/event`` feeding live text deltas
  and tool activity into the running turn.
* **watcher** - polls permission requests, answers them automatically when the
  policy allows, and otherwise pings the chat with approve/reject buttons.

Security model
--------------
Only senders in ``TELEGRAM_CHAT_ID`` are processed; everyone else is ignored
without a reply. Sessions may target any directory unless ``ALLOWED_ROOTS``
is set. A deny-list blocks destructive commands from being auto-approved even
when the policy is ``all``. The bot token and server password are read from the
environment and never written to disk or the log.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import shlex
import signal

# Suppression justification: bridge.py has exactly one sanctioned shell-out
# (the STT transcription below); everything else here spawns nothing at all.
import subprocess  # nosec
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from bridgekit import attachments, commands, keyboards
from bridgekit.api import ApiError, OpenCode
from bridgekit.config import Config, ConfigError
from bridgekit.events import EventStream
from bridgekit.policy import Policy
from bridgekit.progress import TurnProgress
from bridgekit.schedule import ScheduleStore
from bridgekit.state import State
from bridgekit.telegram import Telegram, TelegramError

log = logging.getLogger("bridge")

MAX_SEEN_PERMISSIONS = 500

# Pending requests cached for button presses.
MAX_CACHED_REQUESTS = 200

# Messages held per chat while a turn is running.
MAX_BACKLOG = 5

# How often the scheduler checks for due work.
SCHEDULE_TICK = 60

# Consecutive transport failures that mean the OpenCode server is gone rather
# than briefly busy. Five polls at the default 2s cadence is about ten seconds.
MAX_TRANSPORT_FAILURES = 5

# Reactions that steer the bridge without typing. Approve answers every
# pending permission request for the chat once; stop interrupts the turn
# that is running. Only a newly *added* reaction acts, so removing one
# never repeats anything.
APPROVE_REACTIONS = frozenset({"👍"})
STOP_REACTIONS = frozenset({"👎", "🛑"})


@dataclass
class Health:
    """Runtime facts for /health and /status."""

    started_at: float = field(default_factory=time.time)
    last_poll_at: float = 0.0
    last_turn_at: float = 0.0
    turns: int = 0
    prompts: int = 0
    errors: int = 0
    streaming: bool = True
    worker_alive: bool = True
    permissions_alive: bool = True

    def uptime(self) -> float:
        return time.time() - self.started_at


class Bridge:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.api = OpenCode(config.opencode_url, config.password)
        self.telegram = Telegram(
            config.token, max_chunk=config.max_chunk, edit_interval=config.edit_interval
        )
        self.state = State(config.state_file, config.default_dir)
        self.events = EventStream(config.opencode_url, ("opencode", config.password))
        self.policy = Policy(
            mode=config.approval_mode,
            deny=list(Policy().deny) if config.auto_deny else [],
        )
        # Set once the state file has been read; before that there is nothing
        # to restore and the config defaults stand.
        self._policy_restored = False
        self.schedules = ScheduleStore()
        self.work: queue.Queue[tuple[str, str, list | None]] = queue.Queue()
        self.stop_event = threading.Event()
        self.aborts: dict[str, threading.Event] = {}
        self._workers: dict[str, threading.Thread] = {}
        # request_id -> request, filled by the watcher so button presses can be
        # answered without an API round trip.
        self._requests: dict[str, dict] = {}
        # Reaction targets: (chat, message_id) of a permission card that is
        # still pending, so a react-on-the-card is scoped to that one
        # request instead of the whole chat.
        self._perm_cards: dict[tuple[str, int], str] = {}
        # Messages that arrived while a chat was mid-turn.
        self._backlog: dict[str, list] = {}
        # Set when polling cannot continue at all.
        self.fatal: str | None = None
        self.health = Health(streaming=config.stream_enabled)
        self.router = keyboards.Router()
        self._lock = threading.Lock()
        self._register_callbacks()

    # ------------------------------------------------------------------ setup

    def start(self) -> None:
        self.state.load()
        legacy = self.state.take_legacy()
        if legacy and legacy.session:
            chats = list(self.config.allowed_chats)
            if len(chats) == 1:
                self.state.set_session(
                    chats[0], legacy.session, directory=legacy.directory or None
                )
                log.info("Adopted session %s from legacy .session.json", legacy.session)

        self._restore_policy()

        log.info("Registered %d commands", len(commands.REGISTRY))
        log.info("Approval policy: %s", self.policy.describe())
        log.info(
            "Turn delivery: %s, hard timeout %dm, heads-up after %dm",
            self.config.prompt_delivery,
            self.config.prompt_timeout // 60,
            self.config.heads_up_after // 60,
        )

        self.schedules.load(self.state.load_schedules())
        if self.schedules.items:
            log.info("Loaded %d schedule(s)", len(self.schedules.items))

        self._report_orphaned_turns()

        attachments.ensure_cache_dir(self.config.attachments_dir)
        self._publish_command_list()
        self._mount_menu()

        if self.config.stream_enabled:
            self.events.start()

        threading.Thread(target=self._worker_loop, name="worker", daemon=True).start()
        threading.Thread(target=self._permission_loop, name="permissions", daemon=True).start()
        threading.Thread(target=self._schedule_loop, name="scheduler", daemon=True).start()

    def _restore_policy(self) -> None:
        """Reload approval settings saved by a previous run."""
        saved = self.state.load_policy()
        if not saved:
            return
        self.policy.restore(saved)
        self._policy_restored = True
        rules = sum(len(v) for v in self.policy.rules.values())
        log.info(
            "Restored policy: mode=%s, %d rule(s) for %d chat(s)",
            self.policy.mode,
            rules,
            len([c for c, r in self.policy.rules.items() if r]),
        )

    def _report_orphaned_turns(self) -> None:
        """Tell chats whose turn died with the previous bridge process.

        Without this a restart during a long job leaves the user staring at a
        chat that will never reply, which is exactly the "the bot shut down"
        experience. The job may well still be running on the server, so the
        message says where it got to instead of claiming failure.
        """
        for chat_id, turn in self.state.orphaned_turns():
            self.state.clear_busy(chat_id)
            if chat_id not in self.config.allowed_chats:
                continue
            minutes = max(1, int((time.time() - (turn.get("started") or 0)) / 60))
            still_running = False
            try:
                messages = self.api.messages(turn["session"], limit=10, order="desc")
                still_running = not any(
                    m.get("type") == "idle"
                    and (m.get("time") or {}).get("created", 0) > (turn.get("started") or 0)
                    for m in messages
                )
            except ApiError:
                pass

            tail = ""
            if still_running:
                tail = (
                    " The job itself is still running on the server - give it time, "
                    "or /abort it."
                )
            self.send(
                chat_id,
                f"The bridge restarted {minutes}m into a turn.{tail}\n"
                "Resend your message to pick up from here.",
            )

    def _register_callbacks(self) -> None:
        router = self.router

        def permission_handler(decision: str):
            def handler(chat_id: str, request_id: str) -> None:
                self._reply_permission(chat_id, request_id, decision)

            return handler

        router.on("perm_yes")(permission_handler("once"))
        router.on("perm_always")(permission_handler("always"))
        router.on("perm_no")(permission_handler("reject"))

        @router.on("cmd")
        def _run_command(chat_id: str, argument: str) -> None:
            self._run_command_text(chat_id, "/" + argument)

        @router.on("abort")
        def _abort(chat_id: str, argument: str) -> None:
            # Same semantics as typed /abort: stop when something is running,
            # say so otherwise, and never leave a stale abort flag armed.
            self._stop_turn(chat_id)

        @router.on("info")
        def _info(chat_id: str, argument: str) -> None:
            self._run_command_text(chat_id, "/info")

        @router.on("compact")
        def _compact(chat_id: str, argument: str) -> None:
            session = self.state.session(chat_id)
            if not session:
                return
            try:
                self.api.compact_session(session)
            except ApiError as exc:
                log.warning("Compact failed: %s", exc)

        @router.on("use")
        def _use(chat_id: str, argument: str) -> None:
            """Switch using the id suffix carried on the button."""
            if not argument:
                return
            try:
                sessions = self.api.list_sessions(
                    directory=self.state.directory(chat_id), limit=50
                )
            except ApiError:
                return
            matches = [s for s in sessions if (s.get("id") or "").endswith(argument)]
            if not matches:
                # The list is a recent window and pages shift; silence here
                # looks exactly like a dead button.
                self.send(chat_id, "! That session is no longer in the recent list.")
                return
            session = matches[0]
            directory = session.get("directory") or self.state.directory(chat_id)
            try:
                # The button path used to skip the containment check the
                # typed /use does: a directory deleted after the button was
                # rendered would switch the chat onto a session that fails
                # on every prompt.
                self.config.resolve_dir(directory)
            except ConfigError as exc:
                self.send(chat_id, f"! Cannot switch: {exc}")
                return
            self.state.set_session(chat_id, session["id"], directory=directory)
            # Never a silent switch: the user tapped something, and
            # Telegram shows nothing on its own.
            title = (session.get("title") or "").strip()[:40]
            suffix = session["id"][-8:]
            self.send(
                chat_id,
                f'Switched to "{title}" ({suffix}).'
                if title
                else f"Switched to session {suffix}.",
            )

    def _run_command_text(self, chat_id: str, text: str) -> None:
        """Run a command on behalf of a button press."""
        try:
            ctx = self.context(chat_id)
            reply = commands.run(ctx, text)
        except commands.CommandError as exc:
            self.send(chat_id, f"! {exc}")
            return
        except ApiError as exc:
            self.send(chat_id, f"! {exc.message}")
            return
        except Exception:
            log.exception("Callback command failed")
            self.send(chat_id, "! Internal error; check the bridge log.")
            return
        if reply:
            self.send(chat_id, reply, ctx.markup)

    def _publish_command_list(self) -> None:
        """Publish commands and the menu button so the UI knows what exists.

        Telegram autocompletes slash commands from the bot's command list,
        which the bridge owns but never told Telegram about; every command
        had to be remembered by hand. The list is the *daily* commands
        (:data:`commands.PRIMARY_COMMANDS`), not the whole registry - the
        input bar is a suggestion bar, not a book, and /help stays the
        complete reference. The Menu button becomes the command keyboard,
        the one discoverable control when nothing else is known.
        Cosmetic: failures are logged and the bridge runs on.
        """
        entries = [
            {"command": name, "description": commands.REGISTRY[name].summary}
            for name in sorted(commands.PRIMARY_COMMANDS)
        ]
        published = self.telegram.set_my_commands(entries)
        menus = sum(
            1
            for chat_id in self.config.allowed_chats
            if self.telegram.set_menu_button(chat_id)
        )
        log.info(
            "Published %d commands (autocomplete %s); menu button on %d chat(s)",
            len(entries),
            "on" if published else "FAILED",
            menus,
        )

    def _mount_menu(self) -> None:
        """Attach the persistent main menu to each allowed chat.

        A reply keyboard rides on a message, so the startup notice carries
        it; every restart re-attaches and thus fingerprints the live
        process. Telegram lets any client collapse it, so this never
        blocks typing.
        """
        markup = keyboards.reply_menu_markup()
        for chat_id in self.config.allowed_chats:
            self.send(
                chat_id, "Bridge is live.\nTap a menu button, or type / for commands.", markup
            )

    # ---------------------------------------------------------------- helpers

    def abort_flag(self, chat_id: str) -> threading.Event:
        with self._lock:
            return self.aborts.setdefault(chat_id, threading.Event())

    def clear_abort(self, chat_id: str) -> None:
        self.abort_flag(chat_id).clear()

    def send(self, chat_id: str, text: str, markup: dict | None = None) -> None:
        try:
            self.telegram.send(chat_id, text, reply_markup=markup)
        except (TelegramError, OSError) as exc:
            log.warning(
                "Could not deliver message to %s: %s", chat_id, self.telegram.redact(exc)
            )

    def context(self, chat_id: str) -> commands.Context:
        return commands.Context(
            api=self.api,
            state=self.state,
            config=self.config,
            telegram=self.telegram,
            chat_id=chat_id,
            policy=self.policy,
            health=self.health_report(),
            on_permission_answered=lambda request_id: self._requests.pop(request_id, None),
            schedules=self.schedules,
            macros=dict(self.state.macros),
            respond=lambda text: self.send(chat_id, text),
        )

    def health_report(self) -> dict:
        """A plain snapshot for /health and /status."""
        return {
            "started_at": self.health.started_at,
            "last_turn_at": self.health.last_turn_at,
            "turns": self.health.turns,
            "prompts": self.health.prompts,
            "errors": self.health.errors,
            "worker_alive": self.health.worker_alive,
            "permissions_alive": self.health.permissions_alive,
            "uptime": self.health.uptime(),
            "streaming": self.health.streaming,
            "events": {
                "connected": self.events.connected,
                "frames": self.events.frames,
                "reconnects": self.events.reconnects,
                "last_error": self.events.last_error,
            },
            "telegram": {
                "edit_failures": self.telegram.edit_failures,
                "rate_limited": self.telegram.rate_limited,
                "edit_interval": self.telegram.effective_interval(),
            },
        }

    def ensure_session(self, chat_id: str) -> str:
        session = self.state.session(chat_id)
        if session:
            return session
        directory = self.state.directory(chat_id)
        created = self.api.create_session(directory)
        self.state.set_session(chat_id, created["id"], directory=directory)
        log.info("Created session %s for chat %s", created["id"], chat_id)
        return created["id"]

    # ------------------------------------------------------------- main loop

    def _touch_heartbeat(self) -> None:
        """Stamp the liveness file the heartbeat watchdog reads.

        Best effort in every way: computed lazily so test-built bridges work
        unchanged, OSError swallowed (a read-only disk should not take the
        poll thread down), and the file's own mtime carries the information -
        nothing is ever appended.
        """
        path = getattr(self, "_heartbeat_path", None)
        if path is None:
            path = self.config.state_file.parent / ".bridge-heartbeat"
            self._heartbeat_path = path
        try:
            path.touch(exist_ok=True)
        except OSError:
            pass

    def poll_loop(self) -> None:
        # Resume where we left off: starting at zero would re-deliver every
        # update Telegram still holds, so a restart could resubmit your prompt.
        offset = self.state.load_offset()
        if offset:
            log.info("Resuming Telegram updates from %d", offset)
        log.info("Polling Telegram; /help lists commands")
        while not self.stop_event.is_set():
            # Liveness signal for the heartbeat watchdog: touched before the
            # long poll every iteration, success or failure alike - the
            # watchdog must restart a *wedged* process, not one whose calls
            # are erroring while it lives.
            self._touch_heartbeat()
            try:
                updates = self.telegram.get_updates(offset, timeout=30)
            except TelegramError as exc:
                log.error("%s", exc)
                self.fatal = str(exc)
                return
            except requests.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", 0)
                if status in (401, 403, 409):
                    # 401/403 is a bad token and 409 is another poller.
                    # Retrying forever just fills the journal: exit non-zero
                    # so systemd's start limit trips and the failure is visible.
                    self.fatal = f"Telegram rejected polling: HTTP {status}"
                    log.error(self.fatal)
                    return
                # Everything else (429 throttling, 5xx, gateway hiccups) is
                # transient: sleeping through it beats restarting the whole
                # bridge, which previously bounced and re-announced itself
                # every few seconds during a Telegram outage.
                backoff = 5
                if status == 429:
                    try:
                        body = exc.response.json()  # type: ignore[union-attr]
                        params = (body.get("parameters") or {}) if body else {}
                        backoff = max(backoff, int(params.get("retry_after") or 0))
                    except (ValueError, AttributeError):
                        pass
                log.warning(
                    "Telegram poll got HTTP %s; retrying in %ds: %s",
                    status,
                    backoff,
                    exc.__class__.__name__,
                )
                time.sleep(backoff)
                continue
            except OSError as exc:
                log.warning("Telegram poll failed: %s", self.telegram.redact(exc))
                time.sleep(5)
                continue

            self.health.last_poll_at = time.time()
            for update in updates:
                if not isinstance(update, dict):
                    log.warning("Ignoring malformed update: %r", update)
                    continue
                update_id = update.get("update_id")
                if not isinstance(update_id, int):
                    log.warning("Ignoring update without a valid id: %r", update)
                    continue
                offset = update_id + 1
                try:
                    self._handle_update(update)
                except Exception:
                    # A single malformed update must not kill the poll thread.
                    # Log it and advance the offset, or Telegram redelivers the
                    # same poison update on every restart.
                    log.exception("Update %s failed; skipping it", update_id)
                # Per update, not per batch: a crash mid-batch used to forfeit
                # the acknowledged position of every update already handled in
                # it, so those were replayed as duplicate turns on restart.
                self.state.save_offset(offset)

    def _handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return

        if "message_reaction" in update:
            self._handle_reaction(update["message_reaction"])
            return

        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if chat_id not in self.config.allowed_chats:
            # Not on the allow-list: ignore completely, no error, no hint.
            return
        # The chat alone is not enough in a group: any member could otherwise
        # send prompts, /sh or attachments. Require the sender too, matching
        # edits, reactions and buttons. In a private chat both ids are equal.
        sender = str((message.get("from") or {}).get("id", "") or chat_id)
        if sender not in self.config.allowed_chats:
            return

        if "message" not in update:
            # Telegram reposted an edit (to a user or bot message).
            self._handle_edited_message(chat_id, message)
            return

        # A file, caption, or voice note instead of a command.
        found = attachments.extract(message)
        if found:
            self._handle_attachment(chat_id, message, *found)
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        # A tap on the bottom keyboard arrives as its label; run what the
        # label stands for. Unknown labels are not commands and need no
        # translation.
        text = keyboards.from_menu(text)

        if self.abort_flag(chat_id).is_set() and not text.startswith("/abort"):
            self.abort_flag(chat_id).clear()

        try:
            ctx = self.context(chat_id)
            reply = commands.run(ctx, text)
        except commands.CommandError as exc:
            self.send(chat_id, f"! {exc}")
            return
        except ApiError as exc:
            log.warning("Command failed: %s", exc)
            self.send(chat_id, f"! OpenCode error {exc.status or ''}: {exc.message}".strip())
            return
        except Exception:
            log.exception("Command raised")
            self.send(chat_id, "! Internal error; check the bridge log.")
            return

        if reply is None:
            # A macro rewrote the text into the standing prompt it stands
            # for; otherwise the raw text is the prompt. commands.run()
            # refuses to hand back an empty rewrite, but the guard stays:
            # queueing "" would silently swallow the message.
            prompt = ctx.rewrite if ctx.rewrite is not None else text
            if prompt:
                self.work.put((chat_id, prompt, None))
            return
        if text.startswith("/help"):
            self.send(chat_id, reply, keyboards.help_buttons())
            return
        self.send(chat_id, reply, ctx.markup)

    def _handle_callback(self, query: dict) -> None:
        chat_id = str((query.get("message") or {}).get("chat", {}).get("id", ""))
        if chat_id not in self.config.allowed_chats:
            return
        # Buttons approve permissions and abort turns. In a group the chat id
        # can be allow-listed while the person tapping is not, so the sender
        # must pass the same check reactions and edits already use.
        sender = str((query.get("from") or {}).get("id", "") or chat_id)
        if sender not in self.config.allowed_chats:
            return
        action, argument = keyboards.decode(query.get("data") or "")
        handler = self.router.resolve(action)
        self.telegram.answer_callback(query.get("id", ""))
        if handler:
            try:
                handler(chat_id, argument)
            except Exception:
                log.exception("Callback %s failed", action)

    def _handle_edited_message(self, chat_id: str, message: dict) -> None:
        """Editing a sent message resends its text as a fresh prompt.

        The chat history doubles as the queue: correct a typo, refine an
        ask, and the new wording becomes a new turn without retyping.
        The bridge's own edits never count - those are progress updates -
        and slash commands are ignored, because they already ran when
        first sent and re-running one because its wording changed is more
        surprise than service.
        """
        sender = message.get("from") or {}
        if sender.get("is_bot"):
            return
        reactor = str(sender.get("id", "") or chat_id)
        if reactor not in self.config.allowed_chats:
            return
        text = keyboards.from_menu((message.get("text") or "").strip())
        if not text or text.startswith("/"):
            # Slash commands already ran when first sent - and a menu label's
            # canonical form IS a command, so editing the label "sessions"
            # once queued the bare word "sessions" as a prompt for the agent.
            return
        self.send(chat_id, "Resending your edit as a new prompt.")
        self.work.put((chat_id, text, None))

    # ---------------------------------------------------------- attachments

    def _handle_reaction(self, reaction: dict) -> None:
        """A reaction carries the two actions people reach for fastest.

        Approve answers every pending permission request; stop interrupts
        the running turn. Telegram only delivers these updates because
        :data:`telegram.ALLOWED_UPDATES` includes ``message_reaction``.
        Only allow-listed people in allow-listed chats count, and only a
        newly added reaction acts - removing one must never repeat an
        action.
        """
        chat_id = str((reaction.get("chat") or {}).get("id", ""))
        if chat_id not in self.config.allowed_chats:
            return
        reactor = str((reaction.get("user") or {}).get("id", "") or chat_id)
        if reactor not in self.config.allowed_chats:
            return
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
        card_request = self._perm_cards.get((chat_id, message_id))
        if card_request:
            self._reply_permission(chat_id, card_request, "once" if approve else "reject")
            return

        if approve:
            self._approve_all_pending(chat_id)
        if stop:
            self._stop_turn(chat_id)

    def _approve_all_pending(self, chat_id: str) -> None:
        """Approve everything pending in this chat's directory, once each."""
        try:
            pending = self.api.permission_requests(directory=self.state.directory(chat_id))
        except ApiError as exc:
            log.warning("Reaction approval listing failed: %s", exc)
            self.send(chat_id, f"! Could not list pending approvals: {exc.message}")
            return
        targets = [request for request in pending or [] if request.get("id")]
        if not targets:
            self.send(chat_id, "Nothing waiting for approval.")
            return
        for request in targets:
            self._reply_permission(chat_id, str(request["id"]), "once", request)

    def _stop_turn(self, chat_id: str) -> None:
        """Interrupt whatever is running here - the typing-free /abort.

        Fires only when a turn is actually running: arming the abort flag on
        an idle chat used to kill the *next* turn - often a scheduled job - the
        moment it started, looking like a random cancellation.
        """
        session = self.state.session(chat_id)
        if not session:
            self.send(chat_id, "Nothing is running.")
            return
        if not self._chat_busy(chat_id):
            self.send(chat_id, "Nothing is running.")
            return
        self.abort_flag(chat_id).set()
        try:
            self.api.interrupt_session(session)
        except ApiError as exc:
            log.warning("Interrupt failed: %s", exc)
            self.send(chat_id, f"! Could not interrupt the turn: {exc.message}")
            return
        self.send(chat_id, "Stopping - the turn is being interrupted.")

    def _handle_attachment(
        self, chat_id: str, message: dict, file_id: str, name: str, kind: str
    ) -> None:
        caption = (message.get("caption") or "").strip()
        try:
            if kind == "voice":
                self._handle_voice(chat_id, file_id, name, caption)
                return
            stored = attachments.store(
                self.telegram, file_id, name, kind, self.config.attachments_dir, caption
            )
        except (TelegramError, ValueError, OSError) as exc:
            self.send(chat_id, f"! Could not fetch that file: {exc}")
            return

        self.work.put((chat_id, caption or stored.caption, [stored.as_payload()]))

    def _handle_voice(self, chat_id: str, file_id: str, name: str, caption: str) -> None:
        stored = attachments.store(
            self.telegram, file_id, name, "voice", self.config.attachments_dir
        )
        transcript = self._transcribe(stored.path)
        if transcript:
            prompt = f"{transcript}\n\n(via voice note)" if not caption else caption
            self.work.put((chat_id, prompt, None))
            return

        self.send(
            chat_id,
            "Voice note received but no transcription tool is configured.\n"
            "Set STT_COMMAND in .env, for example:\n"
            "  STT_COMMAND=whisper -m base {file}\n"
            "Or reply with a photo or document instead.",
        )

    def _transcribe(self, path) -> str | None:
        command = self.config.stt_command
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

    # ----------------------------------------------------------- worker loop

    def _worker_loop(self) -> None:
        """Dispatch queued turns, one worker thread per chat.

        Turns are serialised *within* a chat, because a session processes one
        turn at a time anyway, but different chats run concurrently. A single
        global worker meant one twenty-minute job silently blocked every other
        allow-listed user for twenty minutes.
        """
        while not self.stop_event.is_set():
            try:
                chat_id, text, files = self.work.get(timeout=1.0)
            except queue.Empty:
                # The dispatcher demonstrably ran this iteration, so the
                # infrastructure is alive - the number of registered turn
                # workers has nothing to do with that.
                self.health.worker_alive = True
                continue

            self.health.worker_alive = True
            # The busy check and the backlog append must be atomic against the
            # turn worker's "pop worker + drain backlog" section: check-then-act
            # used to straddle the turn end, so a message could be appended to a
            # backlog tuple that had already been drained and was never sent.
            queued = False
            busy = False
            with self._lock:
                busy = self._chat_busy(chat_id)
                if busy:
                    backlog = self._backlog.setdefault(chat_id, [])
                    if len(backlog) < MAX_BACKLOG:
                        backlog.append((chat_id, text, files))
                        queued = True
            if not busy:
                pass
            elif queued:
                # /delivery promises queueing, so hold the message rather than
                # making the user retype it - and say so, because a silently
                # delayed message looks like a dropped one.
                self.send(
                    chat_id,
                    "! Queued - I am still working on the previous message.",
                )
                self.work.task_done()
                continue
            else:
                self.send(chat_id, "! Backlog full; send that again once I finish.")
                self.work.task_done()
                continue

            # Register before starting: the thread sets its own identity on
            # entry, so between .start() and that assignment a second prompt
            # for the same chat would look idle and run concurrently.
            placeholder = threading.current_thread()
            with self._lock:
                self._workers[chat_id] = placeholder
            thread = threading.Thread(
                target=self._turn_worker,
                args=(chat_id, text, files),
                name=f"turn-{chat_id}",
                daemon=True,
            )
            thread.start()
            self.work.task_done()

    def _chat_busy(self, chat_id: str) -> bool:
        worker = self._workers.get(chat_id)
        return bool(worker and worker.is_alive())

    def _turn_worker(self, chat_id: str, text: str, files: list | None) -> None:
        """Run one turn in its own thread. Errors are reported, never raised."""
        self._workers[chat_id] = threading.current_thread()
        try:
            self._run_turn(chat_id, text, files)
        except ApiError as exc:
            self.health.errors += 1
            self.send(chat_id, f"! OpenCode error: {exc.message}")
        except Exception:
            log.exception("Turn failed")
            self.health.errors += 1
            self.send(chat_id, "! The turn failed; check the bridge log.")
        finally:
            self.clear_abort(chat_id)
            # Pop the worker and drain the backlog as one step so a dispatcher
            # arriving between the two either still sees this turn as busy (and
            # appends to the backlog we then drain) or sees it free (and runs
            # directly). The old pop-then-drain let an interleaved append land
            # in a backlog nobody would ever revisit.
            with self._lock:
                if self._workers.get(chat_id) is threading.current_thread():
                    self._workers.pop(chat_id, None)
                drained = self._backlog.pop(chat_id, [])
            for item in drained:
                self.work.put(item)

    def _run_turn(self, chat_id: str, text: str, files: list | None = None) -> None:
        session = self.ensure_session(chat_id)
        self.health.turns += 1
        self.health.last_turn_at = time.time()
        self.health.prompts += 1
        log.info(
            "Prompt for %s (%d chars): %s",
            chat_id,
            len(text),
            text[: self.config.log_prompt_chars].replace("\n", " "),
        )

        watch = bool(self.state.pref(chat_id, "watch", True) and self.config.stream_enabled)
        progress = TurnProgress()
        # Per turn, not on self: with two chats running concurrently, a shared
        # flag meant one turn's alert silenced the other's, or doubled it.
        cost_alerted = {"fired": False}
        message_id = None

        if self.config.stream_enabled:
            self.events.listen(session, progress.on_event)

        submitted_at = int(time.time() * 1000)
        delivery = self.state.pref(chat_id, "delivery", self.config.prompt_delivery)
        try:
            if watch:
                # Guarded: a transport error here used to abort the turn before
                # the prompt was ever submitted, silently losing the message.
                try:
                    message_id = self.telegram.send(chat_id, progress.render())
                except (TelegramError, OSError) as exc:
                    log.warning(
                        "Could not post the live placeholder: %s", self.telegram.redact(exc)
                    )
                    message_id = None

            # Remember the in-flight turn so a bridge restart can report it
            # instead of silently orphaning the work.
            self.state.set_busy(chat_id, session=session, started=submitted_at)

            try:
                self.api.send_prompt(session, text, files, delivery=delivery)
            except ApiError as exc:
                if exc.status != 404:
                    raise
                old_session = session
                session = self._recover_session(chat_id, exc)
                submitted_at = int(time.time() * 1000)
                self.state.set_busy(chat_id, session=session, started=submitted_at)
                if self.config.stream_enabled:
                    # Unlisten the *old* id: after reassignment the new id was
                    # never registered, so the dead listener leaked forever.
                    self.events.unlisten(old_session)
                    self.events.listen(session, progress.on_event)
                self.api.send_prompt(session, text, files, delivery=delivery)

            message_id = self._pump(
                chat_id, session, progress, message_id, submitted_at, watch, cost_alerted
            )
        finally:
            if self.config.stream_enabled:
                self.events.unlisten(session)
            self.state.clear_busy(chat_id)
            self.clear_abort(chat_id)

        final = self._final_text(progress, session, submitted_at)
        markup = keyboards.session_buttons(session)

        self._deliver_final(chat_id, message_id, final, markup, watch)

        self._check_cost_alert(chat_id, progress, cost_alerted)

    def _pump(
        self,
        chat_id: str,
        session: str,
        progress: TurnProgress,
        message_id: int | None,
        submitted_at: int,
        watch: bool,
        cost_alerted: dict,
    ) -> int | None:
        """Poll until the turn ends, editing the live message as it goes.

        The hard deadline only fires for genuinely wedged turns. A long but
        healthy job gets a heads-up at HEADS_UP_AFTER and then keeps waiting,
        because abandoning a turn that is still running is what makes a chat
        look dead.
        """
        deadline = time.time() + self.config.prompt_timeout
        heads_up = self.config.heads_up_after
        warned = False
        failures = 0
        last_render = 0.0
        last_messages: list[dict] = []

        while time.time() < deadline:
            if self.abort_flag(chat_id).is_set():
                progress.add_note("interrupted")
                progress.status = "done"
                progress.finished_at = time.time()
                try:
                    self.api.interrupt_session(session)
                except ApiError:
                    pass
                break

            if not warned and progress.elapsed >= heads_up:
                warned = True
                progress.add_note(f"still working after {progress.elapsed / 60:.0f}m")
                log.info("Long turn in %s after %ds", session, int(progress.elapsed))
                last_render = 0.0  # force a refresh so the note is visible

            try:
                messages = self.api.messages(session, limit=30, order="desc")
                failures = 0
            except ApiError as exc:
                # A dead server used to mean silence for the whole timeout -
                # up to two hours of nothing, one log line every poll. Count
                # consecutive transport failures and bail out with an
                # explanation instead.
                failures += 1
                if failures == 1 or failures % 15 == 0:
                    log.warning("Message poll failed (%d in a row): %s", failures, exc)
                if failures >= MAX_TRANSPORT_FAILURES:
                    progress.add_note("lost contact with the OpenCode server")
                    progress.status = "failed"
                    progress.error = "the server became unreachable"
                    progress.finished_at = time.time()
                    self.health.errors += 1
                    log.error("Abandoning turn after %d transport failures", failures)
                    break
                messages = []

            progress.absorb_messages(messages, since=submitted_at)
            last_messages = messages

            polled = OpenCode.assistant_text(messages, submitted_at)
            if polled:
                # The event stream is the fast path for text. If it is down or
                # dropped events, fall back to what polling already has.
                # Adopting the polled text only when it is *longer* keeps a
                # healthy stream authoritative while still letting a turn
                # whose stream died partway through be repaired - and doing
                # it under the progress lock stops the adoption from
                # interleaving with a delta append mid-write.
                progress.set_if_longer(polled)

            finished = any(
                m.get("type") == "idle"
                and (m.get("time") or {}).get("created", 0) > submitted_at
                for m in messages
            )

            # Cost alerts fire while the turn is still spending, not only at
            # the end: by then the money is already gone.
            if not cost_alerted["fired"] and progress.cost:
                threshold = float(
                    self.state.pref(chat_id, "cost_alert", self.config.cost_alert) or 0
                )
                if threshold and progress.cost >= threshold:
                    cost_alerted["fired"] = True
                    self.send(
                        chat_id,
                        f"Cost alert: ${progress.cost:.4f} so far, past your "
                        f"${threshold:.2f} threshold. /abort if you want it stopped.",
                    )

            if (
                watch
                and message_id
                and time.time() - last_render >= self.telegram.effective_interval()
            ):
                last_render = time.time()
                try:
                    self.telegram.edit(chat_id, message_id, progress.render())
                except (TelegramError, OSError):
                    # Cosmetic only: never let a progress tick break the turn.
                    log.debug("Live edit failed", exc_info=True)

            if finished:
                if not progress.finished_at:
                    progress.status = "done"
                    progress.finished_at = time.time()
                break

            time.sleep(self.config.poll_messages)
        else:
            # The turn may still be alive on the server; say so rather than
            # pretending it ended.
            progress.add_note("gave up waiting; the job may still be running")
            progress.status = "done"
            progress.finished_at = time.time()

        if last_messages:
            final_polled = OpenCode.assistant_text(last_messages, submitted_at)
            if final_polled:
                progress.set_if_longer(final_polled)
        return message_id

    def _deliver_final(
        self,
        chat_id: str,
        message_id: int | None,
        text: str,
        markup: dict | None,
        watch: bool,
    ) -> None:
        """Send the finished reply, and never let it be swallowed.

        Editing the live message is preferred, but Telegram rate-limits edits
        and refuses edits of old messages. Both cases used to drop the answer on
        the floor, which from the chat looks exactly like the bot stopped
        mid-job. If the edit does not land, send a fresh message instead.
        """
        if watch and message_id:
            if self.telegram.edit(chat_id, message_id, text, reply_markup=markup, force=True):
                return
            log.warning("Final edit failed (rate limit or stale message); sending instead")
        self.send(chat_id, text, markup)

    def _recover_session(self, chat_id: str, exc: ApiError) -> str:
        """Recreate a session the server no longer has, and say so.

        Reached when the OpenCode server restarted mid-conversation and lost
        the session: the prompt came back 404 instead of being queued.
        """
        log.warning("Session for chat %s is gone (%s); recreating", chat_id, exc.message)
        directory = self.state.directory(chat_id)
        created = self.api.create_session(directory)
        self.state.set_session(chat_id, created["id"], directory=directory)
        self.send(
            chat_id,
            "The OpenCode server restarted and dropped this session, so I started a "
            f"fresh one ({created['id']}) and resent your message. Earlier context "
            "in this chat is gone; /sessions lists anything the server kept.",
        )
        return created["id"]

    def _final_text(self, progress: TurnProgress, session: str, submitted_at: int) -> str:
        text = progress.final_text(self.config.max_output)
        # Test the agent's text, not the rendered block: an answer that happens
        # to contain the words "(no reply)" was being thrown away.
        if progress.text.strip() or "(no reply)" not in text:
            return text
        try:
            messages = self.api.messages(session, limit=10, order="desc")
        except ApiError:
            return text
        fallback = OpenCode.assistant_text(messages, submitted_at)
        if not fallback:
            return text
        # Re-render rather than return the bare fallback, so notes like
        # "interrupted" and the duration footer survive.
        progress.set_if_longer(fallback.strip())
        return progress.final_text(self.config.max_output)

    def _check_cost_alert(
        self, chat_id: str, progress: TurnProgress, cost_alerted: dict
    ) -> None:
        """Catch a turn that finished fast but still crossed the threshold."""
        threshold = float(self.state.pref(chat_id, "cost_alert", self.config.cost_alert) or 0)
        if threshold and progress.cost >= threshold and not cost_alerted["fired"]:
            cost_alerted["fired"] = True
            self.send(
                chat_id,
                f"Cost alert: this session has reached ${progress.cost:.4f} "
                f"(threshold ${threshold:.2f}).",
            )

    # ------------------------------------------------------- permission loop

    def _schedule_loop(self) -> None:
        """Submit scheduled prompts through the normal queue.

        Deliberately simple: tick once a minute and hand anything due to the
        same queue a message would use, so scheduled work streams, can be
        aborted and asks for permissions exactly like everything else.
        """
        while not self.stop_event.wait(SCHEDULE_TICK):
            try:
                for schedule in self.schedules.due():
                    log.info("Schedule %s due for chat %s", schedule.id, schedule.chat_id)
                    if schedule.chat_id not in self.config.allowed_chats:
                        continue
                    self.work.put((schedule.chat_id, schedule.prompt, None))
                self.state.save_schedules(self.schedules.to_list())
            except Exception:
                log.exception("Scheduler tick failed")

    def _permission_loop(self) -> None:
        """Watch for approval requests.

        The whole body is guarded: this thread also runs attachment pruning,
        so an unguarded exception here would silently take auto-approval,
        prompts and disk housekeeping down with it, with no symptom other
        than the features quietly going missing.
        """
        seen: list[str] = []
        last_prune = 0.0
        while not self.stop_event.wait(self.config.permission_poll):
            try:
                self.health.permissions_alive = True

                now = time.monotonic()
                if now - last_prune > 3600:
                    last_prune = now
                    try:
                        attachments.prune(
                            self.config.attachments_dir,
                            max_age_hours=self.config.attachment_max_age_hours,
                        )
                    except Exception:
                        log.debug("Attachment prune failed", exc_info=True)

                self._prune_seen(seen)

                for chat_id in list(self.state.chats()):
                    if chat_id not in self.config.allowed_chats:
                        # Polling a removed chat's directory would surface its
                        # pending requests - and possibly auto-answer them -
                        # for a conversation that is no longer allowed.
                        continue
                    directory = self.state.directory(chat_id)
                    try:
                        requests = self.api.permission_requests(directory=directory)
                    except ApiError:
                        continue
                    except Exception:
                        # A malformed response must not stop the watcher.
                        log.exception("Permission poll raised for %s", chat_id)
                        continue

                    for request in requests or []:
                        request_id = request.get("id") or ""
                        if not request_id or request_id in seen:
                            continue

                        self._requests[request_id] = request

                        decision = self.policy.decide(request, chat_id)
                        if decision:
                            self._reply_permission(chat_id, request_id, decision, request)
                            if request_id not in seen:
                                seen.append(request_id)
                            continue

                        # Only mark as seen once the user has actually been
                        # told; marking first meant a failed send left the
                        # agent blocked on a request nobody was ever shown.
                        if self._notify_permission(chat_id, request_id, request):
                            seen.append(request_id)
                            del seen[:-MAX_SEEN_PERMISSIONS]
            except Exception:
                log.exception("Permission watcher iteration failed")

    def _prune_seen(self, seen: list[str]) -> None:
        """Bound the dedupe list and the request cache."""
        if len(seen) > MAX_SEEN_PERMISSIONS:
            del seen[:-MAX_SEEN_PERMISSIONS]
        # Requests answered outside the bridge are never evicted by
        # _reply_permission, so bound the cache independently.
        if len(self._requests) > MAX_CACHED_REQUESTS:
            for request_id in list(self._requests)[: len(self._requests) // 2]:
                self._requests.pop(request_id, None)
        # Card mappings whose request resolved elsewhere (an auto-decision,
        # a server-side answer) must not keep a dead id on a live message;
        # the dict is small, so reconciling it here is cheap.
        for key in list(self._perm_cards):
            if self._perm_cards[key] not in self._requests:
                self._perm_cards.pop(key, None)

    def _reply_permission(
        self,
        chat_id: str,
        request_id: str,
        decision: str,
        request: dict | None = None,
    ) -> None:
        request = request or self._find_request(request_id)
        if not request:
            # The button still exists in the chat even though the request is
            # gone; silence here looks like a successful approval.
            self.send(chat_id, f"! {request_id} is no longer pending.")
            return
        try:
            self.api.reply_permission(request.get("sessionID") or "", request_id, decision)
        except ApiError as exc:
            log.warning("Could not answer permission %s: %s", request_id, exc)
            # Silence here reads as a successful approval: the user reacted,
            # the card stayed, and the agent stayed blocked while we said
            # nothing. The request is still pending, so say so.
            self.send(chat_id, f"! Could not {decision} {request_id}: {exc.message}")
            return

        self._requests.pop(request_id, None)
        for key in [k for k, v in self._perm_cards.items() if v == request_id]:
            self._perm_cards.pop(key, None)
        verb = {"once": "approved", "always": "always allowed", "reject": "rejected"}[decision]
        auto = " (auto)" if request.get("_auto") else ""
        self.send(chat_id, f"Permission {verb}{auto}: {request.get('action', '?')}")

    def _find_request(self, request_id: str) -> dict | None:
        """Look up a pending request from the watcher's cache.

        Button presses arrive with only the request id, and resolving it used
        to mean an API call per allow-listed chat until one matched. The
        watcher already polls these, so it caches them and this becomes a
        dict lookup - which also means answering works even if the server has
        gone away.
        """
        return self._requests.get(request_id)

    def _notify_permission(self, chat_id: str, request_id: str, request: dict) -> bool:
        """Ask the user. Returns False if the ask did not get through."""
        action = request.get("action", "?")
        resources = ", ".join(request.get("resources") or [])[:200]
        body = f"Permission needed: {action}\n{resources}\n\n{request_id}"
        try:
            message_id = self.telegram.send(
                chat_id, body, reply_markup=keyboards.permission_buttons(request_id)
            )
        except (TelegramError, OSError) as exc:
            log.warning(
                "Could not ask about permission %s: %s", request_id, self.telegram.redact(exc)
            )
            return False
        if message_id is None:
            # Delivered, but Telegram did not identify the message: the card
            # simply cannot be scoped by reaction. The ask still went
            # through - retrying would post a duplicate card.
            log.warning(
                "Permission %s card has no message id; reactions degrade to chat-wide",
                request_id,
            )
            return True
        self._perm_cards[(chat_id, message_id)] = request_id
        log.info("Asked chat %s about permission %s (%s)", chat_id, request_id, message_id)
        return True


def main() -> int:
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    # The journal is volatile on some machines, so keep a rotating copy that
    # survives a reboot. Without it, diagnosing a problem that happened before
    # a restart means guessing.
    try:
        probe = Config.from_env()
        log_path = Path(probe.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=probe.log_max_bytes,
                backupCount=probe.log_backups,
                encoding="utf-8",
            )
        )
        # Log lines can contain fragments of what you sent, so keep this as
        # private as the .env it sits next to.
        log_path.chmod(0o600)
    except Exception:  # nosec
        # Logging setup must never stop the daemon (a read-only disk or a
        # bad log path is not worth dying over); basicConfig below still
        # writes to stdout, so the journal keeps its own copy.
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if config.unrestricted:
        log.warning(
            "ALLOWED_ROOTS is empty: sessions may target any directory on this "
            "machine. Set ALLOWED_ROOTS to confine the bridge."
        )

    bridge = Bridge(config)

    def shutdown(signum: int, _frame: Any) -> None:
        """Exit promptly on SIGTERM.

        The main thread is normally parked in a long poll that can block for
        the best part of a minute, and waiting for it to notice the stop flag
        is what previously made systemd escalate to SIGKILL and lose the reply.
        Everything that matters is already on disk - state writes are atomic
        and the in-flight turn is recorded - so flushing and leaving is both
        safe and immediate.
        """
        log.info("Signal %s received; shutting down", signum)
        bridge.stop_event.set()
        bridge.events.stop()
        try:
            bridge.state.save()
        except Exception:
            log.exception("Could not persist state during shutdown")
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        bridge.start()
    except ApiError as exc:
        log.error("Could not start: %s", exc.message)
        return 1

    try:
        bridge.poll_loop()
        if bridge.fatal:
            return 1
    finally:
        bridge.stop_event.set()
        bridge.events.stop()
        bridge.state.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
