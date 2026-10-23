"""Paths the bridge refuses to expose through its file commands.

``ALLOWED_ROOTS`` answers "where may a session work?"  It does not answer
"which files are safe to read back into Telegram?".  A home directory that is
a perfectly reasonable root also contains SSH keys, the bridge's own token in
``.env``, shell startup files, and systemd units.  Those are not project files,
and a mistaken ``/read`` should not turn the chat into a credential dump.

This is defence in depth, not a sandbox.  ``/sh`` and the agent itself still run
as the service account; only an OS boundary (a dedicated user, container, or
the systemd hardening in ``docs/HARDENING.md``) can enforce that.
"""

from __future__ import annotations

from pathlib import Path

# Directories that hold credentials or control-plane files.  A path is refused
# when any component matches, so this also covers descendants.
SENSITIVE_DIRS = frozenset(
    {
        ".aws",
        ".azure",
        ".docker",
        ".git",
        ".gnupg",
        ".kube",
        ".ssh",
    }
)

# Exact basenames.  These are credentials, shell startup files, or bridge
# runtime files.  ``.env.example`` is deliberately allowed below.
SENSITIVE_NAMES = frozenset(
    {
        ".bash_history",
        ".bash_login",
        ".bash_profile",
        ".bashrc",
        ".bridge-heartbeat",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pgpass",
        ".profile",
        ".pypirc",
        ".session.json",
        ".state.json",
        ".zprofile",
        ".zshenv",
        ".zsh_history",
        ".zshrc",
        "authorized_keys",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)

# Two-component directories that are sensitive wherever they appear.  The
# checks are on adjacent resolved path components, not substrings.
SENSITIVE_SEQUENCES = (
    (".config", "opencode"),
    (".config", "systemd"),
)


def reason(path: Path) -> str | None:
    """Return why *path* is sensitive, or ``None`` when it is eligible.

    The path should already be resolved (symlinks followed), otherwise a
    harmless-looking link could point at ``.ssh/id_ed25519`` and evade the
    check.
    """
    name = path.name

    # The checked-in template is documentation, not a secret.  Allow only the
    # exact example; ``.env.local`` and friends remain blocked.
    if name == ".env.example":
        return None
    if name == ".env" or name.startswith(".env."):
        return "environment file"
    if name == "bridge.log" or name.startswith("bridge.log."):
        return "bridge log"

    parts = path.parts
    for directory in SENSITIVE_DIRS:
        if directory in parts:
            return f"{directory} directory"
    for left, right in SENSITIVE_SEQUENCES:
        for index in range(len(parts) - 1):
            if parts[index] == left and parts[index + 1] == right:
                return f"{left}/{right} directory"
    if name in SENSITIVE_NAMES:
        return "credential or runtime file"
    return None
