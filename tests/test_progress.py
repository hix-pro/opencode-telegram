"""Live turn rendering."""

from __future__ import annotations

from bridgekit.events import (
    EXECUTION_FAILED,
    EXECUTION_STARTED,
    EXECUTION_SUCCEEDED,
    STEP_STARTED,
    TEXT_DELTA,
    TEXT_STARTED,
    USAGE_UPDATED,
)
from bridgekit.progress import TurnProgress


def evt(kind: str, **data):
    """Build an event payload the way the stream delivers it."""
    return {"type": kind, "data": data}


def test_text_deltas_accumulate():
    progress = TurnProgress()
    progress.on_event(TEXT_STARTED, evt(TEXT_STARTED, assistantMessageID="m1"))
    for chunk in ("Hello", ", ", "world"):
        progress.on_event(TEXT_DELTA, evt(TEXT_DELTA, assistantMessageID="m1", delta=chunk))
    assert progress.text == "Hello, world"


def test_a_new_assistant_message_starts_a_new_paragraph():
    progress = TurnProgress()
    progress.on_event(TEXT_DELTA, evt(TEXT_DELTA, assistantMessageID="m1", delta="first"))
    progress.on_event(TEXT_DELTA, evt(TEXT_DELTA, assistantMessageID="m2", delta="second"))
    assert progress.text == "first\n\nsecond"


def test_execution_events_move_the_status():
    progress = TurnProgress()
    assert progress.is_running()
    progress.on_event(EXECUTION_STARTED, evt(EXECUTION_STARTED, sessionID="s"))
    assert progress.status == "running"
    progress.on_event(EXECUTION_SUCCEEDED, evt(EXECUTION_SUCCEEDED, sessionID="s"))
    assert progress.status == "done"
    assert not progress.is_running()
    assert progress.finished_at is not None


def test_failure_is_recorded():
    progress = TurnProgress()
    progress.on_event(EXECUTION_FAILED, evt(EXECUTION_FAILED, error={"name": "ProviderError"}))
    assert progress.status == "failed"
    assert "ProviderError" in progress.final_text()


def test_step_start_records_the_model():
    progress = TurnProgress()
    progress.on_event(
        STEP_STARTED,
        evt(STEP_STARTED, agent="build", model={"providerID": "opencode", "id": "beam-free"}),
    )
    assert progress.agent == "build"
    assert progress.model == "opencode/beam-free"
    assert "opencode/beam-free" in progress.render()


def test_usage_is_summed():
    progress = TurnProgress()
    progress.on_event(
        USAGE_UPDATED,
        evt(USAGE_UPDATED, cost=0.0123, tokens={"input": 100, "output": 20}),
    )
    assert progress.tokens == 120
    assert progress.cost == 0.0123


def test_unknown_events_are_ignored():
    progress = TurnProgress()
    progress.on_event(
        "session.renamed", {"type": "session.renamed", "data": {"sessionID": "s"}}
    )
    progress.on_event("provider.updated", {"type": "provider.updated", "data": {}})
    assert progress.text == ""


def test_tool_lines_are_collected_from_messages():
    """Current servers send `name` and `state.content`; older ones `tool`."""
    progress = TurnProgress()
    progress.absorb_messages(
        [
            {
                "type": "assistant",
                "content": [
                    {
                        "type": "tool",
                        "name": "shell",
                        "state": {
                            "status": "completed",
                            "input": {"command": "pytest -q"},
                            "content": [{"type": "text", "text": "210 passed"}],
                        },
                    }
                ],
            }
        ]
    )
    assert len(progress.tools) == 1
    assert "shell" in progress.tools[0]
    assert "pytest -q" in progress.tools[0]


def test_legacy_tool_field_is_still_understood():
    progress = TurnProgress()
    progress.absorb_messages(
        [
            {
                "type": "assistant",
                "content": [
                    {
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "completed", "input": {"command": "ls"}},
                    }
                ],
            }
        ]
    )
    assert "bash" in progress.tools[0]


def test_repeated_tools_collapse():
    progress = TurnProgress()
    message = {
        "type": "assistant",
        "content": [
            {
                "type": "tool",
                "name": "read",
                "state": {"status": "completed", "input": {"filePath": "a.py"}},
            }
        ],
    }
    progress.absorb_messages([message])
    progress.absorb_messages([message])
    assert len(progress.tools) == 1


def test_running_tools_show_progress():
    progress = TurnProgress()
    progress.absorb_messages(
        [
            {
                "type": "assistant",
                "content": [{"type": "tool", "tool": "write", "state": {"status": "running"}}],
            }
        ]
    )
    assert progress.tools[0].endswith("...")


def test_tool_trail_is_capped():
    progress = TurnProgress()
    for index in range(40):
        progress.add_tool("read", f"file{index}.py")
    assert len(progress.tools) <= 12
    assert "file39.py" in progress.tools[-1]


def test_render_shows_status_tools_and_text():
    progress = TurnProgress()
    progress.add_tool("bash - pytest -q")
    progress.text = "All green."
    rendered = progress.render()
    assert "All green." in rendered
    assert "pytest" in rendered
    assert "starting" in rendered or "working" in rendered


def test_render_before_anything_happens():
    assert "working" in TurnProgress().render()


def test_final_text_includes_a_footer():
    progress = TurnProgress()
    progress.text = "Done."
    progress.on_event(
        USAGE_UPDATED, evt(USAGE_UPDATED, cost=0.5, tokens={"input": 10, "output": 5})
    )
    final = progress.final_text()
    assert final.startswith("Done.")
    assert "$0.5000" in final
    assert "s" in final


def test_final_text_says_so_when_empty():
    assert "(no reply)" in TurnProgress().final_text()


def test_notes_are_shown():
    progress = TurnProgress()
    progress.add_note("interrupted")
    assert "interrupted" in progress.render()
    assert "interrupted" in progress.final_text()


def test_render_respects_the_limit():
    progress = TurnProgress()
    progress.text = "x" * 10_000
    assert len(progress.render(limit=500)) <= 500


# --- regression: the live view used to trim from the head --------------------
# A long reply silently lost every new sentence once it passed the limit, so the
# chat looked frozen while the turn was still progressing.


def test_render_keeps_the_newest_text_when_the_reply_overflows():
    progress = TurnProgress()
    progress.status = "running"
    # A unique marker only in the oldest sentence, so the assertion cannot pass
    # by accident on filler text that also appears later in the body.
    progress.text = (
        "OLDEST-SENTENCE-MARKER " + ("filler words. " * 600) + "MY RECOMMENDATIONS: try this."
    )
    rendered = progress.render(limit=3500)
    assert "MY RECOMMENDATIONS" in rendered
    assert "OLDEST-SENTENCE-MARKER" not in rendered


def test_render_keeps_the_status_line_when_the_reply_overflows():
    progress = TurnProgress()
    progress.status = "running"
    progress.text = "PREAMBLE. " * 600
    rendered = progress.render(limit=3500)
    first_line = rendered.splitlines()[0]
    assert "working" in first_line


def test_render_keeps_the_tool_trail_when_the_reply_overflows():
    progress = TurnProgress()
    progress.add_tool("grep", "-n something")
    progress.add_tool("read", "bridge.py")
    progress.text = "PREAMBLE. " * 600
    rendered = progress.render(limit=3500)
    assert "grep" in rendered
    assert "read" in rendered


def test_render_overflow_still_respects_the_limit():
    progress = TurnProgress()
    progress.add_tool("grep", "-n something")
    progress.text = "PREAMBLE. " * 600
    assert len(progress.render(limit=3500)) <= 3500


def test_render_marks_what_it_trimmed():
    progress = TurnProgress()
    progress.text = "PREAMBLE. " * 600 + "THE END"
    assert "earlier text trimmed" in progress.render(limit=500)


# --- regression: tools whose output IS the message ---------------------------
# A question lived in the tool part's ``input``. The one-line tool summary has
# no case for it and rendered an empty detail, so the chat showed the bare word
# "question" and the prompt itself was never read.


def _question_part(question, options, created=5000):
    return {
        "type": "assistant",
        "time": {"created": created},
        "content": [
            {
                "type": "tool",
                "name": "question",
                "state": {
                    "status": "completed",
                    "input": {
                        "questions": [
                            {"question": question, "options": options, "multiple": False}
                        ]
                    },
                    "content": [{"type": "text", "text": "user answered"}],
                },
            }
        ],
    }


def test_question_tool_is_rendered_as_text():
    progress = TurnProgress()
    progress.status = "done"
    progress.absorb_messages(
        [_question_part("How do you want to handle it?", [{"label": "Restart now"}])]
    )
    out = progress.final_text()
    assert "How do you want to handle it?" in out
    assert "1. Restart now" in out


def test_question_tool_shows_option_descriptions():
    progress = TurnProgress()
    progress.absorb_messages(
        [
            _question_part(
                "Pick one",
                [{"label": "Restart", "description": "picks up the fix"}],
            )
        ]
    )
    assert "picks up the fix" in progress.final_text()


def test_question_tool_tells_the_reader_how_to_answer():
    progress = TurnProgress()
    progress.absorb_messages([_question_part("Pick", [{"label": "A"}])])
    assert "Reply with the number." in progress.final_text()


def test_question_alone_does_not_report_no_reply():
    # The turn ended by asking something; "(no reply)" would be wrong.
    progress = TurnProgress()
    progress.status = "done"
    progress.absorb_messages([_question_part("Pick", [{"label": "A"}])])
    assert "(no reply)" not in progress.final_text()


def test_question_survives_an_overflowing_reply():
    progress = TurnProgress()
    progress.status = "done"
    progress.text = "OLDEST-MARKER " + ("filler. " * 600)
    progress.absorb_messages([_question_part("Pick", [{"label": "A"}])])
    out = progress.render(limit=1200)
    assert "Pick" in out
    assert "1. A" in out


def test_question_appears_in_the_live_render_not_only_the_final_one():
    progress = TurnProgress()
    progress.status = "running"
    progress.absorb_messages([_question_part("Pick", [{"label": "A"}])])
    assert "Pick" in progress.render()


def test_repeated_polling_does_not_duplicate_the_question():
    progress = TurnProgress()
    message = _question_part("Pick", [{"label": "A"}])
    progress.absorb_messages([message])
    progress.absorb_messages([message])
    assert progress.final_text().count("Pick") == 1


def test_task_conclusion_is_surfaced():
    progress = TurnProgress()
    progress.absorb_messages(
        [
            {
                "type": "assistant",
                "time": {"created": 5000},
                "content": [
                    {
                        "type": "tool",
                        "name": "task",
                        "state": {
                            "status": "completed",
                            "content": [{"type": "text", "text": "Subagent conclusion."}],
                        },
                    }
                ],
            }
        ]
    )
    assert "Subagent conclusion." in progress.final_text()


def test_a_previous_turns_question_is_not_echoed():
    # Regression: the poll returns 30 messages, spanning turns. Collecting
    # prompts without a time bound made every later reply repeat the old
    # question forever.
    old = {
        "type": "assistant",
        "time": {"created": 1000},
        "content": [
            {
                "type": "tool",
                "name": "question",
                "state": {
                    "status": "completed",
                    "input": {"questions": [{"question": "OLD QUESTION", "options": []}]},
                    "content": [{"type": "text", "text": "answered"}],
                },
            }
        ],
    }
    progress = TurnProgress()
    progress.status = "done"
    progress.absorb_messages([old], since=2000)
    assert progress.prompts == []
    assert "OLD QUESTION" not in progress.final_text()


def test_the_current_turns_question_is_kept():
    fresh = {
        "type": "assistant",
        "time": {"created": 5000},
        "content": [
            {
                "type": "tool",
                "name": "question",
                "state": {
                    "status": "completed",
                    "input": {
                        "questions": [
                            {"question": "NEW QUESTION", "options": [{"label": "yes"}]}
                        ]
                    },
                    "content": [{"type": "text", "text": "answered"}],
                },
            }
        ],
    }
    progress = TurnProgress()
    progress.status = "done"
    progress.absorb_messages([fresh], since=2000)
    assert "NEW QUESTION" in progress.final_text()


def test_old_question_does_not_leak_into_a_new_reply():
    # The exact failure: turn 1 asks, turn 2 answers and the old prompt is
    # re-appended to turn 2's message.
    old = {
        "type": "assistant",
        "time": {"created": 1000},
        "content": [
            {
                "type": "tool",
                "name": "question",
                "state": {
                    "status": "completed",
                    "input": {"questions": [{"question": "STALE PROMPT", "options": []}]},
                    "content": [{"type": "text", "text": "answered"}],
                },
            }
        ],
    }
    fresh = {
        "type": "assistant",
        "time": {"created": 6000},
        "content": [{"type": "text", "text": "A brand new reply."}],
    }
    progress = TurnProgress()
    progress.status = "done"
    progress.absorb_messages([old, fresh], since=2000)
    progress.text = "A brand new reply."
    out = progress.final_text()
    assert "STALE PROMPT" not in out
    assert "A brand new reply." in out


def test_ordinary_tools_are_still_only_summarised():
    # A shell command must not dump its output into the message.
    progress = TurnProgress()
    progress.absorb_messages(
        [
            {
                "type": "assistant",
                "content": [
                    {
                        "type": "tool",
                        "name": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "ls -la"},
                            "content": [{"type": "text", "text": "a very long listing"}],
                        },
                    }
                ],
            }
        ]
    )
    assert progress.prompts == []
    assert "ls -la" in progress.render()
