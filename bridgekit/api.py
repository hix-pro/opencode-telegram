"""Thin client over the OpenCode HTTP API.

Design notes
------------
* Every response is wrapped in ``{"data": ...}`` except a few endpoints
  (``fs/read`` returns the file body verbatim). :meth:`OpenCode._request`
  unwraps by default and :meth:`read_file` opts out.
* Endpoints that operate on a project take a ``directory`` query parameter.
  It is declared as an OpenAPI ``deepObject`` named ``location``, which in
  practice means ``?directory=/path`` - *not* ``?location=/path``.
* ``fs/read`` takes a path relative to ``directory``; an absolute path is
  silently reinterpreted as relative to the server's cwd and 404s.
"""

from __future__ import annotations

import time
from typing import Any

import requests

# Permission decisions, in the order a human would pick them.
PERMISSION_ONCE = "once"
PERMISSION_ALWAYS = "always"
PERMISSION_REJECT = "reject"


class ApiError(RuntimeError):
    """An HTTP call to OpenCode failed."""

    def __init__(self, status: int, message: str, connect_failed: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        # True only when the request was never established, so the caller
        # knows a retry is side-effect free.
        self.connect_failed = connect_failed


class OpenCode:
    def __init__(self, base_url: str, password: str, timeout: int = 30) -> None:
        self.base = base_url.rstrip("/")
        self.auth = ("opencode", password)
        self.timeout = timeout
        self._http = requests.Session()
        self._location: str = ""
        self._location_at: float = 0.0

    # ------------------------------------------------------------------ core

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
        directory: str | None = None,
        unwrap: bool = True,
    ) -> Any:
        query = dict(params or {})
        if directory:
            query["directory"] = directory
        try:
            resp = self._http.request(
                method,
                f"{self.base}{path}",
                params=query or None,
                json=body,
                auth=self.auth,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise ApiError(0, f"Could not reach OpenCode: {exc}", connect_failed=True) from exc

        if resp.status_code >= 400:
            raise ApiError(resp.status_code, self._error_message(resp))

        if not resp.content:
            return {}
        try:
            payload = resp.json()
        except ValueError as exc:
            # A 200 carrying an HTML error page (proxy, mid-restart server)
            # must not escape as a bare JSONDecodeError past `except ApiError`.
            raise ApiError(resp.status_code, "Server sent a non-JSON response") from exc
        if unwrap and isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    @staticmethod
    def _error_message(resp: requests.Response) -> str:
        """Turn an error body into something worth reading on a phone."""
        try:
            payload = resp.json()
        except ValueError:
            return resp.text.strip()[:300] or f"HTTP {resp.status_code}"

        if isinstance(payload, dict):
            for key in ("message", "error", "_tag"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    extra = payload.get("data") or payload.get("path")
                    return f"{value} {extra}" if extra else value
        return str(payload)[:300]

    # -------------------------------------------------------------- instance

    def info(self) -> dict:
        return self._request("GET", "/api/info")

    def location(self, max_age: float = 30.0) -> str:
        """The directory the server's file operations are rooted at.

        The `directory` query parameter is ignored by the fs endpoints: they
        always resolve relative paths against this one location, which is the
        server's own working directory. Cached because it cannot change while
        the service runs.
        """
        now = time.monotonic()
        if self._location and now - self._location_at < max_age:
            return self._location
        try:
            data = self._request("GET", "/api/location")
        except ApiError:
            return self._location or ""
        found = (data or {}).get("directory") or ""
        if found:
            self._location = found
            self._location_at = now
        return self._location or ""

    def projects(self) -> list[dict]:
        return self._request("GET", "/api/project") or []

    # ------------------------------------------------------------- discovery

    def agents(self, directory: str) -> list[dict]:
        return self._request("GET", "/api/agent", directory=directory) or []

    def models(self, directory: str) -> list[dict]:
        return self._request("GET", "/api/model", directory=directory) or []

    def commands(self, directory: str) -> list[dict]:
        return self._request("GET", "/api/command", directory=directory) or []

    def skills(self, directory: str) -> list[dict]:
        return self._request("GET", "/api/skill", directory=directory) or []

    def mcp_servers(self, directory: str) -> list[dict]:
        return self._request("GET", "/api/mcp", directory=directory) or []

    # --------------------------------------------------------------- session

    def list_sessions(
        self,
        directory: str | None = None,
        limit: int = 20,
        search: str | None = None,
    ) -> list[dict]:
        params: dict = {"limit": limit, "order": "desc"}
        if search:
            params["search"] = search
        return self._request("GET", "/api/session", params=params, directory=directory) or []

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"/api/session/{session_id}")

    def create_session(
        self,
        directory: str,
        title: str | None = None,
        model: str | None = None,
        agent: str | None = None,
    ) -> dict:
        body: dict = {"location": {"directory": directory}}
        if title:
            body["title"] = title
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = self._model_ref(model)
        return self._request("POST", "/api/session", body=body)

    def update_session(self, session_id: str, title: str | None = None) -> dict:
        body: dict = {}
        if title is not None:
            body["title"] = title
        return self._request("PATCH", f"/api/session/{session_id}", body=body)

    def delete_session(self, session_id: str) -> dict:
        return self._request("DELETE", f"/api/session/{session_id}")

    def fork_session(self, session_id: str) -> dict:
        return self._request("POST", f"/api/session/{session_id}/fork", body={})

    def compact_session(self, session_id: str) -> dict:
        return self._request("POST", f"/api/session/{session_id}/compact", body={})

    def interrupt_session(self, session_id: str) -> dict:
        return self._request("POST", f"/api/session/{session_id}/interrupt", body={})

    def switch_agent(self, session_id: str, agent: str) -> dict:
        return self._request("POST", f"/api/session/{session_id}/agent", body={"agent": agent})

    def switch_model(self, session_id: str, model: str) -> dict:
        return self._request(
            "POST",
            f"/api/session/{session_id}/model",
            body={"model": self._model_ref(model)},
        )

    def send_prompt(
        self,
        session_id: str,
        text: str,
        files: list | None = None,
        delivery: str | None = None,
        attempts: int = 5,
        backoff: float = 2.0,
    ) -> None:
        """Submit a turn.

        *files* is a list of {uri, name, description} dicts.

        *delivery* matters more than it looks. The server defaults to ``steer``,
        which feeds a message into the running turn and cancels it. ``queue``
        waits its turn instead, which is what a chat wants.

        Connection failures are retried with backoff because the OpenCode
        server restarts independently of this bridge and a refused connection
        mid-turn would otherwise swallow the message.
        """
        body: dict = {"text": text}
        if files:
            body["files"] = files
        if delivery:
            body["delivery"] = delivery

        for attempt in range(attempts):
            try:
                self._request("POST", f"/api/session/{session_id}/prompt", body=body)
                return
            except ApiError as exc:
                # Only a failure to *connect* is safe to retry. A connection
                # that dropped after the request was sent is ambiguous, and
                # retrying would run the agent's work twice - two edits, two
                # deploys, two commands.
                if not exc.connect_failed or attempt == attempts - 1:
                    raise
                time.sleep(backoff * (2**attempt))

    def run_command(self, session_id: str, name: str, args: str = "") -> None:
        self._request(
            "POST",
            f"/api/session/{session_id}/command",
            body={"name": name, "text": args},
        )

    def messages(self, session_id: str, limit: int = 20, order: str = "desc") -> list[dict]:
        return (
            self._request(
                "GET",
                f"/api/session/{session_id}/message",
                params={"limit": limit, "order": order},
            )
            or []
        )

    def context(self, session_id: str) -> dict:
        return self._request("GET", f"/api/session/{session_id}/context")

    def stats(self) -> dict:
        """Aggregate usage across all sessions.

        The endpoint returns a single object - sessions, prompts, steps,
        tokens, cost, tool totals, activity, models - not a per-session list.
        """
        return self._request("GET", "/api/experimental/session/stats") or {}

    def export_session(self, session_id: str) -> Any:
        return self._request(
            "GET", f"/api/experimental/session/{session_id}/export", unwrap=False
        )

    # ---------------------------------------------------------------- revert

    def revert_stage(self, session_id: str) -> dict:
        """Mark the last turn for rollback."""
        return self._request("POST", f"/api/session/{session_id}/revert/stage", body={})

    def revert_commit(self, session_id: str) -> dict:
        return self._request("POST", f"/api/session/{session_id}/revert/commit", body={})

    def revert_clear(self, session_id: str) -> dict:
        return self._request("DELETE", f"/api/session/{session_id}/revert")

    # ----------------------------------------------------------- permissions

    def search_session_text(
        self, query: str, directory: str | None = None, sessions: int = 10
    ) -> list[tuple[str, str]]:
        """Search the *content* of recent sessions.

        ``session.list``'s search parameter matches titles only, so anything
        whose title does not contain the query is invisible. Returns
        (session_id, snippet) pairs, newest first.
        """
        needle = query.lower()
        hits: list[tuple[str, str]] = []
        for session in self.list_sessions(directory=directory, limit=sessions):
            try:
                messages = self.messages(session["id"], limit=60, order="desc")
            except ApiError:
                continue
            for message in messages:
                text = "".join(
                    block.get("text", "")
                    for block in message.get("content", [])
                    if block.get("type") == "text"
                ).strip()
                if needle in text.lower():
                    index = text.lower().index(needle)
                    start = max(0, index - 40)
                    hits.append((session["id"], text[start : index + len(query) + 60].strip()))
                    break
            if len(hits) >= 5:
                break
        return hits

    def permission_requests(self, directory: str | None = None) -> list[dict]:
        return self._request("GET", "/api/permission/request", directory=directory) or []

    def reply_permission(
        self,
        session_id: str,
        request_id: str,
        decision: str,
        message: str | None = None,
    ) -> dict:
        if decision not in (PERMISSION_ONCE, PERMISSION_ALWAYS, PERMISSION_REJECT):
            raise ValueError(f"Invalid permission decision: {decision}")
        body: dict = {"decision": decision}
        if message:
            body["message"] = message
        return self._request(
            "POST",
            f"/api/session/{session_id}/permission/{request_id}/reply",
            body=body,
        )

    # ------------------------------------------------------------ filesystem

    def run_shell(self, command: str, cwd: str, directory: str | None = None) -> dict:
        return self._request(
            "POST",
            "/api/shell",
            body={"command": command, "cwd": cwd, "timeout": 120},
            directory=directory,
        )

    def shell_output(self, shell_id: str, directory: str | None = None) -> dict:
        return self._request("GET", f"/api/shell/{shell_id}/output", directory=directory)

    def list_shells(self, directory: str | None = None) -> list[dict]:
        return self._request("GET", "/api/shell", directory=directory) or []

    # ------------------------------------------------------------------- vcs

    def vcs_status(self, directory: str) -> list[dict]:
        return self._request("GET", "/api/vcs/status", directory=directory) or []

    def vcs_diff(self, directory: str) -> str:
        data = self._request("GET", "/api/vcs/diff", directory=directory)
        return data if isinstance(data, str) else str(data)

    def worktrees(self, directory: str) -> list[dict]:
        return self._request("GET", "/api/worktree", directory=directory) or []

    # ----------------------------------------------------------------- misc

    def create_worktree(self, directory: str, name: str) -> dict:
        return self._request(
            "POST", "/api/worktree", params={"directory": directory}, body={"name": name}
        )

    def remove_worktree(self, directory: str, name: str) -> dict:
        return self._request(
            "DELETE",
            "/api/worktree",
            params={"directory": directory, "name": name},
        )

    def websearch(self, query: str, directory: str | None = None) -> Any:
        return self._request(
            "POST", "/api/websearch", body={"query": query}, directory=directory
        )

    # --------------------------------------------------------------- waiting

    @staticmethod
    def assistant_text(messages: list[dict], since: int) -> str | None:
        """Every assistant text fragment of the turn after *since*, joined.

        One turn is routinely several assistant messages: the agent narrates,
        calls a tool, narrates again, and so on. Returning only the first match
        discarded the rest of the reply, which is how a turn could end up
        showing one stray sentence instead of the answer.

        Fragments are ordered by creation time rather than by the order the
        server returned them. The caller may ask for ``desc`` or ``asc``, and
        reading the newest-first list in place produced the reply backwards.
        """
        qualifying = [
            message
            for message in messages
            if message.get("type") == "assistant"
            and (message.get("time") or {}).get("created", 0) > since
        ]
        qualifying.sort(key=lambda m: (m.get("time") or {}).get("created", 0))

        chunks = []
        for message in qualifying:
            text = "".join(
                block.get("text", "")
                for block in message.get("content", [])
                if block.get("type") == "text"
            ).strip()
            if text:
                chunks.append(text)
        return "\n\n".join(chunks) if chunks else None

    @staticmethod
    def latest_assistant_text(messages: list[dict], since: int) -> str | None:
        """Kept for callers that genuinely want one fragment: the newest one."""
        fragments = OpenCode.assistant_text(messages, since)
        if fragments is None:
            return None
        return fragments.rsplit("\n\n", 1)[-1]

    @staticmethod
    def _model_ref(model: str) -> dict:
        """Accept ``provider/model`` or ``provider/model#variant``."""
        variant = None
        if "#" in model:
            model, variant = model.split("#", 1)
        provider, _, model_id = model.partition("/")
        if not model_id:
            raise ValueError(
                f"Model must look like provider/model, got {model!r}. "
                "Use /models to list them."
            )
        ref = {"providerID": provider, "id": model_id}
        if variant:
            ref["variant"] = variant
        return ref
