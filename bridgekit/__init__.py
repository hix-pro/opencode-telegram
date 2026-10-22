"""Telegram bridge for OpenCode.

Modules:
    config      environment loading and validation
    state       per-chat session persistence
    api         thin client over the OpenCode HTTP API
    telegram    Telegram Bot API client and message chunking
    formatting  render helpers for humans reading on a phone
    commands    command registry and handlers
"""

__all__ = ["api", "commands", "config", "formatting", "state", "telegram"]
