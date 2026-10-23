"""Local filesystem access.

The bridge is a systemd user service on the same machine as the OpenCode
server, so file commands talk to the filesystem directly instead of over HTTP.

That is not an optimisation, it is a correctness fix. The server's fs endpoints
ignore the directory hint and always resolve against their own working
directory (``/home/demo``), ``..`` is sanitised so nothing above it is
readable, and ``fs/find`` searches that whole tree with a fixed 50-result cap
and no scoping. Routing through it made ``/write`` land in the wrong directory
entirely and made ``/find`` useless outside the server root.

Every entry point resolves against a base directory and enforces
``allowed_roots``, which is the only place the confinement is actually done.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .pathguard import reason as sensitive_reason

MAX_READ_BYTES = 512 * 1024
MAX_ENTRIES = 500
MAX_WALK = 20_000


class PathError(RuntimeError):
    """A path is missing, escapes the scope, or is not a regular file."""


@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool
    size: int = 0


def _resolve(base: str, path: str, allowed_roots: tuple = ()) -> Path:
    """Resolve *path* against *base* and refuse anything out of scope."""
    raw = Path(path).expanduser()
    candidate = raw if raw.is_absolute() else Path(base) / raw
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise PathError(f"Could not resolve path: {path}") from exc

    if allowed_roots:
        for raw_root in allowed_roots:
            try:
                root = Path(raw_root).resolve()
            except (OSError, RuntimeError):
                root = Path(raw_root)
            if resolved == root or root in resolved.parents:
                break
        else:
            raise PathError(
                f"{resolved} is outside the allowed roots: "
                + ", ".join(str(r) for r in allowed_roots)
            )

    reason = sensitive_reason(resolved)
    if reason:
        raise PathError(
            f"Refusing to access {resolved}: {reason}. Use /sh if you need it deliberately."
        )
    return resolved


def list_dir(base: str, path: str = ".", allowed_roots: tuple = ()) -> list[Entry]:
    target = _resolve(base, path, allowed_roots)
    if not target.exists():
        raise PathError(f"No such directory: {target}")
    if not target.is_dir():
        raise PathError(f"Not a directory: {target}")

    entries: list[Entry] = []
    try:
        with os.scandir(target) as scan:
            for item in scan:
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                    size = 0 if is_dir else item.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                entries.append(Entry(name=item.name, is_dir=is_dir, size=size))
                if len(entries) >= MAX_ENTRIES:
                    break
    except PermissionError as exc:
        raise PathError(f"Permission denied: {target}") from exc
    except OSError as exc:
        raise PathError(str(exc)) from exc

    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def read_file(base: str, path: str, allowed_roots: tuple = ()) -> str:
    target = _resolve(base, path, allowed_roots)
    if not target.exists():
        raise PathError(f"No such file: {target}")
    if target.is_dir():
        raise PathError(f"Not a file: {target}")
    try:
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            raise PathError(
                f"{target.name} is {size // 1024} KB, over the "
                f"{MAX_READ_BYTES // 1024} KB read limit. Use /sh head or grep."
            )
        data = target.read_bytes()
    except PermissionError as exc:
        raise PathError(f"Permission denied: {target}") from exc
    except OSError as exc:
        raise PathError(str(exc)) from exc

    return data.decode("utf-8", errors="replace")


def write_file(base: str, path: str, content: str, allowed_roots: tuple = ()) -> Path:
    target = _resolve(base, path, allowed_roots)
    if target.is_dir():
        raise PathError(f"Not a file: {target}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except PermissionError as exc:
        raise PathError(f"Permission denied: {target}") from exc
    except OSError as exc:
        raise PathError(str(exc)) from exc
    return target


def find(
    base: str,
    query: str,
    allowed_roots: tuple = (),
    max_depth: int = 6,
    limit: int = 40,
) -> list[str]:
    """Search for files whose name contains *query*, scoped to *base*."""
    root = _resolve(base, ".", allowed_roots)
    needle = query.lower()
    results: list[str] = []
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        depth = len(current.relative_to(root).parts)
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            scanned += 1
            if needle in name.lower():
                results.append(str(current.relative_to(root) / name))
                if len(results) >= limit:
                    return sorted(results)
        if scanned > MAX_WALK:
            break

    return sorted(results)
