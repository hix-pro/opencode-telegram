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
    }
    for name, value in documented_pairs().items():
        Config.from_env({**base, name: value})


def test_service_unit_exists_in_the_repo():
    unit = ROOT / "systemd" / "opencode-telegram-bridge.service"
    assert unit.exists(), "the service definition must be recoverable from the repo"
    text = unit.read_text()
    assert "ExecStart=" in text and "EnvironmentFile=" in text


def test_install_scripts_exist_and_are_executable():
    for name in ("install.sh", "uninstall.sh"):
        script = ROOT / name
        assert script.exists(), f"{name} is missing"
        assert script.stat().st_mode & 0o111, f"{name} is not executable"
