"""The heartbeat watchdog's head and its stinger: the pure decision matrix,
and the wiring that turns ``restart`` into an actual unit restart."""

from __future__ import annotations

import time

import scripts.heartbeat_watchdog as watchdog


def test_the_decision_matrix_covers_every_case():
    # Healthy: a fresh heartbeat under an active unit.
    assert watchdog.decide(True, True, 600.0, 5.0, 180.0) == "fresh"
    # A unit systemd does not count as running is never our problem.
    assert watchdog.decide(True, False, 0.0, 10_000.0, 180.0) == "inactive"
    assert watchdog.decide(False, False, 0.0, None, 180.0) == "inactive"
    # Just booted, no heartbeat yet: warming, not restarting.
    assert watchdog.decide(False, True, 10.0, None, 180.0) == "warming"
    # Up forever without a single heartbeat - wedged before the first poll.
    assert watchdog.decide(False, True, 600.0, None, 180.0) == "restart"
    # The headline case: running unit, silent heartbeat.
    assert watchdog.decide(True, True, 600.0, 1_000.0, 180.0) == "restart"


def test_a_silent_bridge_gets_restarted(tmp_path, monkeypatch):
    restarted: list[list[str]] = []
    monkeypatch.setattr(watchdog, "_unit_active", lambda unit: (True, 900.0))
    monkeypatch.setattr(watchdog.subprocess, "run", lambda cmd, **k: restarted.append(cmd))

    heartbeat = tmp_path / ".bridge-heartbeat"
    heartbeat.touch()
    decades_ago = time.time() - 10_000
    import os

    os.utime(heartbeat, (decades_ago, decades_ago))

    code = watchdog.main([str(heartbeat), "--unit", "opencode-telegram-bridge"])

    assert code == 0
    assert restarted and restarted[0][-1] == "opencode-telegram-bridge", restarted
    assert restarted[0][1:3] == ["--user", "restart"], restarted[0]


def test_an_errored_telegram_is_not_a_wedge(tmp_path, monkeypatch):
    """The heartbeat is touched before every poll, so a bridge keep-calling
    and keep-failing keeps its stamp - the watchdog leaves it alone."""
    monkeypatch.setattr(watchdog, "_unit_active", lambda unit: (True, 900.0))
    restarted: list[list[str]] = []
    monkeypatch.setattr(watchdog.subprocess, "run", lambda cmd, **k: restarted.append(cmd))

    heartbeat = tmp_path / ".bridge-heartbeat"
    heartbeat.touch()  # fresh stamp: written even as calls fail

    assert watchdog.main([str(heartbeat)]) == 0
    assert restarted == []


def test_a_missing_heartbeat_only_worries_after_boot_grace(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "_unit_active", lambda unit: (True, 20.0))
    restarted: list[list[str]] = []
    monkeypatch.setattr(watchdog.subprocess, "run", lambda cmd, **k: restarted.append(cmd))

    missing = tmp_path / "nothing-here"

    # Warming: recently started, no stamp yet.
    assert watchdog.main([str(missing)]) == 0
    # Wedged before the first poll kicks in only once the grace is over.
    monkeypatch.setattr(watchdog, "_unit_active", lambda unit: (True, 600.0))
    assert watchdog.main([str(missing)]) == 0
    assert restarted, "boot-wedged bridge must be restarted"
    assert restarted[0][-1] == "opencode-telegram-bridge"
