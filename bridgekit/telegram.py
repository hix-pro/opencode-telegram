"""Telegram Bot API client.

Long polling only. Plain text everywhere: HTML would force escaping on every
snippet the agent emits, and the agent's own output is not under our control.

Edits are rate limited. Telegram allows roughly 30 edits per minute per chat
and answers the overflow with 429, so :class:`Telegram` keeps its own throttle
window rather than relying on the server to bounce us.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from pathlib import Path

import requests

log = logging.getLogger("bridge.telegram")

# Telegram's own limits.
MIN_EDIT_INTERVAL = 1.2
EDIT_BURST = 25

# How long throttle bookkeeping is kept per message.
EDIT_MEMORY_SECONDS = 600.0

# The update types the bridge consumes. message_reaction is not in
# Telegram's default set, so reactions would silently never arrive
# without asking for it here; edited_message carries the edit-a-sent-
# message re-prompt.
ALLOWED_UPDATES = (
    "message",
    "edited_message",
    "callback_query",
    "message_reaction",
)

# One command list per bot: autocomplete stops being offered past these.
MAX_COMMANDS = 100
MAX_COMMAND_DESC = 256


class TelegramError(RuntimeError):
    pass


def _safe_detail(resp) -> str:
    """Never let a malformed error body escape as a parse error."""
    try:
        return str(resp.json().get("description") or resp.text[:120])
    except (ValueError, AttributeError, KeyError):
        return str(getattr(resp, "text", ""))[:120]


def _safe_retry_after(resp, default: float = 2.0) -> float:
    """Seconds to wait. float, not int: a sub-second value must not truncate
    to zero and lose the backoff entirely."""
    try:
        return float(resp.json().get("parameters", {}).get("retry_after", default))
    except (ValueError, AttributeError, KeyError, TypeError):
        return float(default)


class Telegram:
    def __init__(
        self,
        token: str,
        max_chunk: int = 4096,
        timeout: int = 30,
        edit_interval: float = MIN_EDIT_INTERVAL,
    ) -> None:
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.max_chunk = max_chunk
        self.timeout = timeout
        self.edit_interval = edit_interval
        self._http = requests.Session()
        self._last_edit: dict[str, float] = {}
        # Health counters: a silent edit failure can lose a reply, so it is
        # counted rather than left to the debug log.
        self.edit_failures = 0
        self.rate_limited = 0
        self._penalty = 0.0
        self._penalty_until = 0.0

    # ------------------------------------------------------------------ reads

    def get_updates(
        self,
        offset: int,
        timeout: int = 30,
        allowed_updates: Iterable[str] = ALLOWED_UPDATES,
    ) -> list[dict]:
        params: dict = {"offset": offset, "timeout": timeout}
        if allowed_updates is not None:
            # Must be a JSON-serialized list in ONE parameter. Requests would
            # happily encode a tuple as repeated query keys, but the Bot API
            # server reads only the first pair, then tries to JSON-decode
            # "message" - which fails, resets nothing, and silently leaves
            # the default set (no reactions). The official server source is
            # unambiguous about this.
            params["allowed_updates"] = json.dumps(list(allowed_updates))
        resp = self._http.get(
            self.base + "/getUpdates",
            params=params,
            timeout=timeout + 15,
        )
        if resp.status_code == 409:
            raise TelegramError(
                "Conflict: another instance is polling this bot token. Stop the other process."
            )
        resp.raise_for_status()
        body = resp.json()
        result = body.get("result", [])
        return result if isinstance(result, list) else []

    # ----------------------------------------------------------------- writes

    def send(self, chat_id: str, text: str, reply_markup: dict | None = None) -> int | None:
        """Send text, splitting it across messages if Telegram would reject it."""
        last_id = None
        for chunk in split_message(text, self.max_chunk):
            last_id = self._send_chunk(chat_id, chunk, reply_markup)
        return last_id

    def _send_chunk(
        self, chat_id: str, text: str, reply_markup: dict | None = None
    ) -> int | None:
        for _attempt in range(4):
            payload: dict = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            resp = self._http.post(
                self.base + "/sendMessage", json=payload, timeout=self.timeout
            )
            if resp.status_code == 429:
                # _safe_retry_after, not a bare resp.json(): a malformed body
                # used to escape as ValueError and abort the whole turn
                # instead of simply waiting its turn.
                retry_after = _safe_retry_after(resp, default=5.0)
                log.warning("Rate limited; sleeping %ss", retry_after)
                time.sleep(retry_after)
                continue
            if resp.status_code >= 400:
                raise TelegramError(
                    f"sendMessage failed ({resp.status_code}): {resp.text[:200]}"
                )
            return (resp.json().get("result") or {}).get("message_id")
        raise TelegramError("Still rate limited after 4 attempts")

    def edit(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
        force: bool = False,
    ) -> bool:
        """Edit a message in place, respecting the edit rate limit.

        Returns False when the edit was skipped as too frequent or when the new
        text is identical, which are both normal and not worth logging loudly.
        """
        key = f"{chat_id}:{message_id}"
        now = time.monotonic()
        last = self._last_edit.get(key, 0.0)
        if not force and now - last < self.effective_interval():
            return False

        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        try:
            resp = self._http.post(
                self.base + "/editMessageText", json=payload, timeout=self.timeout
            )
        except OSError as exc:
            # A network error must not propagate: the caller treats an edit
            # failure as "send a fresh message instead", and an exception
            # would take the whole turn down with it. OSError rather than
            # requests.RequestException because the built-in ConnectionError
            # is not a subclass of it, and requests errors subclass IOError.
            self.edit_failures += 1
            log.warning("editMessageText transport error: %s", exc)
            return False
        finally:
            self._last_edit[key] = time.monotonic()
            self._evict_edit_times()

        if resp.status_code == 429:
            retry_after = _safe_retry_after(resp)
            self.rate_limited += 1
            self._penalty = max(self._penalty, float(retry_after))
            self._penalty_until = now + float(retry_after)
            log.info("Edit rate limited; waiting %ss before the next attempt", retry_after)
            return False
        if resp.status_code >= 400:
            detail = _safe_detail(resp)
            # "message is not modified" is benign - the text is already on
            # screen. Anything else matters, because the final reply may be
            # delivered by edit and a lost edit is a lost answer.
            if "not modified" not in detail:
                self.edit_failures += 1
                log.warning("editMessageText failed (%s): %s", resp.status_code, detail)
            return False
        return True

    def _evict_edit_times(self) -> None:
        """Drop throttle entries for messages nobody will edit again."""
        if len(self._last_edit) < 64:
            return
        cutoff = time.monotonic() - EDIT_MEMORY_SECONDS
        self._last_edit = {k: v for k, v in self._last_edit.items() if v >= cutoff}

    def effective_interval(self) -> float:
        """Current edit spacing, widened while the server is throttling us."""
        if not self._penalty_until:
            return self.edit_interval
        if time.monotonic() >= self._penalty_until:
            self._penalty_until = 0.0
            return self.edit_interval
        return max(self.edit_interval, self.edit_interval * 2)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        payload: dict = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        try:
            self._http.post(
                self.base + "/answerCallbackQuery", json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            log.debug("answerCallbackQuery failed: %s", exc)

    # ------------------------------------------------------- bot cosmetics

    def set_my_commands(self, commands: list[dict]) -> bool:
        """Publish the command list Telegram autocompletes from.

        Best effort: a failure is logged and the bridge runs on, because a
        missing autocomplete list is never worth crashing a start over.
        """
        entries = [
            {
                "command": str(entry["command"])[:32],
                "description": str(entry["description"])[:MAX_COMMAND_DESC],
            }
            for entry in commands[:MAX_COMMANDS]
            if entry.get("command")
        ]
        if not entries:
            return False
        return self._post_ok("setMyCommands", {"commands": entries})

    def set_menu_button(self, chat_id: str) -> bool:
        """Turn a chat's Menu button into the command keyboard."""
        return self._post_ok(
            "setChatMenuButton",
            {
                "chat_id": chat_id,
                "menu_button": {"type": "commands"},
            },
        )

    def _post_ok(self, method: str, payload: dict) -> bool:
        """A cosmetic POST: never raises, always tells whether it landed."""
        try:
            resp = self._http.post(
                self.base + "/" + method, json=payload, timeout=self.timeout
            )
        except (OSError, requests.exceptions.RequestException) as exc:
            # OSError rather than RequestException alone: the built-in
            # ConnectionError is not a subclass of it, and a network error
            # must not propagate into startup.
            log.warning("%s transport error: %s", method, exc)
            return False
        if resp.status_code >= 400:
            log.warning("%s failed (%s): %s", method, resp.status_code, _safe_detail(resp))
            return False
        return True

    # ------------------------------------------------------------------ files

    def download(self, file_id: str, destination: Path) -> Path:
        """Fetch a Telegram file by id and write it to *destination*."""
        resp = self._http.get(
            self.base + "/getFile", params={"file_id": file_id}, timeout=self.timeout
        )
        if resp.status_code >= 400:
            raise TelegramError(f"getFile failed ({resp.status_code}): {resp.text[:160]}")
        file_path = (resp.json().get("result") or {}).get("file_path")
        if not file_path:
            raise TelegramError("Telegram returned no file_path")

        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._http.get(
                f"{self.file_base}/{file_path}", stream=True, timeout=120
            ) as stream:
                if stream.status_code >= 400:
                    raise TelegramError(f"download failed ({stream.status_code})")
                with destination.open("wb") as handle:
                    for chunk in stream.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            handle.write(chunk)
        except Exception:
            # A half-written attachment is worse than none: the agent would
            # read a truncated file and never know it was incomplete.
            destination.unlink(missing_ok=True)
            raise
        return destination


def _utf16_len(text: str) -> int:
    """Length as Telegram counts it.

    The Bot API limits messages in UTF-16 code units, so an emoji costs two
    and a message of 4096 code points can be 8000 units and get rejected.
    """
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = 4096) -> list[str]:
    """Split text into chunks that fit Telegram's limit.

    Prefers paragraph, then line, then hard character boundaries so markdown
    fences and bullets survive the split, and measures in UTF-16 units so
    emoji-heavy replies are chunked before Telegram rejects them.
    """
    if _utf16_len(text) <= limit:
        return [text]

    # Reserve headroom only when surrogate pairs are actually present; plain
    # ASCII should still use the full budget.
    has_astral = any(ord(ch) > 0xFFFF for ch in text)
    safe = max(1, limit - 8 if has_astral else limit)

    chunks: list[str] = []
    buffer = ""

    def fits(candidate: str) -> bool:
        return _utf16_len(candidate) <= safe

    def flush() -> None:
        nonlocal buffer
        if buffer:
            chunks.append(buffer)
            buffer = ""

    for line in text.splitlines(keepends=True):
        # A single line may still be far too long on its own.
        while not fits(line):
            flush()
            take = len(line)
            while take > 0 and not fits(line[:take]):
                take -= 1
            if take == 0:  # one unit still too big; emit it and move on
                take = 1
            chunks.append(line[:take])
            line = line[take:]
        if not fits(buffer + line):
            flush()
        buffer += line
    flush()

    return [c for c in chunks if c] or [""]
