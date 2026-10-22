"""Inbound attachment handling.

Telegram delivers pictures, documents, and voice notes as file ids that must be
fetched separately. This module turns any of them into a local file plus the
``file://`` URI that the prompt endpoint accepts, and writes a short caption
that goes along with it so the agent knows what it is looking at.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("bridge.attachments")

MAX_BYTES = 20 * 1024 * 1024
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Attachment:
    path: Path
    uri: str
    name: str
    kind: str
    caption: str

    def as_payload(self) -> dict:
        return {"uri": self.uri, "name": self.name, "description": self.kind}


def safe_name(raw: str | None, fallback: str) -> str:
    """Strip anything path-like so a download can never escape the cache."""
    name = SAFE_NAME.sub("_", (raw or "").strip())[:80].strip("_.")
    return name or fallback


def extract(message: dict) -> tuple[str, str, str] | None:
    """Pull (file_id, suggested_name, kind) out of a Telegram message.

    Returns None when the message carries no file.
    """
    photos = message.get("photo") or []
    if photos:
        largest = max(photos, key=lambda p: p.get("file_size") or 0)
        return largest.get("file_id"), safe_name(None, "photo.jpg"), "photo"

    for field, kind, fallback in (
        ("document", "document", "file.bin"),
        ("video", "video", "video.mp4"),
        ("audio", "audio", "audio.bin"),
        ("voice", "voice", "voice.ogg"),
    ):
        payload = message.get(field)
        if not payload:
            continue
        file_id = payload.get("file_id")
        if not file_id:
            continue
        name = safe_name(payload.get("file_name"), fallback)
        if kind == "voice":
            name = f"voice-{int(time.time())}.ogg"
        return file_id, name, kind

    sticker = message.get("sticker")
    if sticker:
        return sticker.get("file_id"), safe_name(None, "sticker.webp"), "image"

    return None


def store(
    telegram, file_id: str, name: str, kind: str, cache: Path, caption: str = ""
) -> Attachment:
    """Download an attachment into the cache and describe it for the agent."""
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / f"{int(time.time())}-{name}"
    telegram.download(file_id, destination)

    size = destination.stat().st_size
    if size > MAX_BYTES:
        destination.unlink(missing_ok=True)
        raise ValueError(
            f"{name} is {size // 1048576} MB, over the {MAX_BYTES // 1048576} MB limit"
        )

    if not caption:
        caption = describe(kind, destination)

    attachment = Attachment(
        path=destination,
        uri=destination.as_uri(),
        name=name,
        kind=kind,
        caption=caption,
    )
    log.info("Stored %s attachment %s (%d bytes)", kind, destination.name, size)
    return attachment


def prune(cache: Path, max_age_hours: float = 24.0, keep: int = 50) -> int:
    """Delete cached files that are old or beyond the newest *keep*.

    The agent reads these once, during the turn they arrive in, so anything
    older than a day is dead weight. Without this the directory grows without
    bound on a bridge that receives a steady stream of screenshots.

    Returns the number of files removed.
    """
    if not cache.exists():
        return 0

    try:
        files = sorted(
            (f for f in cache.iterdir() if f.is_file()),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return 0

    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for index, path in enumerate(files):
        try:
            expired = path.stat().st_mtime < cutoff
        except OSError:
            continue
        if not expired and index < keep:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue

    if removed:
        log.info("Pruned %d cached attachment(s)", removed)
    return removed


def describe(kind: str, path: Path) -> str:
    """A short human-readable hint so the agent knows what it received."""
    size = path.stat().st_size if path.exists() else 0
    readable = f"{size // 1024} KB" if size < 1048576 else f"{size // 1048576} MB"
    if kind == "photo":
        return f"Photo sent from Telegram ({readable})."
    if kind == "voice":
        return f"Voice note from Telegram ({readable})."
    if kind == "document":
        return f"Document '{path.name}' from Telegram ({readable})."
    return f"{kind.title()} from Telegram ({readable})."
