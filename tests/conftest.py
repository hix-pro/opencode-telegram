"""Shared fixtures.

Tests never touch the network: :class:`FakeAPI` records calls and returns
canned payloads so command behaviour can be asserted in isolation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridgekit.api import ApiError
from bridgekit.config import Config
from bridgekit.state import State


class FakeAPI:
    """Minimal stand-in for :class:`bridgekit.api.OpenCode`."""

    def __init__(self, **overrides):
        self.calls: list[tuple] = []
        self.sessions: list[dict] = [
            {
                "id": "ses_aaaaaaaaaaaaaaaa",
                "title": "First session",
                "directory": "/home/demo",
                "time": {"created": 1_700_000_000_000, "updated": 1_700_000_100_000},
            },
            {
                "id": "ses_bbbbbbbbbbbbbbbb",
                "title": "Second session",
                "directory": "/home/demo",
                "time": {"created": 1_700_000_200_000, "updated": 1_700_000_300_000},
            },
        ]
        self.permissions: list[dict] = []
        self.fs: dict[str, str] = {}
        self.dirs: dict[str, list[dict]] = {}
        self.model_data: list[dict] = [
            {"providerID": "opencode", "id": "claude-haiku-5-5"},
            {"providerID": "opencode", "id": "gemini-4-argon"},
        ]
        self.agent_data: list[dict] = [
            {"id": "build", "description": "Default agent", "hidden": False},
            {"id": "plan", "description": "Planning only", "hidden": False},
            {"id": "secret", "description": "hidden", "hidden": True},
        ]
        self.shell_result = {"id": "sh_1", "status": "running"}
        self._pending_token = ""
        # Set False to simulate a shell the server reaps before it finishes.
        self.echo_sentinel = True
        self.shell_output_data = {"output": "hello\n", "status": "running"}
        for key, value in overrides.items():
            setattr(self, key, value)

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def list_sessions(self, directory=None, limit=20, search=None):
        self._record("list_sessions", directory, limit, search)
        return self.sessions[:limit]

    def get_session(self, session_id):
        self._record("get_session", session_id)
        for session in self.sessions:
            if session["id"] == session_id:
                return session
        raise ApiError(404, "Session not found")

    def create_session(self, directory, title=None, model=None, agent=None):
        self._record("create_session", directory, title, model, agent)
        created = {
            "id": "ses_created0000000000",
            "title": title or "untitled",
            "directory": directory,
        }
        self.sessions.insert(0, created)
        return created

    def update_session(self, session_id, title=None):
        self._record("update_session", session_id, title)
        return {"id": session_id, "title": title}

    def delete_session(self, session_id):
        self._record("delete_session", session_id)
        self.sessions = [s for s in self.sessions if s["id"] != session_id]
        return {}

    def fork_session(self, session_id):
        self._record("fork_session", session_id)
        return {"id": "ses_forked00000000000"}

    def compact_session(self, session_id):
        self._record("compact_session", session_id)
        return {}

    def interrupt_session(self, session_id):
        self._record("interrupt_session", session_id)
        return {}

    def switch_model(self, session_id, model):
        self._record("switch_model", session_id, model)
        return {}

    def switch_agent(self, session_id, agent):
        self._record("switch_agent", session_id, agent)
        return {}

    def send_prompt(self, session_id, text):
        self._record("send_prompt", session_id, text)

    def run_command(self, session_id, name, args=""):
        self._record("run_command", session_id, name, args)

    def messages(self, session_id, limit=20, order="desc"):
        self._record("messages", session_id, limit, order)
        return []

    def stats(self):
        return {
            "sessions": 3,
            "prompts": 12,
            "steps": 40,
            "tokens": {
                "input": 1000,
                "output": 200,
                "reasoning": 50,
                "cache": {"read": 5000, "write": 0},
            },
            "cost": 0.25,
            "tools": {"totals": {"calls": 9, "succeeded": 8, "failed": 1}},
            "activeDays": 3,
            "streak": 2,
            "activity": [{"date": "2026-10-15", "steps": 5}],
        }

    def export_session(self, session_id):
        return {"id": session_id, "messages": []}

    def permission_requests(self, directory=None):
        self._record("permission_requests", directory)
        return self.permissions

    def search_session_text(self, query, directory=None, sessions=10):
        return []

    def reply_permission(self, session_id, request_id, decision, message=None):
        self._record("reply_permission", session_id, request_id, decision)
        self.permissions = [p for p in self.permissions if p["id"] != request_id]
        return {}

    def models(self, directory):
        return self.model_data

    def agents(self, directory):
        return self.agent_data

    def commands(self, directory):
        return [{"name": "review"}]

    def skills(self, directory):
        return []

    def mcp_servers(self, directory):
        return []

    def projects(self):
        return [{"id": "proj_1", "worktree": "/home/demo"}]

    def read_file(self, directory, path):
        self._record("read_file", directory, path)
        if path not in self.fs:
            raise ApiError(404, f"File not found: {path}")
        return self.fs[path]

    def list_dir(self, directory, path="."):
        self._record("list_dir", directory, path)
        return self.dirs.get(path, [])

    def find_files(self, directory, query, limit=50):
        self._record("find_files", directory, query, limit)
        return [f"{query}.py", f"src/{query}.js"]

    def write_file(self, directory, path, content):
        self._record("write_file", directory, path, content)
        self.fs[path] = content
        return {}

    def run_shell(self, command, cwd, directory=None):
        self._record("run_shell", command, cwd, directory)
        # Mirror the server: the wrapper's sentinel is printed by the command,
        # so the fake must echo it back or the runner never finishes.
        import re as _re

        match = _re.search(r"__bridge_done_[0-9a-f]+", command)
        token = match.group(0) if match else ""
        self._pending_token = token
        return self.shell_result

    def shell_output(self, shell_id, directory=None):
        data = dict(self.shell_output_data)
        token = getattr(self, "_pending_token", "")
        if token and self.echo_sentinel and token not in data.get("output", ""):
            data["output"] = data.get("output", "") + f"\n{token} 0\n"
        return data

    def vcs_status(self, directory):
        return [{"state": "modified", "path": "README.md"}]

    def vcs_diff(self, directory):
        return "--- a\n+++ b\n"

    def worktrees(self, directory):
        return []

    def websearch(self, query, directory=None):
        self._record("websearch", query, directory)
        return {"results": [query]}

    def info(self):
        return {"version": "2.0.31", "pid": 1234, "url": "http://127.0.0.1:49374"}

    # Revert helpers used by the /revert command.
    def revert_stage(self, session_id):
        self._record("revert_stage", session_id)
        return {}

    def revert_commit(self, session_id):
        self._record("revert_commit", session_id)
        return {}

    def revert_clear(self, session_id):
        self._record("revert_clear", session_id)
        return {}


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture
def config(tmp_path):
    return Config(
        token="token",
        allowed_chats=frozenset({"42"}),
        opencode_url="http://127.0.0.1:1",
        password="secret",
        default_dir=str(tmp_path),
        state_file=tmp_path / ".state.json",
        max_output=3500,
        max_chunk=4096,
    )


@pytest.fixture
def state(tmp_path):
    st = State(tmp_path / ".state.json", str(tmp_path))
    st.load()
    return st


@pytest.fixture
def ctx(api, state, config, tmp_path):
    from bridgekit.commands import Context
    from bridgekit.schedule import ScheduleStore

    return Context(
        api=api,
        state=state,
        config=config,
        telegram=None,
        chat_id="42",
        schedules=ScheduleStore(),
    )


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "notes.md").write_text("hello from disk")
    (tmp_path / "sub").mkdir()
    return tmp_path
