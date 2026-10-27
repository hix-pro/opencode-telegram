"""Credential-path denylist for local file commands.

The allow-list answers where a session may work; it does not make every file
under a home directory safe to read back into a chat. These tests pin the
small denylist that catches the bridge's own secrets, SSH keys, and control
files before an accidental /read becomes a disclosure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bridgekit import localfs
from bridgekit.config import Config, ConfigError
from bridgekit.localfs import PathError
from bridgekit.pathguard import reason


def test_common_secret_paths_are_named():
    assert reason(Path("/home/me/.env")) == "environment file"
    assert reason(Path("/home/me/.ssh/id_ed25519")) == ".ssh directory"
    assert reason(Path("/home/me/.git/hooks/pre-commit")) == ".git directory"
    assert reason(Path("/home/me/bridge.log.1")) == "bridge log"


def test_case_variants_of_sensitive_paths_are_refused_too():
    """On case-insensitive filesystems (macOS, Windows) ``.SSH`` *is*
    ``.ssh``; the exact-case comparison used to let it bypass the guard."""
    assert reason(Path("/home/me/.SSH/id_rsa")) == ".ssh directory"
    assert reason(Path("/home/me/ID_RSA")) == "credential or runtime file"
    assert reason(Path("/home/me/.ENV")) == "environment file"
    assert reason(Path("/home/me/.CONFIG/opencode/auth.json")) == (
        ".config/opencode directory"
    )
    # Exact-case keeps its original reason.
    assert reason(Path("/home/me/.ssh/id_rsa")) == ".ssh directory"


def test_the_example_file_and_ordinary_project_files_are_allowed():
    assert reason(Path("/home/me/project/.env.example")) is None
    assert reason(Path("/home/me/project/notes.md")) is None
    assert reason(Path("/home/me/project/.gitignore")) is None


def test_read_and_write_refuse_dotenv(tmp_path):
    target = tmp_path / ".env"
    target.write_text("TELEGRAM_BOT_TOKEN=secret\n")
    with pytest.raises(PathError, match="environment file"):
        localfs.read_file(str(tmp_path), ".env")
    with pytest.raises(PathError, match="environment file"):
        localfs.write_file(str(tmp_path), ".env", "replaced")


def test_a_symlink_cannot_hide_the_target_name(tmp_path):
    target = tmp_path / ".env"
    target.write_text("TELEGRAM_BOT_TOKEN=secret\n")
    (tmp_path / "notes.txt").symlink_to(target)
    with pytest.raises(PathError, match="environment file"):
        localfs.read_file(str(tmp_path), "notes.txt")


def test_config_check_path_applies_the_same_denylist(tmp_path):
    (tmp_path / ".env").write_text("x")
    config = Config(
        token="t",
        allowed_chats=frozenset({"42"}),
        opencode_url="u",
        password="p",
        default_dir=str(tmp_path),
        state_file=tmp_path / ".state.json",
        allowed_roots=(tmp_path,),
    )
    with pytest.raises(ConfigError, match="environment file"):
        config.check_path(str(tmp_path), ".env")


def test_ssh_and_systemd_paths_are_refused(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    key = ssh / "id_ed25519"
    key.write_text("private")
    with pytest.raises(PathError, match=".ssh directory"):
        localfs.read_file(str(tmp_path), ".ssh/id_ed25519")

    unit = tmp_path / ".config" / "systemd" / "user"
    unit.mkdir(parents=True)
    (unit / "bridge.service").write_text("[Service]\n")
    with pytest.raises(PathError, match=".config/systemd directory"):
        localfs.write_file(str(tmp_path), ".config/systemd/user/bridge.service", "x")
