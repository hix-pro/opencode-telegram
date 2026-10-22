"""Live turn rendering.

Collects what the agent is doing during a turn and renders it as a single
message that is edited in place, so the chat shows progress rather than
silence. Tool activity is discovered by diffing the session's message parts,
because the event stream reports tool execution only through aggregate step
events rather than a per-tool payload.

Throttling matters: Telegram tolerates about 30 edits per minute per chat, so
:class:`TurnProgress` renders on demand and the caller decides when to push.
"""

from __future__ import annotations

import threading
import time

from .events import (
    EXECUTION_FAILED,
    EXECUTION_STARTED,
    EXECUTION_SUCCEEDED,
    STEP_STARTED,
    TEXT_DELTA,
    TEXT_ENDED,
    TEXT_STARTED,
    USAGE_UPDATED,
)
from .formatting import head_tail, tail, truncate

MAX_TOOL_LINES = 12
SPINNER = ("...", " ..", " .", "")

# Tools whose output is meant for the reader rather than for the transcript.
# Their text is surfaced in the message instead of being summarised away.
USER_FACING_TOOLS = frozenset({"question", "task"})

SPIN = "\u280b"


class TurnProgress:
    """Accumulates one turn's events and renders it for a phone screen."""

    def __init__(self, started_at: float | None = None) -> None:
        # on_event runs on the events thread while render/absorb_messages run
        # on the turn thread; without this, text.append could interleave badly.
        self._lock = threading.Lock()
        self.started_at = started_at or time.time()
        self.text = ""
        self.current_message_id: str | None = None
        self._text_source: str | None = None
        self.tools: list[str] = []
        self.status = "starting"
        self.model = ""
        self.agent = ""
        self.cost = 0.0
        self.tokens = 0
        self.notes: list[str] = []
        self.prompts: list[str] = []
        self.finished_at: float | None = None
        self.error: str | None = None

    # ------------------------------------------------------------- ingestion

    def on_event(self, kind: str, event: dict) -> None:
        """Feed one stream event in. Unknown types are ignored by design."""
        with self._lock:
            self._on_event(kind, event)

    def _on_event(self, kind: str, event: dict) -> None:
        data = event.get("data") or {}

        if kind == EXECUTION_STARTED:
            self.status = "running"
        elif kind == EXECUTION_SUCCEEDED:
            self.status = "done"
            self.finished_at = self.finished_at or time.time()
        elif kind == EXECUTION_FAILED:
            self.status = "failed"
            self.error = (data.get("error") or {}).get("name") or "execution failed"
            self.finished_at = self.finished_at or time.time()
        elif kind == STEP_STARTED:
            self.status = "running"
            self.agent = data.get("agent") or self.agent
            model = data.get("model") or {}
            if isinstance(model, dict) and model.get("id"):
                self.model = f"{model.get('providerID', '')}/{model['id']}".strip("/")
        elif kind == TEXT_STARTED:
            self.current_message_id = data.get("assistantMessageID")
        elif kind == TEXT_DELTA:
            delta = data.get("delta") or ""
            source = data.get("assistantMessageID")
            # Each assistant message is its own block of text; without this the
            # reply arrives as "first sentencesecond sentence".
            if self._text_source is not None and source != self._text_source:
                if self.text.strip():
                    self.text += "\n\n"
            self._text_source = source
            self.text += delta
        elif kind == TEXT_ENDED:
            self.current_message_id = None
        elif kind == USAGE_UPDATED:
            self.cost = data.get("cost", self.cost) or 0.0
            tokens = data.get("tokens") or {}
            self.tokens = (tokens.get("input", 0) or 0) + (tokens.get("output", 0) or 0)

    def add_tool(self, name: str, detail: str = "", running: bool = False) -> None:
        """Record a tool invocation.

        Keyed on name and detail so a tool that starts as ``running ...`` is
        replaced in place when it completes, rather than listed twice.
        """
        with self._lock:
            self._add_tool(name, detail, running)

    def _add_tool(self, name: str, detail: str, running: bool) -> None:
        entry = f"{name} ..." if running else name
        entry = f"{entry}{(' - ' + detail) if detail else ''}"
        for index, existing in enumerate(self.tools):
            if existing.split(" - ", 1)[0].rstrip(" .") == name and (
                detail == "" or existing.endswith(detail)
            ):
                self.tools[index] = entry
                return

        self.tools.append(entry)
        del self.tools[:-MAX_TOOL_LINES]

    def add_note(self, note: str) -> None:
        if note and note not in self.notes:
            self.notes.append(note)

    def absorb_messages(self, messages: list[dict], since: int = 0) -> None:
        """Pull tool activity out of polled messages the stream did not carry.

        *since* bounds prompt collection to the current turn. The poll asks for
        30 messages, which spans earlier turns, so without this bound every new
        turn rediscovered the previous turn's question and re-appended it to the
        reply - an echo that never went away.
        """
        for message in messages or ():
            if message.get("type") != "assistant":
                continue
            current = (message.get("time") or {}).get("created", 0) > since
            for part in message.get("content", []):
                if part.get("type") != "tool":
                    continue
                state = part.get("state") or {}
                status = state.get("status", "completed")
                name = part.get("name") or part.get("tool") or "tool"
                # `name` is the current field; `tool` was the older spelling.
                if current and name in USER_FACING_TOOLS:
                    prompt = self._user_facing(name, part, state)
                    if prompt and prompt not in self.prompts:
                        self.prompts.append(prompt)
                self.add_tool(
                    name,
                    self._tool_detail(part, state),
                    running=status in ("pending", "running"),
                )

    @classmethod
    def _user_facing(cls, tool: str, part: dict, state: dict) -> str:
        """The text of a tool whose output is addressed to the reader.

        These are not transcript events. A question, or a subagent's
        conclusion, *is* the reply: throwing it away left the chat showing an
        empty ``question ...`` line while the actual prompt sat unread in the
        part's ``input``.
        """
        if tool == "question":
            return cls._format_question(state.get("input") or {})
        blocks = state.get("content") or []
        text = "\n".join(
            block.get("text", "").strip()
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        return text

    @staticmethod
    def _format_question(payload: dict) -> str:
        """Render a question prompt as plain text Telegram can actually show.

        The interactive widget does not exist on this side of the bridge, so the
        options are numbered and the reader is told how to answer.
        """
        rendered = []
        for entry in payload.get("questions") or ():
            question = str(entry.get("question") or "").strip()
            if question:
                rendered.append(question)
            options = entry.get("options") or ()
            for index, option in enumerate(options, start=1):
                label = str(option.get("label") or "").strip()
                description = str(option.get("description") or "").strip()
                line = f"{index}. {label}"
                if description:
                    line += f"\n   {description}"
                rendered.append(line)
            if options:
                rendered.append("Reply with the number.")
        return "\n\n".join(rendered).strip()

    @staticmethod
    def _tool_detail(part: dict, state: dict) -> str:
        """One short line describing what a tool is doing."""
        tool = part.get("name") or part.get("tool")
        inputs = state.get("input") or {}
        blocks = state.get("content") or []
        output = state.get("output") or ""
        if not output:
            output = "".join(
                block.get("text", "") for block in blocks if isinstance(block, dict)
            )

        if tool in ("bash", "shell"):
            command = inputs.get("command") or inputs.get("cmd") or ""
            return " ".join(str(command).split())[:70]
        if tool == "read":
            return str(inputs.get("filePath") or inputs.get("path") or "")[-60:]
        if tool in ("write", "edit", "patch", "apply_patch"):
            target = inputs.get("filePath") or inputs.get("path") or ""
            return str(target)[-60:]
        if tool in ("glob", "grep"):
            pattern = inputs.get("pattern") or inputs.get("query") or ""
            return str(pattern)[:50]
        if tool in ("list", "ls"):
            return str(inputs.get("path") or "")[:50]
        if tool == "todowrite":
            return ""
        if state.get("title"):
            return str(state["title"])[:60]
        return str(output or "")[:50].replace("\n", " ").strip()

    # -------------------------------------------------------------- rendering

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    def is_running(self) -> bool:
        return self.status in ("starting", "running")

    def render(self, limit: int = 3500, streaming: bool = True) -> str:
        """The live message: status, tool trail, and the newest text so far.

        While a turn is streaming, the tail is what matters - that is the advice
        being given right now. Trimming from the head (as this used to) dropped
        every new sentence once the message passed *limit*, so a long reply
        looked frozen for its whole second half. The head is not lost: the
        finished message goes through :meth:`final_text`, which keeps both ends.

        The header and tool trail are budgeted separately from the body so that
        trimming never costs the status line or the running tool list.
        """
        if not streaming and self.text.strip():
            return self.final_text(limit)

        header_bits = [self._spinner() + " " + self._status_word()]
        if self.model:
            header_bits.append(self.model)
        header_bits.append(f"{self.elapsed:.0f}s")

        head_lines = [" | ".join(header_bits)]

        if self.tools:
            shown = self.tools[-6:]
            head_lines.append("")
            head_lines.extend(f"  {line}" for line in shown)

        head = "\n".join(head_lines)

        notes = ""
        if self.notes:
            notes = "\n" + "\n".join(f"  {note}" for note in self.notes[-3:])

        # A question or a subagent conclusion is the point of the turn, so it
        # is never the thing that gets trimmed.
        prompt_block = ""
        if self.prompts:
            prompt_block = "\n\n" + "\n\n".join(self.prompts)

        body = self.text.strip()
        if not body:
            if not self.tools:
                head += "\n\n  working..."
            return truncate(head + prompt_block + notes, limit)

        # -2 for the blank line joining head and body.
        budget = max(0, limit - len(head) - len(prompt_block) - len(notes) - 2)
        return head + "\n\n" + tail(body, budget) + prompt_block + notes

    def final_text(self, limit: int = 3500) -> str:
        """The finished message, with a footer the agent never has to write."""
        body = self.text.strip()
        if body:
            parts = [body]
        elif self.status == "failed":
            # "(no reply)" would be actively misleading here: the turn did not
            # finish, so say what happened instead.
            parts = [f"Turn failed: {self.error or 'unknown error'}."]
        elif self.prompts:
            # The turn ended by asking something. Saying "(no reply)" above the
            # question would be both noisy and wrong.
            parts = []
        else:
            parts = ["(no reply)"]

        if self.prompts:
            parts.extend(self.prompts)

        if self.notes:
            parts.extend(f"({note})" for note in self.notes[-3:])

        footer_bits = []
        if self.status == "failed" and body:
            footer_bits.append(f"failed: {self.error or 'unknown error'}")
        footer_bits.append(f"{self.elapsed:.0f}s")
        if self.cost:
            footer_bits.append(f"${self.cost:.4f}")
        parts.append(" - ".join(footer_bits))

        return head_tail("\n\n".join(parts), limit)

    def _status_word(self) -> str:
        return {
            "starting": "starting",
            "running": "working",
            "done": "done",
            "failed": "failed",
        }.get(self.status, self.status)

    def _spinner(self) -> str:
        if not self.is_running():
            return "" if self.status == "done" else "!"
        return SPIN + SPINNER[int(self.elapsed) % len(SPINNER)]
