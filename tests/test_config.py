"""Config loading and directory scoping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridgekit.config import Config, ConfigError


def base_env(**overrides):
    env = {
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_CHAT_ID": "42, 43",
        "OPENCODE_PASSWORD": "secret",
        "PROJECT_DIR": "/home/demo",
    }
    env.update(overrides)
    return env


def test_missing_token_is_rejected():
    env = base_env()
    del env["TELEGRAM_BOT_TOKEN"]
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Config.from_env(env)


def test_missing_allow_list_is_rejected():
    """Without TELEGRAM_CHAT_ID the bot token alone would be enough to own the box."""
    env = base_env()
    del env["TELEGRAM_CHAT_ID"]
    with pytest.raises(ConfigError, match="refuses to start"):
        Config.from_env(env)


def test_blank_allow_list_is_rejected():
    with pytest.raises(ConfigError, match="usable id"):
        Config.from_env(base_env(TELEGRAM_CHAT_ID=" , "))


def test_allow_list_is_split_and_trimmed():
    config = Config.from_env(base_env(TELEGRAM_CHAT_ID=" 42 , 43 "))
    assert config.allowed_chats == frozenset({"42", "43"})


def test_password_falls_back_to_service_json(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    service = tmp_path / ".config/opencode/service.json"
    service.parent.mkdir(parents=True)
    service.write_text(json.dumps({"password": "from-file"}))

    env = base_env(PROJECT_DIR=str(tmp_path))
    del env["OPENCODE_PASSWORD"]
    config = Config.from_env(env)
    assert config.password == "from-file"


def test_url_defaults_and_trailing_slash():
    config = Config.from_env(base_env(OPENCODE_URL="http://host:1234/"))
    assert config.opencode_url == "http://host:1234"


def test_numeric_overrides_are_validated():
    with pytest.raises(ConfigError, match="PROMPT_TIMEOUT"):
        Config.from_env(base_env(PROMPT_TIMEOUT="soon"))


def test_unrestricted_by_default(tmp_path):
    config = Config.from_env(base_env(PROJECT_DIR=str(tmp_path)))
    assert config.unrestricted is True
    assert config.resolve_dir("/etc") == "/etc"


def test_allowed_roots_confine_resolution(tmp_path):
    root = tmp_path / "projects"
    (root / "inner").mkdir(parents=True)
    config = Config.from_env(
        base_env(PROJECT_DIR=str(root), ALLOWED_ROOTS=f"{root},{tmp_path / 'other'}")
    )
    assert config.unrestricted is False
    assert config.resolve_dir(str(root / "inner")) == str(root / "inner")

    with pytest.raises(ConfigError, match="outside the allowed roots"):
        config.resolve_dir(str(tmp_path))


def test_resolve_dir_rejects_missing_path(tmp_path):
    config = Config.from_env(base_env(PROJECT_DIR=str(tmp_path)))
    with pytest.raises(ConfigError, match="No such directory"):
        config.resolve_dir(str(tmp_path / "nope"))


def test_resolve_dir_rejects_files(tmp_path):
    target = tmp_path / "afile"
    target.write_text("x")
    config = Config.from_env(base_env(PROJECT_DIR=str(tmp_path)))
    with pytest.raises(ConfigError, match="Not a directory"):
        config.resolve_dir(str(target))


# ------------------------------------------------- inline comments in .env


def test_inline_comment_is_stripped_from_a_number():
    """Copying a documented line and keeping its explanation must still work."""
    config = Config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "TELEGRAM_CHAT_ID": "42",
            "PROMPT_TIMEOUT": "7200     # hard cap on one turn (2h)",
        }
    )
    assert config.prompt_timeout == 7200


def test_a_hash_inside_a_password_survives():
    config = Config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "TELEGRAM_CHAT_ID": "42",
            "OPENCODE_PASSWORD": "abc#123def",
        }
    )
    assert config.password == "abc#123def"


def test_a_fully_commented_value_is_ignored():
    """A line someone commented out must not become a setting."""
    with pytest.raises(ConfigError):
        Config.from_env(
            {
                "TELEGRAM_BOT_TOKEN": "123:abc",
                "TELEGRAM_CHAT_ID": "42",
                "ALLOWED_ROOTS": "# /home/demo",
            }
        )


def test_allowed_roots_tolerate_spaces_after_commas():
    config = Config.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_CHAT_ID": "42",
            "OPENCODE_URL": "http://x",
            "OPENCODE_PASSWORD": "p",
            "PROJECT_DIR": "/home/demo",
            "ALLOWED_ROOTS": "/home/demo, /home/demo/Desktop",
        }
    )
    assert Path("/home/demo") in config.allowed_roots
    assert Path("/home/demo/Desktop") in config.allowed_roots
    # A leading space used to resolve as a distinct (nonexistent) root.
    assert not any(str(root).lstrip() != str(root) for root in config.allowed_roots)
