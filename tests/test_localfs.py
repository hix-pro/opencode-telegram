"""Local filesystem access for /ls, /read, /find and /write.

These commands used to go through the server's HTTP fs API, which ignores the
requested directory and resolves everything against its own working directory.
That made /write land in the wrong place, /read return the wrong file in a
subdirectory, and /find return unrelated matches from across the home folder.

The bridge runs on the same machine, so it reads and writes the filesystem
directly. These tests pin both the behaviour and the containment.
"""

from __future__ import annotations

import pytest

from bridgekit import localfs
from bridgekit.localfs import PathError


def test_list_dir_orders_directories_first(tmp_path):
    (tmp_path / "zebra").mkdir()
    (tmp_path / "alpha.txt").write_text("a")
    (tmp_path / "Beta.txt").write_text("b")
    names = [e.name for e in localfs.list_dir(str(tmp_path))]
    assert names[0] == "zebra"
    assert set(names[1:]) == {"alpha.txt", "Beta.txt"}


def test_list_dir_accepts_a_subpath(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("x")
    assert [e.name for e in localfs.list_dir(str(tmp_path), "sub")] == ["f.txt"]


def test_list_dir_reports_missing_and_files(tmp_path):
    with pytest.raises(PathError, match="No such directory"):
        localfs.list_dir(str(tmp_path), "nope")
    (tmp_path / "f").write_text("x")
    with pytest.raises(PathError, match="Not a directory"):
        localfs.list_dir(str(tmp_path), "f")


def test_read_file(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    assert localfs.read_file(str(tmp_path), "a.txt") == "hello"


def test_read_handles_binary_without_crashing(tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\x00\xff\xfe binary")
    text = localfs.read_file(str(tmp_path), "b.bin")
    assert isinstance(text, str)


def test_read_enforces_a_size_cap(tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (localfs.MAX_READ_BYTES + 10))
    with pytest.raises(PathError, match="read limit"):
        localfs.read_file(str(tmp_path), "big.bin")


def test_write_file_creates_parents(tmp_path):
    written = localfs.write_file(str(tmp_path), "a/b/c.txt", "data")
    assert written.read_text() == "data"


def test_write_refuses_a_directory(tmp_path):
    (tmp_path / "adir").mkdir()
    with pytest.raises(PathError, match="Not a file"):
        localfs.write_file(str(tmp_path), "adir", "x")


# ------------------------------------------------------------- containment


def roots(tmp_path, *names):
    made = []
    for name in names:
        path = tmp_path / name
        path.mkdir(parents=True, exist_ok=True)
        made.append(path)
    return tuple(made)


def test_traversal_is_blocked(tmp_path):
    allowed = roots(tmp_path, "allowed")
    with pytest.raises(PathError, match="outside the allowed roots"):
        localfs.read_file(str(allowed[0]), "../../../etc/passwd", allowed)
    with pytest.raises(PathError, match="outside the allowed roots"):
        localfs.write_file(str(allowed[0]), "../escape.txt", "x", allowed)


def test_absolute_paths_are_blocked(tmp_path):
    allowed = roots(tmp_path, "allowed")
    with pytest.raises(PathError):
        localfs.read_file(str(allowed[0]), "/etc/passwd", allowed)


def test_sneaky_traversal_is_blocked(tmp_path):
    allowed = roots(tmp_path, "allowed")
    (allowed[0] / "sub").mkdir()
    with pytest.raises(PathError):
        localfs.read_file(str(allowed[0]), "sub/../../escape", allowed)


def test_inside_the_root_is_fine(tmp_path):
    allowed = roots(tmp_path, "allowed")
    (allowed[0] / "sub").mkdir()
    (allowed[0] / "sub" / "f.txt").write_text("ok")
    assert localfs.read_file(str(allowed[0]), "sub/f.txt", allowed) == "ok"
    assert localfs.read_file(str(allowed[0]), "sub/../sub/f.txt", allowed) == "ok"


def test_unrestricted_allows_anything(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    assert localfs.read_file(str(tmp_path), "f.txt", ()) == "x"


# ------------------------------------------------------------------- find


def test_find_is_scoped_to_the_base(tmp_path):
    base = tmp_path / "project"
    (base / "sub").mkdir(parents=True)
    (base / "sub" / "child.txt").write_text("x")
    outside = tmp_path / "other"
    outside.mkdir()
    (outside / "child_elsewhere.txt").write_text("x")

    found = localfs.find(str(base), "child")
    assert found == ["sub/child.txt"]


def test_find_skips_hidden_directories(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "childfile").write_text("x")
    assert localfs.find(str(tmp_path), "child") == []


def test_find_respects_its_limit(tmp_path):
    for index in range(50):
        (tmp_path / f"match{index}.txt").write_text("x")
    assert len(localfs.find(str(tmp_path), "match", limit=10)) == 10


def test_find_returns_nothing_for_no_match(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert localfs.find(str(tmp_path), "zzzz") == []


def test_find_is_confined_like_everything_else(tmp_path):
    """find resolves its base through the same guard as read and write."""
    allowed = roots(tmp_path, "allowed")
    (allowed[0] / "keep.txt").write_text("x")
    assert localfs.find(str(allowed[0]), "keep", allowed) == ["keep.txt"]
