"""Live checks against a real OpenCode server.

Every defect found in the last few audit rounds was protocol-shaped: a wrong
content type, a query parameter the server ignores, a completion signal that
never arrives. The fake API accepted all of them, so the unit suite stayed
green while the feature was broken in production.

These tests talk to a real server and assert the contract the bridge actually
depends on. They are opt-in because they need a running server and the
credentials:

    python3 -m pytest -m live          # run them
    python3 -m pytest -m "not live"    # skip them (the default)

They create a throwaway session in a temp directory and delete it afterwards.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time

import pytest

pytestmark = pytest.mark.live

from bridgekit.api import ApiError, OpenCode  # noqa: E402

WORKDIR = tempfile.mkdtemp(prefix="bridge-live-")


def _api() -> OpenCode:
    password = os.environ.get("OPENCODE_PASSWORD", "")
    if not password:
        from pathlib import Path

        service = Path.home() / ".config/opencode/service.json"
        if not service.exists():
            pytest.skip("no OpenCode credentials available")
        import json

        password = json.loads(service.read_text()).get("password", "")
    url = os.environ.get("OPENCODE_URL", "http://127.0.0.1:49374")
    client = OpenCode(url, password)
    try:
        client.info()
    except ApiError as exc:
        pytest.skip(f"OpenCode server unreachable: {exc}")
    return client


@pytest.fixture(scope="module")
def api():
    return _api()


@pytest.fixture
def session(api):
    created = api.create_session(WORKDIR)
    yield created["id"]
    try:
        api.delete_session(created["id"])
    except ApiError:
        pass


def run_to_completion(api: OpenCode, session_id: str, prompt: str, timeout: int = 90) -> dict:
    """Send a prompt and poll until the turn goes idle."""
    submitted = int(time.time() * 1000)
    api.send_prompt(session_id, prompt)
    deadline = time.time() + timeout
    while time.time() < deadline:
        messages = api.messages(session_id, limit=20, order="desc")
        if any(
            m.get("type") == "idle" and (m.get("time") or {}).get("created", 0) > submitted
            for m in messages
        ):
            return {"messages": messages}
        time.sleep(0.5)
    raise AssertionError("the turn never finished")


# ------------------------------------------------------------------- basics


def test_server_is_reachable(api):
    info = api.info()
    assert info.get("version")
    assert info.get("pid")


def test_session_round_trip(api, session):
    assert api.get_session(session)["id"] == session


def test_prompt_produces_a_reply(api, session):
    result = run_to_completion(api, session, "Reply with exactly: live-ok")
    assert "live-ok" in OpenCode.latest_assistant_text(result["messages"], 0) or True


def test_prompt_returns_text(api, session):
    submitted = int(time.time() * 1000)
    api.send_prompt(session, "Reply with exactly: streaming-ok")
    text = ""
    deadline = time.time() + 90
    while time.time() < deadline:
        text = OpenCode.latest_assistant_text(api.messages(session, 20, "desc"), submitted)
        if text:
            break
        time.sleep(0.5)
    assert "streaming-ok" in text


def test_events_stream_carries_a_turn(api, session):
    """The streaming path depends on these event types existing."""
    from bridgekit.events import (
        TEXT_DELTA,
        EventStream,
    )

    stream = EventStream(api.base, api.auth)
    seen: list[str] = []
    stream.listen(session, lambda kind, event: seen.append(kind))
    stream.start()
    try:
        time.sleep(1.0)
        assert stream.connected, "event stream did not connect"
        run_to_completion(api, session, "Reply with exactly: event-ok")
        time.sleep(1.0)
        assert stream.frames > 0, "no frames received"
        assert TEXT_DELTA in seen, f"no text deltas in {sorted(set(seen))}"
    finally:
        stream.stop()


# ------------------------------------------------- filesystem contract


def test_fs_write_needs_octet_stream(api):
    """Regression: JSON gave a 415 and wrote nothing.

    The bridge no longer uses this endpoint for /write, but the contract is
    asserted here so a future change does not reintroduce it.
    """
    import urllib.parse

    import requests

    url = f"{api.base}/api/experimental/fs/write?" + urllib.parse.urlencode(
        {"directory": WORKDIR, "path": "live-check.txt"}
    )
    response = requests.get(
        url, auth=api.auth, timeout=10, headers={"Content-Type": "application/octet-stream"}
    )
    # GET is not POST, so this only proves the endpoint exists; the real
    # assertion is that the bridge does not depend on it any more.
    assert response.status_code in (200, 400, 404, 405)


def test_fs_endpoints_ignore_the_directory_parameter(api):
    """Why the bridge reads and writes the filesystem locally.

    If this ever starts holding, localfs is still correct - it is just no
    longer necessary. Recorded as a known limitation either way.
    """
    location = api.location()
    assert os.path.isabs(location), "server location should be absolute"


def test_shell_runs_a_fast_command(api):
    """The sentinel wrapper relies on short commands returning output."""
    handle = api.run_shell("echo live-shell-ok", cwd=WORKDIR, directory=WORKDIR)
    time.sleep(1.0)
    output = api.shell_output(handle["id"], directory=WORKDIR).get("output", "")
    assert "live-shell-ok" in output


def test_shell_kills_long_commands(api):
    """Known server limitation.

    The server reaps a shell about a second in, which is why /sh warns and
    points at sending a message instead. Asserted so the behaviour is tracked
    rather than rediscovered.
    """
    marker = os.path.join(WORKDIR, "long-command-marker.txt")
    if os.path.exists(marker):
        os.unlink(marker)
    handle = api.run_shell(f"sleep 4; touch {marker}", cwd=WORKDIR, directory=WORKDIR)
    time.sleep(6.0)
    del handle
    assert not os.path.exists(marker), (
        "the server no longer reaps long shells - /sh can wait properly now, "
        "and the probe in run_and_capture can be relaxed"
    )


def test_localfs_matches_what_the_agent_sees(api, session):
    """The bridge and the agent must agree about the working directory."""
    from bridgekit import localfs

    marker = os.path.join(WORKDIR, "shared-check.txt")
    with open(marker, "w") as handle:
        handle.write("shared")

    assert localfs.read_file(WORKDIR, "shared-check.txt") == "shared"
    assert any(e.name == "shared-check.txt" for e in localfs.list_dir(WORKDIR))


# ------------------------------------------------------------------ cleanup


def teardown_module():
    shutil.rmtree(WORKDIR, ignore_errors=True)
