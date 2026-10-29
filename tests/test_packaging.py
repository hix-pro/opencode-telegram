"""Every setting the bridge reads must be documented, and vice versa.

A setting nobody knows about is a setting nobody can safely change. This
guards both directions, so adding a knob without documenting it - or deleting
the docs for a knob that still works - fails the suite instead of confusing
someone at 2am.
"""

from __future__ import annotations

import re
from pathlib import Path

from bridgekit.config import Config

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
CONFIG = ROOT / "bridgekit" / "config.py"


def env_settings() -> set[str]:
    """Names Config reads from the environment."""
    source = CONFIG.read_text()
    # Any reader helper - _env, _int_env, _positive_float, source.get. Anchoring
    # on a specific suffix misses settings and makes this test lie.
    found = set(re.findall(r'\w+\(\s*source,\s*"([A-Z_]+)"', source))
    found |= set(re.findall(r'source\.get\("([A-Z_]+)"', source))
    return found


def test_the_example_file_exists():
    assert EXAMPLE.exists(), ".env.example is required to install the bridge"


def test_every_setting_is_documented():
    documented = EXAMPLE.read_text()
    missing = sorted(name for name in env_settings() if name not in documented)
    assert not missing, f"undocumented settings: {missing}"


def test_no_documented_setting_is_dead():
    """A setting documented but never read is a trap for the reader."""
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z_]{3,})=", EXAMPLE.read_text(), re.M))
    dead = sorted(name for name in documented if name not in env_settings())
    assert not dead, f"documented but never read: {dead}"


def test_the_example_does_not_leak_secrets():
    text = EXAMPLE.read_text()
    assert "TELEGRAM_BOT_TOKEN=\n" in text or "TELEGRAM_BOT_TOKEN=" in text
    # The bot token must never have a real value committed alongside the blank.
    for line in text.splitlines():
        if line.strip().startswith("TELEGRAM_BOT_TOKEN="):
            assert line.strip() == "TELEGRAM_BOT_TOKEN=", "token must ship blank"


def documented_pairs() -> dict[str, str]:
    """NAME=value pairs written in the example file, including commented ones."""
    pairs = {}
    for line in EXAMPLE.read_text().splitlines():
        stripped = line.strip().lstrip("#").strip()
        if "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        name = name.split("#")[0].strip()
        if name.isupper() and name.replace("_", "").isalnum() and value:
            # Blank values are placeholders for credentials the reader must
            # supply; only real values are worth checking.
            pairs[name] = value
    return pairs


def test_example_values_are_accepted():
    """Every value shown in the example must be one the loader accepts.

    A typo in a documented value breaks installation at the worst moment -
    someone following the README on a fresh machine.
    """
    base = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_CHAT_ID": "42",
        # Explicit password: the loader otherwise falls back to the machine's
        # ~/.config/opencode/service.json, absent on a clean CI runner.
        "OPENCODE_PASSWORD": "secret",
        # The example's ALLOWED_ROOTS=~ must contain the default project
        # directory even when the checkout itself lives outside $HOME (for
        # example, a review copy under /tmp).
        "PROJECT_DIR": str(Path.home()),
    }
    for name, value in documented_pairs().items():
        Config.from_env({**base, name: value})


def test_service_units_are_recoverable_from_the_repo():
    """The units are rendered from templates at install time, so the repo must
    carry the template pair plus the timer - otherwise a fresh checkout
    cannot reproduce the deployment."""
    systemd = ROOT / "systemd"
    for name in (
        "opencode-telegram-bridge.service.in",
        "opencode-watchdog.service.in",
        "opencode-watchdog.timer",
    ):
        unit = systemd / name
        assert unit.exists(), f"{name} must be recoverable from the repo"

    text = (systemd / "opencode-telegram-bridge.service.in").read_text()
    assert "ExecStart=" in text and "EnvironmentFile=" in text
    # Soft references only: a foreign machine without opencode-server.service
    # must still be able to boot the bridge.
    assert "Wants=" in text and "Requires=" not in text
    # install.sh renders both @REPO@ and @PYTHON@; a stale placeholder must
    # never leak into an installed unit.
    assert "@REPO@" in text and "@PYTHON@" in text
    watchdog = (systemd / "opencode-watchdog.service.in").read_text()
    assert "@REPO@" in watchdog and "heartbeat_watchdog" in watchdog


def test_install_scripts_exist_and_are_executable():
    for name in ("install.sh", "uninstall.sh"):
        script = ROOT / name
        assert script.exists(), f"{name} is missing"
        assert script.stat().st_mode & 0o111, f"{name} is not executable"


def test_service_units_are_sandboxed():
    """The units are part of the security boundary, not just launchers."""
    systemd = ROOT / "systemd"
    bridge = (systemd / "opencode-telegram-bridge.service.in").read_text()
    for directive in (
        "NoNewPrivileges=yes",
        "ProtectKernelTunables=yes",
        "CapabilityBoundingSet=",
        "SystemCallFilter=@system-service",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "PrivateTmp=yes",
    ):
        assert directive in bridge, f"bridge unit lost {directive}"
    # The filesystem half must not depend on ALLOWED_ROOTS any more; it is
    # the --harden-bridge drop-in's job (see test_hardening.py).
    assert "@SANDBOX@" not in bridge

    bridge_dropin = (systemd / "opencode-telegram-bridge-hardening.conf.in").read_text()
    for directive in (
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "@RW_PATHS@",
    ):
        assert directive in bridge_dropin, f"bridge drop-in lost {directive}"

    server = (systemd / "opencode-server-hardening.conf.in").read_text()
    for directive in (
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "SystemCallFilter=@system-service",
        "@RW_PATHS@",
    ):
        assert directive in server, f"server drop-in lost {directive}"

    watchdog = (systemd / "opencode-watchdog.service.in").read_text()
    assert "NoNewPrivileges=yes" in watchdog
    assert "ProtectSystem=strict" in watchdog

    installer = (ROOT / "install.sh").read_text()
    assert "--harden-server" in installer
    assert "--harden-bridge" in installer
    assert "ReadWritePaths=" in installer


def test_release_version_matches_the_changelog():
    """A tag without a matching changelog entry is an unreviewed release."""
    pyproject = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    assert match, "pyproject.toml has no project version"
    version = match.group(1)

    changelog = (ROOT / "CHANGELOG.md").read_text()
    headings = re.findall(r"^## v([0-9][^\s]*)", changelog, re.M)
    assert headings, "CHANGELOG.md has no version headings"
    assert headings[0] == version, (
        f"pyproject says {version}, CHANGELOG starts at {headings[0]}"
    )
