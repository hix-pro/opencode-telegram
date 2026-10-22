"""Message chunking against Telegram's 4096-character limit."""

from __future__ import annotations

from bridgekit.telegram import split_message


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
