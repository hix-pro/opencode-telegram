"""Attachment extraction and naming."""

from __future__ import annotations

from pathlib import Path

import pytest

from bridgekit import attachments


def test_photo_picks_the_largest_size():
    message = {
        "photo": [
            {"file_id": "small", "file_size": 100},
            {"file_id": "big", "file_size": 5000},
            {"file_id": "mid", "file_size": 900},
        ]
    }
    found = attachments.extract(message)
    assert found[0] == "big"
    assert found[2] == "photo"


def test_document_keeps_its_name():
    message = {"document": {"file_id": "d1", "file_name": "report.pdf"}}
    file_id, name, kind = attachments.extract(message)
    assert (file_id, name, kind) == ("d1", "report.pdf", "document")


def test_voice_gets_a_unique_name():
    message = {"voice": {"file_id": "v1", "duration": 3}}
    file_id, name, kind = attachments.extract(message)
    assert kind == "voice"
    assert name.startswith("voice-") and name.endswith(".ogg")


def test_audio_and_video_are_recognised():
    assert attachments.extract({"audio": {"file_id": "a1"}})[2] == "audio"
    assert attachments.extract({"video": {"file_id": "v1"}})[2] == "video"


def test_sticker_is_treated_as_an_image():
    found = attachments.extract({"sticker": {"file_id": "s1"}})
    assert found is not None and found[2] == "image"


def test_text_only_message_yields_nothing():
    assert attachments.extract({"text": "hello"}) is None


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        "/absolute/path.txt",
        "name with spaces.txt",
        "weird;name|with*chars.txt",
    ],
)
def test_names_cannot_escape_the_cache(hostile):
    name = attachments.safe_name(hostile, "fallback.bin")
    assert "/" not in name and ".." not in name
    assert name == name.strip("_.")


def test_blank_name_gets_the_fallback():
    assert attachments.safe_name("   ", "fallback.bin") == "fallback.bin"


def test_store_writes_the_file_and_builds_a_uri(tmp_path):
    class FakeTelegram:
        def download(self, file_id, destination):
            Path(destination).write_bytes(b"hello")
            return Path(destination)

    stored = attachments.store(
        FakeTelegram(), "f1", "note.txt", "document", tmp_path / ".attachments"
    )
    assert stored.path.read_bytes() == b"hello"
    assert stored.uri.startswith("file://")
    assert "note.txt" in stored.caption


def test_cache_and_downloads_are_owner_only(tmp_path):
    """Attachment copies can contain credentials or private source; the cache
    directory and every file in it must not inherit a permissive umask."""
    import stat

    class FakeTelegram:
        def download(self, file_id, destination):
            Path(destination).write_bytes(b"secret")
            return Path(destination)

    cache = tmp_path / ".attachments"
    stored = attachments.store(FakeTelegram(), "f1", "note.txt", "document", cache)

    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    assert stat.S_IMODE(stored.path.stat().st_mode) == 0o600


def test_store_rejects_oversized_files(tmp_path):
    class BigTelegram:
        def download(self, file_id, destination):
            Path(destination).write_bytes(b"x" * (attachments.MAX_BYTES + 1))
            return Path(destination)

    with pytest.raises(ValueError, match="over the"):
        attachments.store(BigTelegram(), "f1", "big.bin", "document", tmp_path)
