"""Environment loading and validation.

Security model
--------------
Two independent gates protect the machine:

1. ``TELEGRAM_CHAT_ID`` allow-list. Senders outside it are ignored silently.
   Without this, anyone holding the bot token gets code execution.
2. ``ALLOWED_ROOTS``. Optional list of directories sessions may target. Empty
   means unrestricted, which mirrors running ``opencode <directory>`` in a
   terminal by the same user. Set it to confine the bridge.

The bot token and the server password are read from the environment and are
never written to the state file or the log.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:49374"
MAX_CHUNK = 4096


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _int_env(source: dict, name: str, default: int) -> int:
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _positive_float(source: dict, name: str, default: float) -> float:
    """Reject zero and negative intervals.

    An interval of 0 turns a poll loop into a busy loop that hammers the API
    at full speed, so refuse it at startup rather than discover it later.
    """
    value = _float_env(source, name, default)
    if value <= 0:
        raise ConfigError(f"{name} must be greater than 0, got {value}")
    return value


def _float_env(source: dict, name: str, default: float) -> float:
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _path_env(source: dict, name: str, default: Path) -> Path:
    raw = source.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


def _bool_env(source: dict, name: str, default: bool) -> bool:
    raw = source.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _strip_inline_comments(source: dict) -> dict:
    """Drop trailing ``# ...`` comments the way any .env file expects.

    Someone copying a documented line out of .env.example will often uncomment
    it *and* keep the explanation beside it. Without this, ``PROMPT_TIMEOUT``
    arrives as ``7200  # hard cap`` and the bridge refuses to start with a
    message about an integer.

    Only a ``#`` preceded by whitespace counts, so a password containing a
    hash - the common case for generated secrets - survives intact.
    """
    cleaned = {}
    for key, value in source.items():
        if isinstance(value, str) and not value.strip().startswith("#"):
            match = re.search(r"\s#", value)
            if match:
                value = value[: match.start()]
        cleaned[key] = value
    return cleaned


@dataclass(frozen=True)
class Config:
    token: str
    allowed_chats: frozenset[str]
    opencode_url: str
    password: str
    default_dir: str
    state_file: Path
    permission_poll: float = 5.0
    allowed_roots: tuple[Path, ...] = ()
    max_output: int = 3500
    max_chunk: int = MAX_CHUNK

    # Live streaming of the agent's turn.
    stream_enabled: bool = True
    edit_interval: float = 2.0
    poll_messages: float = 2.0

    # Long-running turns. A hard job can take 20+ minutes, so the hard cap is
    # generous and the heads-up arrives early instead of at the deadline.
    prompt_timeout: int = 7200
    heads_up_after: int = 240

    # How a message sent while a turn is running is handled. "queue" waits its
    # turn; "steer" feeds the running turn and cancels it.
    prompt_delivery: str = "queue"

    # Permission handling.
    approval_mode: str = "ask"
    auto_deny: bool = True

    # Cached inbound attachments are pruned after this long.
    attachment_max_age_hours: float = 24.0

    # Cost guard rails.
    cost_alert: float = 0.0

    # Characters of a user's message written to the journal. 0 logs none.
    log_prompt_chars: int = 60

    # A rotating log file alongside the journal, because the journal on this
    # machine is volatile and loses everything on reboot.
    log_file: str = ""
    log_max_bytes: int = 2 * 1024 * 1024
    log_backups: int = 3

    # Optional command that transcribes a voice note; {file} is substituted.
    stt_command: str = ""

    @property
    def attachments_dir(self) -> Path:
        return self.state_file.parent / ".attachments"

    @property
    def unrestricted(self) -> bool:
        """True when sessions may target any directory on the machine."""
        return not self.allowed_roots

    def resolve_dir(self, directory: str | None) -> str:
        """Validate and absolutise a target directory.

        Raises ConfigError when the path is missing or escapes ALLOWED_ROOTS.
        """
        target = Path(directory or self.default_dir).expanduser()
        try:
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ConfigError(f"No such directory: {target}") from exc
        if not resolved.is_dir():
            raise ConfigError(f"Not a directory: {resolved}")

        if self.allowed_roots:
            for root in self.allowed_roots:
                if resolved == root or root in resolved.parents:
                    return str(resolved)
            raise ConfigError(
                f"{resolved} is outside the allowed roots: "
                + ", ".join(str(root) for root in self.allowed_roots)
            )
        return str(resolved)

    def check_path(self, directory: str, path: str) -> str:
        """Resolve a user-supplied file path and keep it inside the scope.

        The OpenCode server does **not** confine file operations to the
        session directory - ``/write ../../../etc/foo`` lands outside it, which
        was verified against the live API. So when ALLOWED_ROOTS is set the
        client has to do the containing, or the flag is decoration. With
        ALLOWED_ROOTS unset the bridge is unrestricted by design and nothing is
        blocked.
        """
        raw = Path(path).expanduser()
        candidate = raw if raw.is_absolute() else Path(directory) / raw
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise ConfigError(f"Could not resolve path: {path}") from exc

        if not self.allowed_roots:
            return str(resolved)

        for raw_root in self.allowed_roots:
            # Resolve the roots too: comparing a resolved candidate against an
            # unresolved root refuses legitimate paths whenever the root path
            # involves a symlink, which is easy to hit by constructing Config
            # directly rather than through from_env.
            try:
                root = Path(raw_root).resolve()
            except (OSError, RuntimeError):
                root = Path(raw_root)
            if resolved == root or root in resolved.parents:
                return str(resolved)
        raise ConfigError(
            f"{resolved} is outside the allowed roots: "
            + ", ".join(str(root) for root in self.allowed_roots)
        )

    @classmethod
    def from_env(cls, env: dict | None = None) -> Config:
        source = _strip_inline_comments(dict(os.environ if env is None else env))

        token = source.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is not set")

        raw_ids = source.get("TELEGRAM_CHAT_ID", "").strip()
        if not raw_ids:
            raise ConfigError(
                "TELEGRAM_CHAT_ID is not set. This bridge refuses to start "
                "without an allow-list: otherwise anyone who finds the bot "
                "token could drive this machine."
            )
        allowed = frozenset(part.strip() for part in raw_ids.split(",") if part.strip())
        if not allowed:
            raise ConfigError("TELEGRAM_CHAT_ID did not contain any usable id")

        password = source.get("OPENCODE_PASSWORD", "").strip()
        if not password:
            service = Path.home() / ".config/opencode/service.json"
            if not service.exists():
                raise ConfigError(
                    "OPENCODE_PASSWORD is not set and no service.json was found. "
                    'Run `opencode service set password "<secret>"` first.'
                )
            try:
                password = json.loads(service.read_text()).get("password", "")
            except (json.JSONDecodeError, OSError) as exc:
                raise ConfigError(f"Could not read {service}: {exc}") from exc
            if not password:
                raise ConfigError("service.json exists but has no password")

        default_dir = Path(source.get("PROJECT_DIR", "").strip() or Path.cwd()).expanduser()
        if not default_dir.is_dir():
            raise ConfigError(f"PROJECT_DIR is not a directory: {default_dir}")

        roots = tuple(
            Path(entry.strip()).expanduser().resolve()
            for entry in (source.get("ALLOWED_ROOTS", "") or "").split(",")
            if entry.strip()
        )
        # Fail at startup rather than on every message: a PROJECT_DIR outside
        # the allow-list makes ensure_session raise forever.
        resolved_default = default_dir.resolve()
        if roots and not any(
            resolved_default == root or root in resolved_default.parents for root in roots
        ):
            raise ConfigError(
                f"PROJECT_DIR ({resolved_default}) is outside ALLOWED_ROOTS: "
                + ", ".join(str(r) for r in roots)
            )

        state_file = _path_env(
            source, "STATE_FILE", Path(__file__).resolve().parent.parent / ".state.json"
        )

        return cls(
            token=token,
            allowed_chats=allowed,
            opencode_url=source.get("OPENCODE_URL", "").strip().rstrip("/") or DEFAULT_URL,
            password=password,
            default_dir=str(default_dir.resolve()),
            state_file=state_file,
            prompt_timeout=_int_env(source, "PROMPT_TIMEOUT", 7200),
            heads_up_after=_int_env(source, "HEADS_UP_AFTER", 240),
            prompt_delivery=(source.get("PROMPT_DELIVERY", "").strip().lower() or "queue"),
            permission_poll=_positive_float(source, "PERMISSION_POLL_INTERVAL", 5.0),
            allowed_roots=roots,
            max_output=_int_env(source, "MAX_OUTPUT", 3500),
            # Telegram rejects anything above 4096 outright, and split_message
            # would build chunks it then refuses to send.
            max_chunk=min(_int_env(source, "MAX_CHUNK", MAX_CHUNK), MAX_CHUNK),
            stream_enabled=_bool_env(source, "STREAM_ENABLED", True),
            edit_interval=_positive_float(source, "EDIT_INTERVAL", 2.0),
            poll_messages=_positive_float(source, "POLL_MESSAGES", 2.0),
            approval_mode=(source.get("APPROVAL_MODE", "").strip().lower() or "ask"),
            auto_deny=_bool_env(source, "AUTO_DENY", True),
            attachment_max_age_hours=_float_env(source, "ATTACHMENT_MAX_AGE_HOURS", 24.0),
            cost_alert=_float_env(source, "COST_ALERT", 0.0),
            log_prompt_chars=_int_env(source, "LOG_PROMPT_CHARS", 60),
            log_file=source.get("LOG_FILE", "").strip()
            or str(Path(__file__).resolve().parent.parent / "bridge.log"),
            log_max_bytes=_int_env(source, "LOG_MAX_BYTES", 2 * 1024 * 1024),
            log_backups=_int_env(source, "LOG_BACKUPS", 3),
            stt_command=source.get("STT_COMMAND", "").strip(),
        )
