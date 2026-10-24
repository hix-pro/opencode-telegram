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

The implementation is split by concern: update ingress and attachments live in
``bridgekit/updates.py``, button callbacks in ``bridgekit/callbacks.py``, turn
execution in ``bridgekit/turns.py`` and the permission/schedule watchers in
``bridgekit/watchers.py``. ``Bridge`` keeps thin wrappers so callbacks and tests
can patch the historic method names.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from bridgekit import attachments, callbacks, commands, keyboards, turns, updates, watchers
from bridgekit.api import ApiError, OpenCode
from bridgekit.config import Config, ConfigError, env_file_warning
from bridgekit.events import EventStream
from bridgekit.policy import Policy
from bridgekit.progress import TurnProgress
from bridgekit.schedule import ScheduleStore
from bridgekit.state import State
from bridgekit.telegram import Telegram, TelegramError

log = logging.getLogger("bridge")


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
            config.token,
            max_chunk=config.max_chunk,
            edit_interval=config.edit_interval,
            secrets=(config.password,),
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

    # ---------------------------------------------------------- attachments

    # ----------------------------------------------------------- worker loop

    # ------------------------------------------------------- permission loop

    # The implementations live in bridgekit/updates.py, callbacks.py,
    # turns.py and watchers.py. These wrappers keep the historic Bridge
    # surface: callbacks, tests and subclasses can patch a method and the
    # other modules still dispatch through it.

    # --- updates ---
    def _handle_update(self, update: dict) -> None:
        updates.handle_update(self, update)

    def _handle_callback(self, query: dict) -> None:
        updates.handle_callback(self, query)

    def _handle_edited_message(self, chat_id: str, message: dict) -> None:
        updates.handle_edited_message(self, chat_id, message)

    def _handle_reaction(self, reaction: dict) -> None:
        updates.handle_reaction(self, reaction)

    def _approve_pending(self, chat_id: str) -> None:
        updates.approve_pending(self, chat_id)

    def _stop_turn(self, chat_id: str) -> None:
        updates.stop_turn(self, chat_id)

    def _handle_attachment(
        self, chat_id: str, message: dict, file_id: str, name: str, kind: str
    ) -> None:
        updates.handle_attachment(self, chat_id, message, file_id, name, kind)

    def _handle_voice(self, chat_id: str, file_id: str, name: str, caption: str) -> None:
        updates.handle_voice(self, chat_id, file_id, name, caption)

    def _transcribe(self, path) -> str | None:
        return updates.transcribe(self, path)

    # --- callbacks ---
    def _register_callbacks(self) -> None:
        callbacks.register_callbacks(self)

    def _run_command_text(self, chat_id: str, text: str) -> None:
        callbacks.run_command_text(self, chat_id, text)

    # --- turns ---
    def _worker_loop(self) -> None:
        turns.worker_loop(self)

    def _chat_busy(self, chat_id: str) -> bool:
        return turns.chat_busy(self, chat_id)

    def _turn_worker(self, chat_id: str, text: str, files: list | None) -> None:
        turns.turn_worker(self, chat_id, text, files)

    def _run_turn(self, chat_id: str, text: str, files: list | None = None) -> None:
        turns.run_turn(self, chat_id, text, files)

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
        return turns.pump(
            self, chat_id, session, progress, message_id, submitted_at, watch, cost_alerted
        )

    def _deliver_final(
        self, chat_id: str, message_id: int | None, text: str, markup: dict | None, watch: bool
    ) -> None:
        turns.deliver_final(self, chat_id, message_id, text, markup, watch)

    def _recover_session(self, chat_id: str, exc: ApiError) -> str:
        return turns.recover_session(self, chat_id, exc)

    def _final_text(self, progress: TurnProgress, session: str, submitted_at: int) -> str:
        return turns.final_text(self, progress, session, submitted_at)

    def _check_cost_alert(
        self, chat_id: str, progress: TurnProgress, cost_alerted: dict
    ) -> None:
        turns.check_cost_alert(self, chat_id, progress, cost_alerted)

    # --- watchers ---
    def _schedule_loop(self) -> None:
        watchers.schedule_loop(self)

    def _permission_loop(self) -> None:
        watchers.permission_loop(self)

    def _prune_seen(self, seen: list[str]) -> None:
        watchers.prune_seen(self, seen)

    def _reply_permission(
        self, chat_id: str, request_id: str, decision: str, request: dict | None = None
    ) -> None:
        watchers.reply_permission(self, chat_id, request_id, decision, request)

    def _find_request(self, request_id: str) -> dict | None:
        return watchers.find_request(self, request_id)

    def _notify_permission(self, chat_id: str, request_id: str, request: dict) -> bool:
        return watchers.notify_permission(self, chat_id, request_id, request)


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

    env_warning = env_file_warning(Path(__file__).resolve().parent / ".env")
    if env_warning:
        log.warning("%s", env_warning)

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
