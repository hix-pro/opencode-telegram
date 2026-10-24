"""In-product self-test.

The unit suite runs against a fake API, and a fake accepts things the real
server rejects. These checks run against the live server so ``/selftest`` can
answer "is the thing actually working right now?" from a phone, without a
terminal.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .api import ApiError, OpenCode


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def render(self) -> str:
        lines = [
            f"{'PASS' if r.ok else 'FAIL'}  {r.name}"
            + (f"  -  {r.detail}" if r.detail else "")
            for r in self.results
        ]
        lines.append("")
        lines.append(
            f"{self.passed} passed, {self.failed} failed"
            if self.failed
            else f"All {self.passed} checks passed."
        )
        return "\n".join(lines)


def _timed(name: str, func) -> Result:
    started = time.time()
    try:
        detail = func() or ""
        return Result(name, True, str(detail), time.time() - started)
    except Exception as exc:
        return Result(name, False, f"{type(exc).__name__}: {exc}"[:90], time.time() - started)


def run(api: OpenCode, directory: str | None = None) -> Report:
    """Exercise the paths the bridge actually depends on."""
    report = Report()
    # A throwaway workspace is only used when the caller did not name one.
    created_here = directory is None
    workdir = directory or tempfile.mkdtemp(prefix="bridge-selftest-")
    session_id = None

    try:

        def reachable():
            return f"v{api.info().get('version')}"

        report.results.append(_timed("Server reachable", reachable))

        def make_session():
            nonlocal session_id
            created = api.create_session(workdir)
            session_id = created["id"]
            return created["id"]

        report.results.append(_timed("Create session", make_session))

        # Everything after this point needs the real session id; make_session
        # already succeeded as a timed result, and a missing id is a hard
        # failure of the selftest, not something to probe with None.
        if not session_id:
            raise AssertionError("the server returned no session id")
        sid = str(session_id)

        def turn():
            submitted = int(time.time() * 1000)
            api.send_prompt(sid, "Reply with exactly: selftest-ok")
            deadline = time.time() + 120
            text = ""
            while time.time() < deadline:
                text = OpenCode.assistant_text(api.messages(sid, 20, "desc"), submitted)
                if text:
                    elapsed = time.time() - submitted / 1000
                    return f"replied in {max(1, int(elapsed + 0.5))}s"
                time.sleep(0.5)
            raise TimeoutError("the turn never finished")

        report.results.append(_timed("Prompt round-trip", turn))

        report.results.append(_timed("Event stream deltas", lambda: _probe_events(api, sid)))

        def filesystem():
            from . import localfs

            marker = Path(workdir) / "selftest.txt"
            marker.write_text("selftest")
            # raises, not assert: an optimization run (-O) strips asserts and
            # would pass this selftest with a broken filesystem bridge.
            if localfs.read_file(workdir, "selftest.txt") != "selftest":
                raise AssertionError("the file read back differently than it was written")
            if not any(e.name == "selftest.txt" for e in localfs.list_dir(workdir)):
                raise AssertionError("the file was not visible to list_dir")
            marker.unlink(missing_ok=True)
            return "read/write/list agree with the agent"

        report.results.append(_timed("Local filesystem", filesystem))

        def confinement():
            from . import localfs
            from .localfs import PathError

            try:
                localfs.read_file(workdir, "/etc/passwd", (workdir,))
            except PathError:
                return "traversal refused"
            raise AssertionError("a path outside the allowed roots was readable")

        report.results.append(_timed("Path confinement", confinement))

        def shell():
            handle = api.run_shell("echo selftest-shell", cwd=workdir, directory=workdir)
            time.sleep(1.0)
            out = api.shell_output(handle["id"], directory=workdir).get("output", "")
            if "selftest-shell" not in out:
                raise AssertionError(f"no output: {out!r}")
            return "short commands return output"

        report.results.append(_timed("Shell execution", shell))

    finally:
        if session_id:
            try:
                api.delete_session(session_id)
            except ApiError:
                pass
        if created_here:
            shutil.rmtree(workdir, ignore_errors=True)

    return report


# How long to wait for a streamed turn. Injectable so tests do not have to
# spend the real duration proving that a failure is detected.
EVENT_TIMEOUT = 90.0
CONNECT_TIMEOUT = 10.0


def _probe_events(api: OpenCode, session_id: str) -> str:
    """Drive a real turn and require text deltas to arrive over the stream.

    This is the check the bridge's whole live-streaming feature rests on. It
    has to run a turn *while* listening - checking that the stream connects is
    not the same as checking that it carries the events streaming depends on,
    and a check that cannot fail is worse than no check at all.
    """
    from .events import TEXT_DELTA, EventStream

    stream = EventStream(api.base, api.auth)
    seen: list[str] = []
    stream.listen(session_id, lambda kind, event: seen.append(kind))
    stream.start()
    try:
        deadline = time.time() + CONNECT_TIMEOUT
        while time.time() < deadline and not stream.connected:
            time.sleep(0.2)
        if not stream.connected:
            raise ConnectionError("could not connect to /api/event")

        api.send_prompt(session_id, "Reply with exactly: selftest-stream")

        deadline = time.time() + EVENT_TIMEOUT
        while time.time() < deadline:
            if TEXT_DELTA in seen:
                return f"{seen.count(TEXT_DELTA)} delta(s) over {stream.frames} frame(s)"
            time.sleep(0.25)

        raise AssertionError(f"no {TEXT_DELTA} arrived; saw {sorted(set(seen)) or 'nothing'}")
    finally:
        stream.stop()
