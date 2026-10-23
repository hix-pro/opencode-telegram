"""The one authorization rule for every inbound Telegram surface.

The bot is a remote shell. A chat-id allow-list alone is not enough in a
group: membership is not authorization. Every update must come from an
allow-listed chat *and* an allow-listed sender; a private chat uses one id
for both. Keeping the rule in one function means a new update type cannot
quietly implement a weaker check.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

Payload = Mapping[str, Any]


def authorize(chat: Payload | None, actor: Payload | None, allowed: frozenset[str]) -> str:
    """Return the authorized chat id, or "" when the update must be dropped.

    Telegram omits ``from`` on channel posts; falling back to the chat id
    keeps an allow-listed channel working and changes nothing for private
    chats, where both ids are equal.
    """
    chat_id = str((chat or {}).get("id", ""))
    if not chat_id or chat_id not in allowed:
        return ""
    actor_id = str((actor or {}).get("id", "") or chat_id)
    if actor_id not in allowed:
        return ""
    return chat_id
