"""Per-chat session state.

Each allow-listed chat gets its own active session and working directory, so
two conversations never collide. State survives restarts because the bridge
resumes from this file on boot.

Writes are atomic (temp file + rename) so a crash mid-write cannot leave a
truncated file that would strand the bridge on a fresh session.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

VERSION = 2

log = logging.getLogger("bridge.state")


@dataclass
class ChatState:
    session: str | None = None
    directory: str = ""
    busy: dict | None = None
    updated: float = field(default_factory=time.time)
    prefs: dict = field(default_factory=dict)


class State:
    def __init__(self, path: Path, default_dir: str) -> None:
        self.path = Path(path)
        self.default_dir = default_dir
        self._chats: dict[str, ChatState] = {}
        # Global, not per chat: the approval mode and the guard's deny-list.
        self.policy: dict = {}
        self.schedules: list = []
        # Standing prompts, global to the bot; /macro records them and
        # /<name> runs one.
        self.macros: dict[str, str] = {}
        self._dirty = False
        # Main thread, turn threads and the watcher all save concurrently.
        # RLock: mutators acquire the lock, mutate, then call save() which
        # acquires it again - and the same thread runs the signal handlers
        # that persist-on-shutdown while a save may already be in progress.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ load

    def load(self) -> None:
        self._chats = {}
        data = self._read()
        self._harden_existing()
        if data:
            self._load_v2(data)
        else:
            self._migrate_legacy()
        if self._dirty:
            self.save()

    def _read(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        # A hand-edited or corrupted file could hold any JSON value; treat
        # non-objects as missing so load() falls back to legacy migration
        # instead of raising on every subsequent boot.
        return data if isinstance(data, dict) else None

    def load_schedules(self) -> list:
        return list(self.schedules)

    def save_schedules(self, items: list) -> None:
        with self._lock:
            self.schedules = list(items)
            self._dirty = True
            self.save()

    def load_offset(self) -> int:
        """Last acknowledged Telegram update, so a restart does not replay it."""
        with self._lock:
            try:
                return int(self.policy.get("update_offset") or 0)
            except (TypeError, ValueError):
                return 0

    def save_offset(self, offset: int) -> None:
        """Recorded on every batch; writes are cheap and atomic."""
        with self._lock:
            if offset == self.policy.get("update_offset"):
                return
            self.policy["update_offset"] = int(offset)
            self._dirty = True
            self.save()

    def load_policy(self) -> dict:
        with self._lock:
            return dict(self.policy)

    def save_policy(self, data: dict) -> None:
        """Adopt a new policy snapshot.

        The snapshot carries only mode/deny/rules, but this dict is also
        where the Telegram update offset lives. A plain replace would set
        the offset back to zero, so every retained update is replayed as a
        duplicate turn after the next restart; carry the offset forward.
        """
        if isinstance(data, dict):
            with self._lock:
                updated = dict(data)
                offset = self.policy.get("update_offset")
                if offset is not None and "update_offset" not in updated:
                    updated["update_offset"] = offset
                self.policy = updated
                self._dirty = True
                self.save()

    def save_macros(self, macros: dict[str, str]) -> None:
        self.macros = dict(macros)
        self._dirty = True
        self.save()

    def _load_v2(self, data: dict) -> None:
        policy = data.get("policy")
        self.policy = policy if isinstance(policy, dict) else {}
        schedules = data.get("schedules")
        self.schedules = schedules if isinstance(schedules, list) else []
        macros = data.get("macros")
        self.macros = dict(macros) if isinstance(macros, dict) else {}
        chats = data.get("chats")
        for chat_id, entry in (chats if isinstance(chats, dict) else {}).items():
            if not isinstance(entry, dict):
                continue
            prefs = entry.get("prefs")
            busy = self._valid_busy(entry.get("busy"))
            if busy != entry.get("busy"):
                # Drop the corruption from the file too, not just from memory.
                self._dirty = True
            self._chats[str(chat_id)] = ChatState(
                session=entry.get("session"),
                directory=entry.get("directory") or self.default_dir,
                busy=busy,
                updated=entry.get("updated", 0.0),
                prefs=prefs if isinstance(prefs, dict) else {},
            )

    @staticmethod
    def _valid_busy(value) -> dict | None:
        """Normalize a persisted busy marker, or None when it is unusable.

        The docstring promise is that a corrupted file is survived: a hand
        edit or a partial write from an older version can leave a busy entry
        without a usable "session" (which orphan reporting indexes into) or
        a non-numeric "started" (which the age math subtracts). Either would
        raise inside Bridge.start() and restart-loop forever.
        """
        if not isinstance(value, dict):
            return None
        session = value.get("session")
        started = value.get("started", 0.0)
        if not (isinstance(session, str) and session.strip()):
            return None
        if not isinstance(started, (int, float)) or isinstance(started, bool):
            return None
        return {"session": session, "started": float(started)}

    def _migrate_legacy(self) -> None:
        """Adopt the old single-session .session.json, if present."""
        legacy = self.path.with_name(".session.json")
        if not legacy.exists():
            return
        try:
            data = json.loads(legacy.read_text())
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        session_id = data.get("sessionID")
        if session_id:
            self._chats["*"] = ChatState(session=session_id, directory=self.default_dir)
            self._dirty = True

    def take_legacy(self) -> ChatState | None:
        """Remove and return the placeholder imported from .session.json."""
        with self._lock:
            legacy = self._chats.pop("*", None)
            if legacy:
                self._dirty = True
                self.save()
            return legacy

    # ------------------------------------------------------------------ save

    def save(self) -> None:
        """Write state atomically, safe against concurrent writers.

        The temp name must be unique: with a shared name one thread could
        truncate the file while another renamed it into place, publishing an
        empty state file that wiped every session on the next boot.
        """
        with self._lock:
            chats = {
                chat_id: asdict(chat)
                for chat_id, chat in list(self._chats.items())
                if chat_id != "*"
            }
            payload = {
                "version": VERSION,
                # Policy is global rather than per chat, so it does not belong
                # in the chats map.
                "policy": self.policy,
                "schedules": self.schedules,
                "macros": self.macros,
                # Persist chats even without a session: /dir, /delivery and
                # /alert all take effect before the first turn.
                "chats": chats,
            }
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    dir=str(self.path.parent),
                    prefix=self.path.name + ".",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    tmp = Path(handle.name)
                    handle.write(json.dumps(payload, indent=2))
                    handle.flush()
                    os.fsync(handle.fileno())
                # Session ids and per-chat preferences: owner-only, like .env.
                tmp.chmod(0o600)
                os.replace(tmp, self.path)
                self._mirror_legacy(chats)
                self._dirty = False
            except OSError:
                # Losing persistence is survivable; losing the daemon is not.
                # But quietly swallowing it at debug level hid the fact the
                # disk was full, so escalate - this is worth a warning.
                log.warning("Could not persist state", exc_info=True)

    def _harden_existing(self) -> None:
        """Tighten permissions on a state file written by an older version."""
        try:
            if self.path.exists() and (self.path.stat().st_mode & 0o077):
                self.path.chmod(0o600)
        except OSError:
            pass

    def _mirror_legacy(self, chats: dict) -> None:
        """Keep .session.json current for anything else that reads it."""
        legacy = self.path.with_name(".session.json")
        with_session = {
            chat_id: chat for chat_id, chat in chats.items() if chat.get("session")
        }
        if not with_session:
            # Every session is gone (e.g. /del on the last one): a mirror
            # that still points at a deleted session is worse than none.
            try:
                legacy.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            return
        newest = max(with_session.values(), key=lambda c: c.get("updated") or 0)
        try:
            legacy.write_text(json.dumps({"sessionID": newest.get("session")}))
            legacy.chmod(0o600)  # same sensitivity as the sessions themselves
        except OSError:
            pass

    # ----------------------------------------------------------------- access

    def chat(self, chat_id: str) -> ChatState:
        with self._lock:
            if chat_id not in self._chats:
                self._chats[chat_id] = ChatState(directory=self.default_dir)
                self._dirty = True
            return self._chats[chat_id]

    def session(self, chat_id: str) -> str | None:
        return self.chat(chat_id).session

    def directory(self, chat_id: str) -> str:
        return self.chat(chat_id).directory or self.default_dir

    def set_session(
        self, chat_id: str, session_id: str | None, directory: str | None = None
    ) -> None:
        with self._lock:
            chat = self.chat(chat_id)
            chat.session = session_id
            if directory:
                chat.directory = directory
            chat.updated = time.time()
            self._dirty = True
            self.save()

    def set_directory(self, chat_id: str, directory: str) -> None:
        with self._lock:
            self.chat(chat_id).directory = directory
            self._dirty = True
            self.save()

    # ------------------------------------------------------------------ prefs

    def pref(self, chat_id: str, key: str, default=None):
        return self.chat(chat_id).prefs.get(key, default)

    def set_pref(self, chat_id: str, key: str, value) -> None:
        with self._lock:
            self.chat(chat_id).prefs[key] = value
            self._dirty = True
            self.save()

    def prefs(self, chat_id: str) -> dict:
        return dict(self.chat(chat_id).prefs)

    # ------------------------------------------------------------------- busy

    def set_busy(self, chat_id: str, session: str, started: float) -> None:
        """Record the turn currently running, so a restart can report it.

        This is deliberately persisted: a clean shutdown clears it, so anything
        found on disk at boot belongs to a turn that died with the process.
        """
        with self._lock:
            chat = self.chat(chat_id)
            chat.busy = {"session": session, "started": started}
            chat.updated = time.time()
            self._dirty = True
            self.save()

    def clear_busy(self, chat_id: str) -> None:
        with self._lock:
            chat = self.chat(chat_id)
            if chat.busy:
                chat.busy = None
                self._dirty = True
                self.save()

    def busy(self, chat_id: str) -> dict | None:
        """The in-flight turn, or None. Never persisted across a clean exit."""
        return self.chat(chat_id).busy or None

    def orphaned_turns(self, older_than: float = 0.0) -> list[tuple[str, dict]]:
        """Turns that were running when the bridge died."""
        with self._lock:
            states = list(self._chats.items())
        found = []
        for chat_id, chat in states:
            if chat_id == "*" or not chat.busy:
                continue
            if time.time() - (chat.busy.get("started") or 0) >= older_than:
                found.append((chat_id, chat.busy))
        return found

    def owner_of(self, session_id: str) -> str | None:
        """Find which chat a session belongs to, for proactive notifications."""
        with self._lock:
            states = list(self._chats.items())
        for chat_id, chat in states:
            if chat.session == session_id and chat_id != "*":
                return chat_id
        return None

    def chats(self) -> list[str]:
        with self._lock:
            return [c for c in self._chats if c != "*"]
