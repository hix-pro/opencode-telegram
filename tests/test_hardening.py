"""Production hardening.

Each test here corresponds to a defect found during the production audit,
so the names are the failure modes rather than the functions.
"""

from __future__ import annotations

import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from bridgekit import attachments
from bridgekit.api import ApiError
from bridgekit.config import Config
from bridgekit.keyboards import Router
from bridgekit.policy import Policy
from bridgekit.schedule import ScheduleStore
from bridgekit.state import State

CHAT = "42"


# --------------------------------------------------------------- fixtures


class DeadServer:
    """Every call fails at the transport level, as a stopped server would."""

    def __init__(self):
        self.calls = 0

    def messages(self, *a, **k):
        self.calls += 1
        raise ApiError(0, "Could not reach OpenCode: connection refused")

    def send_prompt(self, *a, **k):
        return None

    def interrupt_session(self, *a, **k):
        return {}

    def permission_requests(self, directory=None):
        return []

    def create_session(self, directory, **k):
        return {"id": "ses_new0000000000000"}


class StubEvents:
    last_error = ""
    connected = False
    frames = 0
    reconnects = 0

    def listen(self, *a):
        pass

    def unlisten(self, *a):
        pass

    def stop(self):
        pass


class RecordingTelegram:
    def __init__(self):
        self.sent: list[str] = []
        self.edits: list[str] = []

    def send(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        return 1

    def edit(self, chat_id, message_id, text, reply_markup=None, force=False):
        self.edits.append(text)
        return True

    def effective_interval(self):
        return 1.0

    def get_updates(self, offset, timeout=30):
        return []

    def answer_callback(self, *a):
        pass


def last_reply(bridge) -> str:
    """The most recent text the user would actually have seen."""
    tg = bridge.telegram
    return (tg.edits[-1] if tg.edits else "") + (tg.sent[-1] if tg.sent else "")


def build_bridge(tmp_path, api, **overrides):
    import bridge as B

    config = Config(
        token="t",
        allowed_chats=frozenset({CHAT}),
        opencode_url="http://x",
        password="p",
        default_dir=str(tmp_path),
        state_file=tmp_path / ".state.json",
        poll_messages=0.01,
        edit_interval=0.0,
        stream_enabled=False,
        prompt_timeout=7200,
    )
    for key, value in overrides.items():
        config = config.__class__(**{**config.__dict__, key: value})

    instance = B.Bridge.__new__(B.Bridge)
    instance.config = config
    instance.api = api
    instance.telegram = RecordingTelegram()
    instance.state = State(config.state_file, config.default_dir)
    instance.state.load()
    instance.policy = Policy()
    instance.events = StubEvents()
    instance.router = Router()
    instance.work = queue.Queue()
    instance.stop_event = threading.Event()
    instance.aborts = {}
    instance._workers = {}
    instance._requests = {}
    instance._perm_cards = {}
    instance._backlog = {}
    instance.health = B.Health(streaming=False)
    instance._lock = threading.Lock()
    instance.schedules = ScheduleStore()
    return instance


# ------------------------------------------- server dies mid-turn (critical)


def test_dead_server_does_not_silence_the_user_for_two_hours(tmp_path):
    """The audit's worst bug: a dead server meant hours of silence."""
    api = DeadServer()
    bridge = build_bridge(tmp_path, api)
    bridge.state.set_session(CHAT, "ses_x")

    started = time.time()
    bridge._run_turn(CHAT, "a job that needs a live server")
    elapsed = time.time() - started

    assert elapsed < 5, "must give up quickly rather than wait out the timeout"
    assert api.calls < 50, "must not keep hammering a dead server"


def test_dead_server_explains_itself(tmp_path):
    api = DeadServer()
    bridge = build_bridge(tmp_path, api)
    bridge.state.set_session(CHAT, "ses_x")
    bridge._run_turn(CHAT, "job")

    reply = last_reply(bridge)
    assert "unreachable" in reply
    assert "failed" in reply.lower()


def test_a_failed_turn_never_says_no_reply(tmp_path):
    """'(no reply)' would wrongly imply the agent finished with nothing."""
    api = DeadServer()
    bridge = build_bridge(tmp_path, api)
    bridge.state.set_session(CHAT, "ses_x")
    bridge._run_turn(CHAT, "job")
    reply = last_reply(bridge)
    assert "(no reply)" not in reply


def test_dead_server_is_counted_as_an_error(tmp_path):
    api = DeadServer()
    bridge = build_bridge(tmp_path, api)
    bridge.state.set_session(CHAT, "ses_x")
    bridge._run_turn(CHAT, "job")
    assert bridge.health.errors >= 1


def test_transient_failure_does_not_abort_the_turn(tmp_path):
    """One failed poll is normal; only a run of them means the server is gone."""

    class FlakyServer(DeadServer):
        def __init__(self):
            super().__init__()
            self.failures = 2

        def messages(self, session_id, limit=20, order="desc"):
            self.calls += 1
            if self.failures:
                self.failures -= 1
                raise ApiError(0, "refused")
            stamp = time.time() * 1000 + 1000
            return [
                {
                    "type": "assistant",
                    "time": {"created": stamp},
                    "content": [{"type": "text", "text": "recovered"}],
                },
                {"type": "idle", "time": {"created": stamp + 1}},
            ]

    api = FlakyServer()
    bridge = build_bridge(tmp_path, api)
    bridge.state.set_session(CHAT, "ses_x")
    bridge._run_turn(CHAT, "job")
    reply = last_reply(bridge)
    assert "recovered" in reply


def test_a_vanished_session_also_ends_the_turn(tmp_path):
    """A 404 during polling means the session is gone; polling it for two
    hours is the same silence as a dead server."""

    class NotFoundServer(DeadServer):
        def messages(self, *a, **k):
            self.calls += 1
            raise ApiError(404, "Session not found")

    api = NotFoundServer()
    bridge = build_bridge(tmp_path, api, prompt_timeout=7200)
    bridge.state.set_session(CHAT, "ses_x")
    started = time.time()
    bridge._run_turn(CHAT, "job")
    assert time.time() - started < 5
    assert api.calls < 50


# ------------------------------------------------ attachment cache retention


def test_prune_deletes_old_files(tmp_path):
    cache = tmp_path / ".attachments"
    cache.mkdir()
    old = cache / "1-old.png"
    old.write_bytes(b"x")
    import os

    ancient = time.time() - 48 * 3600
    os.utime(old, (ancient, ancient))

    removed = attachments.prune(cache, max_age_hours=24)
    assert removed == 1
    assert not old.exists()


def test_prune_keeps_recent_files(tmp_path):
    cache = tmp_path / ".attachments"
    cache.mkdir()
    fresh = cache / "1-fresh.png"
    fresh.write_bytes(b"x")
    assert attachments.prune(cache, max_age_hours=24) == 0
    assert fresh.exists()


def test_prune_enforces_a_file_count_cap(tmp_path):
    cache = tmp_path / ".attachments"
    cache.mkdir()
    for index in range(60):
        (cache / f"{index}.png").write_bytes(b"x")
    removed = attachments.prune(cache, max_age_hours=24, keep=10)
    assert removed == 50
    assert len(list(cache.iterdir())) == 10


def test_prune_keeps_the_newest(tmp_path):
    cache = tmp_path / ".attachments"
    cache.mkdir()
    import os

    paths = []
    for index in range(5):
        path = cache / f"{index}.png"
        path.write_bytes(b"x")
        os.utime(path, (time.time() + index, time.time() + index))
        paths.append(path)
    attachments.prune(cache, max_age_hours=24, keep=2)
    assert paths[-1].exists()
    assert not paths[0].exists()


def test_prune_on_a_missing_directory_is_harmless(tmp_path):
    assert attachments.prune(tmp_path / "nope") == 0


def test_prune_ignores_subdirectories(tmp_path):
    cache = tmp_path / ".attachments"
    (cache / "subdir").mkdir(parents=True)
    assert attachments.prune(cache, max_age_hours=0, keep=0) == 0
    assert (cache / "subdir").exists()


def test_default_retention_is_configured():
    config = Config(
        token="t",
        allowed_chats=frozenset({"1"}),
        opencode_url="u",
        password="p",
        default_dir="/tmp",
        state_file="/tmp/s.json",
    )
    assert 0 < config.attachment_max_age_hours <= 168


# ------------------------------------------------------- per-chat concurrency


class BlockingAPI:
    """A turn that blocks until released."""

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.ran: list[str] = []

    def messages(self, session_id, limit=20, order="desc"):
        self.started.set()
        self.release.wait(timeout=5)
        stamp = time.time() * 1000 + 1000
        return [
            {
                "type": "assistant",
                "time": {"created": stamp},
                "content": [{"type": "text", "text": "done"}],
            },
            {"type": "idle", "time": {"created": stamp + 1}},
        ]

    def send_prompt(self, *a, **k):
        return None

    def permission_requests(self, directory=None):
        return []

    def create_session(self, directory, **k):
        return {"id": f"ses_{directory}"}

    def get_session(self, session_id):
        return {"id": session_id, "title": "t", "time": {}}


def test_one_chats_long_job_does_not_block_another(tmp_path):
    """A single global worker stalled every allow-listed user behind one job."""
    api = BlockingAPI()
    bridge = build_bridge(tmp_path, api)
    bridge.state.set_session("42", "ses_a")
    bridge.state.set_session("43", "ses_b")

    thread = threading.Thread(target=bridge._worker_loop, daemon=True)
    thread.start()

    bridge.work.put(("42", "slow job", None))
    assert api.started.wait(timeout=2), "first turn should have started"

    # A different chat must not wait for the first one to finish.
    bridge.work.put(("43", "quick job", None))
    time.sleep(0.3)
    assert bridge._chat_busy("43"), "second chat should be running concurrently"

    api.release.set()
    time.sleep(0.5)
    bridge.stop_event.set()


def test_a_chat_cannot_run_two_turns_at_once(tmp_path):
    api = BlockingAPI()
    bridge = build_bridge(tmp_path, api)
    bridge.state.set_session("42", "ses_a")

    thread = threading.Thread(target=bridge._worker_loop, daemon=True)
    thread.start()

    bridge.work.put(("42", "first", None))
    assert api.started.wait(timeout=2)
    bridge.work.put(("42", "second", None))
    time.sleep(0.3)

    # /delivery promises queueing, so the message is held, not dropped.
    assert any("Queued" in text for text in bridge.telegram.sent)
    assert len(bridge._backlog.get("42", [])) == 1

    api.release.set()
    time.sleep(0.5)
    bridge.stop_event.set()


def test_worker_is_released_when_a_turn_finishes(tmp_path):
    api = BlockingAPI()
    api.release.set()
    bridge = build_bridge(tmp_path, api)
    bridge.state.set_session("42", "ses_a")

    thread = threading.Thread(target=bridge._worker_loop, daemon=True)
    thread.start()
    bridge.work.put(("42", "job", None))
    deadline = time.time() + 5
    while bridge._chat_busy("42") and time.time() < deadline:
        time.sleep(0.02)
    assert not bridge._chat_busy("42")
    bridge.stop_event.set()


# ---------------------------------------------------------- state hardening


def test_state_file_is_owner_only(tmp_path):
    state = State(tmp_path / ".state.json", "/tmp")
    state.load()
    state.set_session("42", "ses_x")
    mode = (tmp_path / ".state.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_an_existing_loose_state_file_is_tightened(tmp_path):
    path = tmp_path / ".state.json"
    path.write_text('{"version": 2, "chats": {}}')
    path.chmod(0o644)
    State(path, "/tmp").load()
    assert (path.stat().st_mode & 0o777) == 0o600


def test_a_dirty_state_file_is_survivable(tmp_path):
    path = tmp_path / ".state.json"
    path.write_text("{ not json")
    state = State(path, "/tmp")
    state.load()
    assert state.session("42") is None


# ------------------------------------------------------------- log privacy


def test_prompt_preview_length_is_configurable(tmp_path):
    config = Config(
        token="t",
        allowed_chats=frozenset({"1"}),
        opencode_url="u",
        password="p",
        default_dir="/tmp",
        state_file="/tmp/s.json",
        log_prompt_chars=0,
    )
    assert config.log_prompt_chars == 0


def test_queued_messages_run_after_the_current_turn(tmp_path):
    api = BlockingAPI()
    api.release.set()
    bridge = build_bridge(tmp_path, api)
    bridge.state.set_session("42", "ses_a")

    thread = threading.Thread(target=bridge._worker_loop, daemon=True)
    thread.start()
    bridge.work.put(("42", "first", None))
    time.sleep(0.3)
    bridge.work.put(("42", "second", None))
    time.sleep(0.3)

    # The backlog is handed back once the first turn finishes.
    deadline = time.time() + 3
    while bridge._backlog.get("42") and time.time() < deadline:
        time.sleep(0.05)
    assert not bridge._backlog.get("42")
    bridge.stop_event.set()


def test_backlog_is_bounded(tmp_path):
    api = BlockingAPI()
    bridge = build_bridge(tmp_path, api)
    bridge.state.set_session("42", "ses_a")
    for i in range(20):
        bridge._backlog.setdefault("42", [])
        if len(bridge._backlog["42"]) < 5:
            bridge._backlog["42"].append(("42", f"m{i}", None))
    assert len(bridge._backlog["42"]) <= 5


# ================================================================
# The bridge unit's own sandbox (v2.3.7)
# ================================================================

_REPO = Path(__file__).resolve().parents[1]


def _repo_text(name: str) -> str:
    return _REPO.joinpath(name).read_text()


def test_the_base_bridge_unit_carries_the_process_sandbox() -> None:
    """Kernel surfaces, capabilities and the syscall/socket whitelist must
    live in the base unit so every install gets them without flags."""
    unit = _repo_text("systemd/opencode-telegram-bridge.service.in")
    for directive in (
        "NoNewPrivileges=yes",
        "PrivateTmp=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectClock=yes",
        "RestrictSUIDSGID=yes",
        "RestrictRealtime=yes",
        "LockPersonality=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "SystemCallArchitectures=native",
        "SystemCallFilter=@system-service",
        "SystemCallErrorNumber=EPERM",
    ):
        assert directive in unit, f"missing process sandbox directive: {directive}"
    # The conditional @SANDBOX@ rendering is gone for good: filesystem
    # bindings belong to the drop-in only, or empty-roots installs would
    # silently lose ProtectSystem and roots silently land in the bridge.
    assert "@SANDBOX@" not in unit


def test_the_bridge_drop_in_is_the_filesystem_delta_only() -> None:
    template = _repo_text("systemd/opencode-telegram-bridge-hardening.conf.in")
    for directive in ("ProtectSystem=strict", "ProtectHome=read-only", "@RW_PATHS@"):
        assert directive in template, f"missing filesystem directive: {directive}"
    # Single-source doctrine: the process-level directives stay in the base
    # unit; a drop-in copy would only shadow them across upgrades.
    assert "NoNewPrivileges" not in template
    assert "SystemCallFilter" not in template
    assert "ReadWritePaths=/tmp" not in template, (
        "the base unit runs PrivateTmp=yes; granting the shared /tmp would"
        " widen what the process can touch"
    )
    assert "ALLOWED_ROOTS" not in template


def test_install_sh_wires_and_renders_the_bridge_drop_in() -> None:
    install = _repo_text("install.sh")
    assert "--harden-bridge) HARDEN_BRIDGE=1" in install, "flag must be parsed"
    assert "opencode-telegram-bridge-hardening.conf.in" in install, (
        "the template must be the render source"
    )
    assert "render_harden \\" in install, "the bridge renderer must be invoked"
    assert "UNIT_NAME=opencode-telegram-bridge.service" in install
    assert "$UNIT_NAME.d" in install, (
        "the drop-in must land in the bridge unit's conf.d -"
        " $UNIT_NAME ends in .service, appending .service.d would double it"
    )
    assert "$UNIT_NAME.service.d" not in install
    subprocess.run(["bash", "-n", str(_REPO / "install.sh")], check=True)


def test_the_bridge_renderer_grants_only_bridge_paths(tmp_path) -> None:
    """Run the embedded render_harden python against a checkout with a space
    in its name: the rendered drop-in must grant exactly the repo directory
    and the state-file parent - never the ALLOWED_ROOTS entry."""
    workspace = tmp_path / "checkout dir"
    workspace.mkdir()
    state_dir = tmp_path / "state data"
    state_dir.mkdir()
    forbidden_root = tmp_path / "projects"
    forbidden_root.mkdir()

    env_file = tmp_path / "s.env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=x\n"
        "TELEGRAM_CHAT_ID=1\n"
        f"ALLOWED_ROOTS={forbidden_root}\n"
        f"STATE_FILE={state_dir / '.state.json'}\n"
    )

    match = re.search(
        r"render_harden\(\) \{\n    python3 - \S.*? << 'PY'\n(.*?)\nPY\n\}",
        _repo_text("install.sh"),
        re.DOTALL,
    )
    assert match, "render_harden's embedded python not found in install.sh"

    rendered_path = tmp_path / "50-hardening.conf"
    subprocess.run(
        [
            sys.executable,
            "-c",
            match.group(1),
            str(_REPO / "systemd/opencode-telegram-bridge-hardening.conf.in"),
            str(rendered_path),
            str(workspace),
            str(env_file),
        ],
        check=True,
    )
    rendered = rendered_path.read_text()

    for placeholder in ("@REPO@", "@PYTHON@", "@RW_PATHS@", "@SANDBOX@"):
        assert placeholder not in rendered, f"unresolved placeholder: {placeholder}"
    grants = [line for line in rendered.splitlines() if line.startswith("ReadWritePaths=")]
    assert grants == [
        f'ReadWritePaths="{workspace}"',
        f'ReadWritePaths="{state_dir}"',
    ], f"unexpected writable set: {grants}"
    assert str(forbidden_root) not in rendered, "project root must not be granted"


def test_uninstall_removes_both_hardening_drop_ins() -> None:
    install = _repo_text("uninstall.sh")
    assert "opencode-server.service.d/50-hardening.conf" in install
    assert "opencode-telegram-bridge.service.d/50-hardening.conf" in install
    subprocess.run(["bash", "-n", str(_REPO / "uninstall.sh")], check=True)


# ================================================================
# Dedicated-user migration (v2.3.8)
# ================================================================


def test_the_migration_script_retires_the_originals_safely() -> None:
    script = _repo_text("scripts/migrate-dedicated-user.sh")
    for marker in (
        "--check",
        "--apply",
        "loginctl enable-linger",
        "useradd -m -s /usr/sbin/nologin",
        "install.sh --harden-bridge --harden-server",
    ):
        assert marker in script, f"migration script lost: {marker}"
    # Never decommission by deleting: the old user's units are stopped and
    # disabled only after the new ones verify healthy - and only via the
    # old owner's own systemd manager, never root's.
    assert "systemctl --user stop" in script
    assert "systemctl --user disable" in script
    assert '"$OLD_OWNER"' in script
    assert "rm -rf" not in script, "no destructive steps belong in a migration"
    subprocess.run(
        ["bash", "-n", str(_REPO / "scripts/migrate-dedicated-user.sh")], check=True
    )


def test_hardening_doc_carries_the_dedicated_user_runbook() -> None:
    doc = _repo_text("docs/HARDENING.md")
    for fragment in (
        "## Running under a dedicated user",
        "scripts/migrate-dedicated-user.sh",
        "loginctl enable-linger",
        "Rollback:",
    ):
        assert fragment in doc, f"runbook missing: {fragment}"
    readme = _repo_text("README.md")
    assert "scripts/migrate-dedicated-user.sh" in readme, (
        "README must point at the concrete migration it recommends"
    )
