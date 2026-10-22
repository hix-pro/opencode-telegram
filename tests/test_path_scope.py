"""Path containment and the /write transport.

Two findings from the second audit pass:

* ``fs/write`` takes ``application/octet-stream``, not JSON, so ``/write``
  returned 415 and silently wrote nothing.
* The server does **not** confine file operations to the session directory -
  ``../../../tmp/x`` escaped and wrote anyway. ALLOWED_ROOTS therefore has to
  be enforced by the client or it is decoration.
"""

from __future__ import annotations

import pytest

from bridgekit.commands import run
from bridgekit.config import Config, ConfigError

# --------------------------------------------------------------- containment


def scoped(tmp_path, roots):
    return Config(
        token="t",
        allowed_chats=frozenset({"42"}),
        opencode_url="u",
        password="p",
        default_dir=str(tmp_path),
        state_file=tmp_path / ".s.json",
        allowed_roots=tuple(roots),
    )


def test_relative_path_inside_the_root_is_allowed(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    config = scoped(tmp_path, [root])
    assert config.check_path(str(root), "notes.md") == str(root / "notes.md")


def test_traversal_out_of_the_root_is_refused(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    config = scoped(tmp_path, [root])
    with pytest.raises(ConfigError, match="outside the allowed roots"):
        config.check_path(str(root), "../../../etc/passwd")


def test_absolute_path_outside_the_root_is_refused(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    config = scoped(tmp_path, [root])
    with pytest.raises(ConfigError, match="outside the allowed roots"):
        config.check_path(str(root), "/etc/passwd")


def test_sneaky_traversal_is_refused(tmp_path):
    root = tmp_path / "project"
    (root / "sub").mkdir(parents=True)
    config = scoped(tmp_path, [root])
    with pytest.raises(ConfigError):
        config.check_path(str(root), "sub/../../outside.txt")


def test_dotdot_that_stays_inside_is_fine(tmp_path):
    root = tmp_path / "project"
    (root / "a" / "b").mkdir(parents=True)
    config = scoped(tmp_path, [root])
    assert config.check_path(str(root / "a" / "b"), "../c.txt") == str(root / "a" / "c.txt")


def test_a_sibling_directory_is_refused(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "secrets").mkdir()
    config = scoped(tmp_path, [root])
    with pytest.raises(ConfigError):
        config.check_path(str(root), "../secrets/id_rsa")


def test_unrestricted_allows_anything(tmp_path):
    """ALLOWED_ROOTS unset means unrestricted by design, not a bug."""
    config = scoped(tmp_path, [])
    resolved = config.check_path(str(tmp_path), "../../../etc/passwd")
    assert resolved.endswith("/etc/passwd")


def test_home_expansion_is_resolved(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    config = scoped(tmp_path, [root])
    with pytest.raises(ConfigError):
        config.check_path(str(root), "~/.ssh/id_rsa")


def test_commands_refuse_traversal(ctx, tmp_path):
    """The guard has to sit in the command layer, where the user types it."""
    from bridgekit.commands import CommandError

    root = tmp_path / "project"
    root.mkdir()
    object.__setattr__(ctx.config, "allowed_roots", (root,))
    ctx.state.set_directory("42", str(root))

    with pytest.raises(CommandError, match="outside the allowed roots"):
        run(ctx, "/write ../../../tmp/escaped.txt\nnope")


# ------------------------------------------------------------------- /write


def test_write_round_trips_through_the_local_filesystem(ctx, tmp_path):
    run(ctx, "/write out.txt\nhello\nworld")
    assert (tmp_path / "out.txt").read_text() == "hello\nworld"


def test_write_respects_the_allow_list(ctx, tmp_path):
    from bridgekit.commands import CommandError

    root = tmp_path / "allowed"
    root.mkdir()
    object.__setattr__(ctx.config, "allowed_roots", (root,))
    ctx.state.set_directory("42", str(root))
    with pytest.raises(CommandError, match="outside the allowed roots"):
        run(ctx, "/write ../escape.txt\nnope")
    assert not (tmp_path / "escape.txt").exists()
