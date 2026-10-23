"""Message chunking against Telegram's 4096-character limit."""

from __future__ import annotations

import pytest

from bridgekit.telegram import Telegram, split_message


def test_short_text_is_one_chunk():
    assert split_message("hello", 100) == ["hello"]


def test_empty_text_still_yields_one_chunk():
    assert split_message("", 100) == [""]


def test_splits_on_line_boundaries():
    text = "\n".join(f"line {i}" for i in range(50))
    chunks = split_message(text, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


def test_joins_back_to_the_original():
    text = "a" * 250 + "\n" + "b" * 250 + "\n" + "c" * 250
    chunks = split_message(text, 120)
    assert all(len(c) <= 120 for c in chunks)
    assert "".join(chunks) == text


def test_single_giant_line_is_hard_split():
    text = "x" * 1000
    chunks = split_message(text, 256)
    assert len(chunks) == 4  # 256 + 256 + 256 + a short remainder
    assert all(len(c) <= 256 for c in chunks)
    assert "".join(chunks) == text


def test_markdown_fences_are_preferred_over_mid_word_cuts():
    text = "```py\n" + "print('x')\n" * 40 + "```"
    chunks = split_message(text, 200)
    assert all(len(c) <= 200 for c in chunks)
    assert text.startswith(chunks[0][:10])


def test_no_empty_chunks_are_produced():
    text = "\n" * 500 + "end"
    chunks = split_message(text, 50)
    assert all(c for c in chunks)


# ---------------------------------------------------------------- redaction


def test_redact_strips_the_token():
    tg = Telegram("98765")
    assert (
        tg.redact("Max retries exceeded with url: /bot98765/getUpdates refused")
        == "Max retries exceeded with url: /bot***/getUpdates refused"
    )


def test_a_download_transport_error_names_no_token(tmp_path):
    """download() failures are posted straight into the chat: the raw
    requests text used to carry the whole token there."""
    from bridgekit.telegram import TelegramError

    class DeadSession:
        def get(self, url, **kwargs):
            raise ConnectionError(
                "Max retries exceeded with url: /file/bot98765/telegram "
                "(Caused by NameResolutionError)"
            )

    tg = Telegram("98765")
    tg._http = DeadSession()
    destination = tmp_path / "report.pdf"

    with pytest.raises(TelegramError) as caught:
        tg.download("file_x", destination)

    assert "98765" not in str(caught.value)


def test_a_transfer_death_leaves_no_partial_file(tmp_path):
    """A connection that dies mid-body must not publish a truncated file,
    and the error must not carry the token."""
    from types import SimpleNamespace

    from bridgekit.telegram import TelegramError

    class TransferDeath:
        def get(self, url, **kwargs):
            if url.endswith("/getFile"):
                return SimpleNamespace(
                    status_code=200,
                    text="",
                    json=lambda: {"result": {"file_path": "docs/report.pdf"}},
                )
            raise ConnectionError("connection reset by peer")

    tg = Telegram("98765")
    tg._http = TransferDeath()
    destination = tmp_path / "report.pdf"

    with pytest.raises(TelegramError) as caught:
        tg.download("file_x", destination)

    assert not destination.exists(), "a truncated attachment must not survive"
    assert "98765" not in str(caught.value)


def test_send_and_edit_redact_the_token_before_the_wire(monkeypatch):
    """A token that reaches a reply must not reach the chat."""
    from types import SimpleNamespace

    class Capture:
        def __init__(self):
            self.payloads = []

        def post(self, url, json=None, **kwargs):
            self.payloads.append(json)
            return SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {"result": {"message_id": 7}},
            )

    tg = Telegram("98765")
    session = Capture()
    tg._http = session

    tg.send("42", "the token is 98765 and must not leak")
    assert session.payloads[0]["text"] == "the token is *** and must not leak"

    assert tg.edit("42", 7, "again: 98765", force=True) is True
    assert session.payloads[1]["text"] == "again: ***"
