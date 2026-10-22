#!/usr/bin/env python3
"""Heartbeat watchdog for the Telegram bridge service.

``Restart=always`` recovers a bridge that *dies*; it cannot see a bridge that
hangs while alive - no exception, no exit, just a poll thread wedged. The
bridge stamps a heartbeat file once per poll iteration, and this script -
driven by a one-minute systemd timer - restarts the unit when the stamp goes
stale while systemd believes the unit is running.

A Telegram outage must NOT trigger restarts: the bridge touches the
heartbeat before every long poll, success or failure, so errors keep the
stamp live. Only true silence (no heartbeat write at all) trips the check.

Optional alert: set ``WATCHDOG_ALERT_URL`` (e.g. a webhook relay) and the
script POSTs a short JSON ``text`` before restarting.

Decision matrix (all failures visible on stdout, which journald captures):

``inactive``  unit not running - a stopped bridge stays stopped by design.
``warming``   unit is up but no heartbeat yet, and it has not been up long
              enough to worry (boot grace).
``restart``   unit running past max_age with a stale (or missing) heartbeat.
``fresh``     everything healthy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_MAX_AGE = 180.0  # comfortably above the 30s long-poll window


def decide(
    beats: bool, active: bool, active_age: float, stale: float | None, max_age: float
) -> str:
    """The pure decision. ``stale`` is seconds since the last heartbeat."""
    if not active:
        return "inactive"
    if not beats:
        # No heartbeat: normal in the first seconds only; an up-and-hanging
        # bridge (wedge BEFORE the first poll) is caught by the same clock.
        return "restart" if active_age > max_age else "warming"
    if stale is None or stale <= max_age:
        return "fresh"
    return "restart"


def _unit_active(unit: str) -> tuple[bool, float]:
    """systemd's view: is it running, and for how long (monotonic seconds)."""
    up_since = 0.0
    try:
        mono = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                "-p",
                "ActiveEnterTimestampMonotonic",
                "--value",
                unit,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        up_since = max(0.0, (int(mono.stdout.strip() or 0)) / 1_000_000.0)
        state = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return state.stdout.strip() == "active", time.monotonic() - up_since
    except (subprocess.SubprocessError, ValueError, OSError):
        # Cannot even talk to systemd: warn and stand down rather than
        # restarting on a hunch.
        return False, 0.0


def _alert(url: str, message: str) -> None:
    """Best-effort push before the restart; never fatal."""
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps({"text": message}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=5).close()
        print(f"watchdog: alerted {url}")
    except Exception as exc:  # noqa: BLE001 - any error must not block restart
        print(f"watchdog: alert to {url} failed: {exc}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("heartbeat", type=Path, help="the bridge's heartbeat file")
    parser.add_argument("--unit", default="opencode-telegram-bridge")
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE)
    args = parser.parse_args(argv)

    stale = None
    try:
        stale = time.time() - args.heartbeat.stat().st_mtime
    except OSError:
        pass
    active, active_age = _unit_active(args.unit)

    verdict = decide(bool(stale is not None), active, active_age, stale, args.max_age)
    if verdict == "restart":
        detail = (
            f"heartbeat {stale:.0f}s stale"
            if stale is not None
            else "no heartbeat since start"
        )
        message = f"{args.unit}: restarting, {detail} (age {active_age:.0f}s)"
        print(f"watchdog: {message}")
        url = os.environ.get("WATCHDOG_ALERT_URL", "").strip()
        if url:
            _alert(url, message)
        try:
            subprocess.run(
                ["systemctl", "--user", "restart", args.unit], timeout=30, check=False
            )
        except OSError as exc:
            print(f"watchdog: restart failed: {exc}")
            return 1
    else:
        print(f"watchdog: {verdict} (unit {args.unit}, active {active_age:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
