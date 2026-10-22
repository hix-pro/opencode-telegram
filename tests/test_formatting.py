"""Formatting helpers."""

from __future__ import annotations

from bridgekit.formatting import (
    ago,
    basename,
    bullet_list,
    code_block,
    cost_line,
    head_tail,
    permission_line,
    session_line,
    short_id,
    tail,
    truncate,
)


def test_truncate_keeps_the_head():
    text = "x" * 100
    result = truncate(text, limit=50)
    assert len(result) <= 50
    assert result.startswith("x")
    assert "truncated" in result


def test_truncate_is_a_no_op_when_short_enough():
    assert truncate("hello", limit=50) == "hello"


def test_tail_keeps_the_end():
    text = "x" * 100
    result = tail(text, limit=50)
    assert len(result) <= 50
    assert result.endswith("x")
    assert "trimmed" in result


def test_tail_is_a_no_op_when_short_enough():
    assert tail("hello", limit=50) == "hello"


def test_tail_and_truncate_are_mirrors():
    # The live view needs the newest text; the finished message needs the head.
    text = "HEAD" + "m" * 200 + "TAIL"
    assert "TAIL" in tail(text, limit=60)
    assert "TAIL" not in truncate(text, limit=60)


def test_tail_survives_a_limit_smaller_than_its_marker():
    assert len(tail("x" * 100, limit=5)) == 5


def test_head_tail_keeps_both_ends():
    text = "START" + ("m" * 2000) + "END"
    result = head_tail(text, limit=200)
    assert len(result) <= 200
    assert "START" in result and "END" in result
    assert "middle omitted" in result


def test_code_block_adds_a_fence():
    assert code_block("hi") == "```\nhi\n```"


def test_code_block_escalates_the_fence_when_content_has_one():
    result = code_block("```\nnested\n```")
    assert result.startswith("````")
    assert result.endswith("````")


def test_short_id_keeps_the_prefix_readable():
    assert short_id("ses_eb946e1e5ffe2m2ta7xqQ3xG9e") == "ses_eb946e"
    assert short_id(None) == "-"


def test_basename_strips_trailing_slash():
    assert basename("/home/demo/projects/") == "projects"
    assert basename("/") == "/"
    assert basename(None) == "-"


def test_ago_scales_with_elapsed_time():
    import time

    now = int(time.time() * 1000)
    assert ago(now - 5_000) == "5s ago"
    assert ago(now - 120_000) == "2m ago"
    assert ago(now - 7_200_000) == "2h ago"
    assert ago(now - 172_800_000) == "2d ago"
    assert ago(None) == "unknown"


def test_bullet_list_handles_empty():
    assert bullet_list([]) == "(none)"
    assert bullet_list(["a", "b"]) == "- a\n- b"


def test_session_line_falls_back_to_a_directory():
    session = {"id": "ses_abcdefghijkl", "title": "Title", "time": {"updated": 0}}
    line = session_line(session, fallback_dir="/home/demo/code")
    assert "code" in line


def test_session_line_marks_the_active_one():
    session = {"id": "ses_abcdefghijkl", "title": "Title", "time": {"updated": 0}}
    assert session_line(session, active=True).startswith("* ")
    assert session_line(session, active=False).startswith("  ")


def test_permission_line_summarises_extra_resources():
    request = {"id": "p1", "action": "bash", "resources": ["a", "b", "c"]}
    assert "+2 more" in permission_line(request)


def test_cost_line_handles_missing_data():
    assert cost_line(None) == "-"
    assert "in 3 tok" in cost_line({"input": 2, "output": 1})


def test_tail_zero_limit_trims_to_nothing():
    # text[-0:] is the whole string: a zero limit used to return everything.
    assert tail("x" * 100, 0) == ""
    assert tail("x" * 100, -3) == ""
