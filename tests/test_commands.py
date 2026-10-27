"""Command parsing, dispatch, and handlers."""

from __future__ import annotations

import pathlib
import time

import pytest

from bridgekit.api import ApiError
from bridgekit.commands import REGISTRY, CommandError, parse, run

# --------------------------------------------------------------------- parsing


def test_plain_text_is_not_a_command():
    assert parse("just a prompt") is None


def test_command_with_no_args():
    assert parse("/help") == ("help", "", "")


def test_command_with_args():
    assert parse("/model opencode/claude-haiku-5-5") == (
        "model",
        "opencode/claude-haiku-5-5",
        "",
    )


def test_command_keeps_remaining_lines_separately():
    name, args, rest = parse("/write notes.md\nline one\nline two")
    assert (name, args) == ("write", "notes.md")
    assert rest == "line one\nline two"


def test_bot_suffix_is_stripped():
    assert parse("/help@my_bot")[0] == "help"


def test_case_is_normalised():
    assert parse("/HELP")[0] == "help"


def test_extra_whitespace_is_trimmed():
    assert parse("/title   my new name   ")[1] == "my new name"


# -------------------------------------------------------------------- dispatch


def test_unknown_command_suggests_a_near_match(ctx):
    with pytest.raises(CommandError, match="Did you mean /help"):
        run(ctx, "/halp")


def test_unknown_command_without_match_lists_help(ctx):
    with pytest.raises(CommandError, match="Send /help"):
        run(ctx, "/zzzzqqqq")


def test_non_command_returns_none_so_it_becomes_a_prompt(ctx):
    assert run(ctx, "refactor the parser") is None


def test_start_is_alias_for_help(ctx):
    assert "/Session --" in run(ctx, "/start") or "Session" in run(ctx, "/start")


def test_every_registered_command_documents_itself():
    for name, cmd in REGISTRY.items():
        assert cmd.summary, f"/{name} has no summary"
        assert cmd.usage.startswith("/"), f"/{name} usage should start with /"


# --------------------------------------------------------------------- session


def test_new_creates_and_binds_a_session(ctx):
    reply = run(ctx, "/new")
    assert "ses_created0000000000" in reply
    assert ctx.state.session("42") == "ses_created0000000000"


def test_new_accepts_a_directory(ctx, workdir):
    run(ctx, f"/new {workdir}")
    assert ctx.state.directory("42") == str(workdir.resolve())


def test_new_sets_a_title_from_the_remainder(ctx):
    run(ctx, "/new /tmp Debugging the bridge")
    assert ctx.api.sessions[0]["title"] == "Debugging the bridge"


def test_new_rejects_a_directory_outside_the_allow_list(ctx, config, tmp_path):
    object.__setattr__(config, "allowed_roots", (str(tmp_path / "nope"),))
    with pytest.raises(CommandError):
        run(ctx, "/new /etc")


def test_sessions_lists_with_the_active_one_marked(ctx):
    ctx.state.set_session("42", "ses_aaaaaaaaaaaaaaaa")
    reply = run(ctx, "/sessions")
    assert "First session" in reply
    assert "* ses_aaaaaa" in reply


def test_sessions_accepts_a_limit(ctx):
    assert run(ctx, "/sessions 1").count("session") >= 1


def test_use_switches_by_exact_id(ctx):
    run(ctx, "/use ses_bbbbbbbbbbbbbbbb")
    assert ctx.state.session("42") == "ses_bbbbbbbbbbbbbbbb"


def test_use_switches_by_index(ctx):
    run(ctx, "/use #2")
    assert ctx.state.session("42") == "ses_bbbbbbbbbbbbbbbb"


def test_use_switches_by_prefix(ctx):
    run(ctx, "/use ses_bbbb")
    assert ctx.state.session("42") == "ses_bbbbbbbbbbbbbbbb"


def test_use_rejects_an_unknown_reference(ctx):
    with pytest.raises(CommandError, match="No session matching"):
        run(ctx, "/use ses_nope")


def test_use_requires_an_argument(ctx):
    with pytest.raises(CommandError, match="Usage"):
        run(ctx, "/use")


def test_use_rejects_out_of_range_index(ctx):
    with pytest.raises(CommandError, match="No session #99"):
        run(ctx, "/use #99")


def test_del_removes_the_session(ctx):
    run(ctx, "/del #1")
    assert ctx.api.sessions[0]["id"] == "ses_bbbbbbbbbbbbbbbb"


def test_del_clears_state_when_deleting_the_active_session(ctx):
    ctx.state.set_session("42", "ses_aaaaaaaaaaaaaaaa")
    reply = run(ctx, "/del ses_aaaaaaaaaaaaaaaa")
    assert ctx.state.session("42") is None
    assert "was the active session" in reply


def test_title_renames(ctx):
    ctx.state.set_session("42", "ses_aaaaaaaaaaaaaaaa")
    assert "Bridge work" in run(ctx, "/title Bridge work")
    assert (
        "update_session",
        ("ses_aaaaaaaaaaaaaaaa", "Bridge work"),
        {},
    ) in ctx.api.calls


def test_commands_needing_a_session_say_so(ctx):
    for command in ("/info", "/compact", "/abort", "/model"):
        with pytest.raises(CommandError, match="No active session"):
            run(ctx, command)


def test_fork_rebinds_to_the_new_session(ctx):
    ctx.state.set_session("42", "ses_aaaaaaaaaaaaaaaa")
    run(ctx, "/fork")
    assert ctx.state.session("42") == "ses_forked00000000000"


# ----------------------------------------------------------------- model/agent


def test_model_switches(ctx):
    ctx.state.set_session("42", "ses_aaaaaaaaaaaaaaaa")
    run(ctx, "/model opencode/claude-haiku-5-5")
    assert (
        "switch_model",
        ("ses_aaaaaaaaaaaaaaaa", "opencode/claude-haiku-5-5"),
        {},
    ) in ctx.api.calls


def test_model_shows_current_when_no_argument(ctx):
    ctx.state.set_session("42", "ses_aaaaaaaaaaaaaaaa")
    assert "Model:" in run(ctx, "/model")


def test_agents_lists_visible_agents_only(ctx):
    reply = run(ctx, "/agents")
    assert "build" in reply and "plan" in reply
    assert "secret" not in reply


def test_models_are_listed_as_provider_pairs(ctx):
    reply = run(ctx, "/models")
    assert "opencode/claude-haiku-5-5" in reply


# ----------------------------------------------------------------------- files


def test_read_returns_a_fenced_block(ctx):
    (pathlib.Path(ctx.directory) / "notes.md").write_text("hello from disk")
    reply = run(ctx, "/read notes.md")
    assert "```" in reply and "hello from disk" in reply


def test_read_reports_a_missing_file(ctx):
    with pytest.raises(CommandError, match="No such file"):
        run(ctx, "/read missing.md")


def test_read_refuses_a_directory(ctx):
    (pathlib.Path(ctx.directory) / "adir").mkdir()
    with pytest.raises(CommandError, match="Not a file"):
        run(ctx, "/read adir")


def test_ls_marks_directories(ctx):
    (pathlib.Path(ctx.directory) / "sub").mkdir()
    (pathlib.Path(ctx.directory) / "notes.md").write_text("x")
    reply = run(ctx, "/ls")
    assert "sub/" in reply and "notes.md" in reply


def test_ls_reports_a_missing_directory(ctx):
    with pytest.raises(CommandError, match="No such directory"):
        run(ctx, "/ls nope")


def test_write_creates_the_file_where_expected(ctx, tmp_path):
    """Writes land in the working directory, not the server's own cwd."""
    run(ctx, "/write out.txt\nalpha\nbeta")
    written = tmp_path / "out.txt"
    assert written.exists()
    assert written.read_text() == "alpha\nbeta"


def test_write_creates_missing_parents(ctx, tmp_path):
    run(ctx, "/write nested/deep/out.txt\ncontent")
    assert (tmp_path / "nested" / "deep" / "out.txt").read_text() == "content"


def test_write_without_content_explains_itself(ctx):
    with pytest.raises(CommandError, match="Usage"):
        run(ctx, "/write out.txt")


def test_sh_runs_and_returns_output(ctx):
    reply = run(ctx, "/sh echo hi")
    assert "$ echo hi" in reply
    assert "hi" in reply
    # The command is wrapped in a sentinel so its final output cannot be lost.
    call = next(c for c in ctx.api.calls if c[0] == "run_shell")
    assert "echo hi" in call[1][0]
    assert "__bridge_done_" in call[1][0]
    assert call[1][1] == ctx.directory


def test_sh_requires_a_command(ctx):
    with pytest.raises(CommandError, match="Usage"):
        run(ctx, "/sh")


def test_sh_applies_the_command_guard(ctx):
    with pytest.raises(CommandError, match="command guard"):
        run(ctx, "/sh rm -rf /tmp/anything")
    assert not any(call[0] == "run_shell" for call in ctx.api.calls)


def test_dir_switches_and_reports_scope(ctx, workdir):
    run(ctx, f"/dir {workdir}")
    assert ctx.state.directory("42") == str(workdir.resolve())


def test_dir_reports_when_called_bare(ctx):
    assert "Sessions may target" in run(ctx, "/dir")


# ---------------------------------------------------------------- permissions


PERM = {
    "id": "perm_1",
    "sessionID": "ses_aaaaaaaaaaaaaaaa",
    "action": "bash",
    "resources": ["rm -rf /tmp/x"],
}


def test_perms_lists_pending(ctx):
    ctx.api.permissions = [PERM]
    reply = run(ctx, "/perms")
    assert "rm -rf" in reply and "perm_1" in reply


def test_perms_is_quiet_when_nothing_is_pending(ctx):
    assert "Nothing waiting" in run(ctx, "/perms")


def test_yes_approves_once_by_default(ctx):
    ctx.api.permissions = [PERM]
    run(ctx, "/yes perm_1")
    names = [c[0] for c in ctx.api.calls]
    assert "reply_permission" in names
    assert ctx.api.calls[-1][1][2] == "once"


def test_yes_always_sets_the_always_decision(ctx):
    ctx.api.permissions = [PERM]
    run(ctx, "/yes always perm_1")
    assert ctx.api.calls[-1][1][2] == "always"


def test_no_rejects(ctx):
    ctx.api.permissions = [PERM]
    run(ctx, "/no perm_1")
    assert ctx.api.calls[-1][1][2] == "reject"


def test_yes_all_clears_the_queue(ctx):
    ctx.api.permissions = [PERM, {**PERM, "id": "perm_2"}]
    run(ctx, "/yes all")
    assert ctx.api.permissions == []


def test_yes_by_index(ctx):
    ctx.api.permissions = [PERM]
    run(ctx, "/yes #1")
    assert ctx.api.permissions == []


def test_yes_rejects_an_unknown_reference(ctx):
    ctx.api.permissions = [PERM]
    with pytest.raises(CommandError, match="No pending request"):
        run(ctx, "/yes nope")


def test_permission_prompting_survives_api_failure(ctx):
    ctx.api.permission_requests = lambda directory=None: (_ for _ in ()).throw(
        ApiError(500, "boom")
    )
    assert "Nothing waiting" in run(ctx, "/perms")


# ------------------------------------------------------------------- inspection


def test_status_reports_the_server(ctx):
    reply = run(ctx, "/status")
    assert "2.0.31" in reply and "Scope" in reply


def test_export_is_json(ctx):
    ctx.state.set_session("42", "ses_aaaaaaaaaaaaaaaa")
    assert "```json" in run(ctx, "/export")


def test_git_reports_a_clean_tree(ctx):
    reply = run(ctx, "/git")
    assert "modified" in reply and "README.md" in reply


def test_find_requires_a_query(ctx):
    with pytest.raises(CommandError, match="Usage"):
        run(ctx, "/find")


def test_find_returns_relative_paths_scoped_to_the_directory(ctx, tmp_path):
    """/find must be scoped to the working directory, not the whole home."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "child.txt").write_text("x")
    reply = run(ctx, "/find child")
    assert "sub/child.txt" in reply


def test_find_reports_no_matches(ctx):
    assert "No files matching" in run(ctx, "/find zzz-nothing")


def test_help_lists_every_command(ctx):
    reply = run(ctx, "/help")
    for name in REGISTRY:
        assert f"/{name}" in reply


# ------------------------------------------------------- /sh execution model


def test_sh_reports_when_the_server_kills_a_long_command(ctx):
    """A reaped shell is gone from the shell list with no sentinel and no
    output - say so and point at the alternative."""
    # A fake whose output never carries the completion sentinel, as a reaped
    # shell would; the shell is no longer listed either.
    ctx.api.shell_output_data = {"output": "", "status": "running"}
    ctx.api.echo_sentinel = False
    ctx.api.shells = []
    import bridgekit.commands as commands

    original = commands.SHELL_PROBE
    commands.SHELL_PROBE = 0.05
    try:
        with pytest.raises(CommandError, match="nothing to show"):
            run(ctx, "/sh sleep 30")
    finally:
        commands.SHELL_PROBE = original


def test_sh_strips_the_completion_sentinel(ctx):
    """The sentinel is an implementation detail and must not reach the user."""
    reply = run(ctx, "/sh echo hi")
    assert "__bridge_done_" not in reply
    assert "hi" in reply


def test_sh_reports_a_failing_command(ctx):
    ctx.api.shell_output_data = {"output": "boom: no such thing\n", "status": "completed"}
    reply = run(ctx, "/sh whatever")
    assert "boom" in reply


def _await_delivery(delivered: list, timeout: float = 2.0) -> bool:
    """Wait for the shell thread to hand its result over."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not delivered:
        time.sleep(0.02)
    return bool(delivered)


def test_sh_with_respond_acks_then_delivers(ctx):
    """The dispatcher answers immediately; the output arrives out-of-band.

    The dispatcher runs on the main poll thread: joining there used to hold
    every command and every button - /abort for every chat - hostage for up
    to a minute.
    """
    sent: list[str] = []
    ctx.respond = sent.append
    reply = run(ctx, "/sh echo hi")
    assert reply is not None, "the ack must not be mistaken for a prompt"
    assert "Running $" in reply
    assert sent == [] or "$ echo hi" in "\n".join(sent)
    assert _await_delivery(sent)
    assert len(sent) == 1
    assert "hi" in sent[0]


def test_sh_with_respond_reports_failures(ctx, monkeypatch):
    """A dead command reaches the chat too - as an error, not silence."""
    monkeypatch.setattr("bridgekit.commands.SHELL_PROBE", 0.05)
    sent: list[str] = []
    ctx.respond = sent.append
    ctx.api.shell_output_data = {"output": "", "status": "running"}
    ctx.api.echo_sentinel = False
    ctx.api.shells = []
    run(ctx, "/sh sleep 30")
    assert _await_delivery(sent)
    assert sent[0].startswith("!")


def test_sh_with_respond_survives_an_api_error(ctx):
    """A server-side failure is delivered as text, never raised into the poll."""
    sent: list[str] = []
    ctx.respond = sent.append

    def explode(*args, **kwargs):
        raise ApiError(500, "shell endpoint exploded")

    ctx.api.run_shell = explode
    run(ctx, "/sh ls")
    assert _await_delivery(sent)
    assert "explode" in sent[0]


def test_sh_reports_the_exit_code(ctx):
    """[exit N] was unreachable: the code probe needed a space inside the
    sentinel tail, which never has one."""
    from conftest import FakeAPI

    class ExitSeven(FakeAPI):
        def run_shell(self, command, cwd, directory=None):
            self._record("run_shell", command, cwd, directory)
            import re as _re

            match = _re.search(r"__bridge_done_[0-9a-f]+", command)
            self._pending_token = match.group(0) if match else "__bridge_done_none"
            return {"id": "sh1", "status": "running"}

        def shell_output(self, shell_id, directory=None):
            return {"output": f"boom\n{self._pending_token} 7\n", "status": "running"}

    ctx.api = ExitSeven()
    reply = run(ctx, "/sh ls /definitely-missing")

    assert "[exit 7]" in reply


def test_sh_reports_success_with_exit_zero(ctx):
    reply = run(ctx, "/sh echo hi")
    assert "[exit 0]" in reply


def test_a_long_command_that_prints_early_keeps_polling_for_the_sentinel(ctx, monkeypatch):
    """A command that prints something and then runs past the probe used to
    come back after 3s with truncated output and no exit code: "no sentinel
    yet" was read as "the server reaped it". The shell list says the shell is
    still alive, so the runner keeps polling until the real end."""
    from conftest import FakeAPI

    monkeypatch.setattr("bridgekit.commands.SHELL_PROBE", 0.05)

    class LongRunning(FakeAPI):
        polls = 0

        def shell_output(self, shell_id, directory=None):
            self.polls += 1
            if self.polls < 3:
                return {"output": "starting...\n", "status": "running"}
            return {
                "output": f"starting...\ndone\n{self._pending_token} 0\n",
                "status": "completed",
            }

    ctx.api = LongRunning()
    reply = run(ctx, "/sh slow-but-chatty build")

    assert "starting..." in reply
    assert "done" in reply
    assert "[exit 0]" in reply
    # The shell-list check is what kept it waiting.
    assert any(call[0] == "list_shells" for call in ctx.api.calls)


def test_a_shell_that_vanishes_with_partial_output_still_returns_it(ctx, monkeypatch):
    """When the server genuinely reaps the shell - it is gone from the list -
    the partial output is all there is, as before."""
    from conftest import FakeAPI

    monkeypatch.setattr("bridgekit.commands.SHELL_PROBE", 0.05)

    class Reaped(FakeAPI):
        def __init__(self):
            super().__init__()
            self.shells = []

        def shell_output(self, shell_id, directory=None):
            return {"output": "starting...\n", "status": "running"}

    ctx.api = Reaped()
    reply = run(ctx, "/sh half-finished job")

    assert "starting..." in reply


def test_a_silent_command_still_listed_is_not_reported_as_reaped(ctx, monkeypatch):
    """No output and no sentinel is not proof the server reaped anything:
    while the shell is still listed the runner waits (to the tightened
    deadline here) instead of erroring after the 3s probe."""
    from conftest import FakeAPI

    monkeypatch.setattr("bridgekit.commands.SHELL_PROBE", 0.05)
    monkeypatch.setattr("bridgekit.commands.SHELL_TIMEOUT", 0.3)

    class Quiet(FakeAPI):
        def shell_output(self, shell_id, directory=None):
            return {"output": "", "status": "running"}

    ctx.api = Quiet()
    reply = run(ctx, "/sh silent-but-alive")

    assert "(no output)" in reply


def test_when_the_shell_list_fails_the_old_probe_heuristic_applies(ctx, monkeypatch):
    """A failing list call must not turn into a full-deadline wait: fall back
    to the old probe-only behaviour, which returned partial output."""
    from conftest import FakeAPI

    monkeypatch.setattr("bridgekit.commands.SHELL_PROBE", 0.05)

    class DeadList(FakeAPI):
        def list_shells(self, directory=None):
            raise ApiError(500, "list exploded")

        def shell_output(self, shell_id, directory=None):
            return {"output": "starting...\n", "status": "running"}

    ctx.api = DeadList()
    reply = run(ctx, "/sh prints then hangs")

    assert "starting..." in reply


def test_when_the_shell_list_fails_an_outputless_command_still_errors(ctx, monkeypatch):
    """Same fallback, empty-output branch: the friendly error still fires."""
    from conftest import FakeAPI

    monkeypatch.setattr("bridgekit.commands.SHELL_PROBE", 0.05)

    class DeadList(FakeAPI):
        def __init__(self):
            super().__init__()
            self.echo_sentinel = False
            self.shell_output_data = {"output": "", "status": "running"}

        def list_shells(self, directory=None):
            raise ApiError(500, "list exploded")

    ctx.api = DeadList()
    with pytest.raises(CommandError, match="nothing to show"):
        run(ctx, "/sh silent job")


def test_alert_rejects_negative_thresholds(ctx):
    with pytest.raises(CommandError, match="non-negative"):
        run(ctx, "/alert -5")
