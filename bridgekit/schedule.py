"""Simple recurring schedules.

Deliberately small: ``every 30m``, ``hourly``, and ``daily 09:00``. No cron
parser, because a schedule you cannot read is a schedule you cannot debug.

A scheduler thread ticks once a minute and hands matching prompts to the same
queue the chat uses, so scheduled work runs through exactly the same path -
streaming, permissions, abort, everything.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

MINUTE = 60
HOUR = 3600
DAY = 86400

EVERY = re.compile(r"^every\s+(\d+)\s*(m|min|mins|minutes|h|hours?)$", re.IGNORECASE)
DAILY = re.compile(r"^daily\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)


@dataclass
class Schedule:
    id: str
    chat_id: str
    spec: str
    prompt: str
    interval: int
    at_hour: int | None = None
    at_minute: int | None = None
    created: float = field(default_factory=time.time)
    last_run: float = 0.0

    @property
    def description(self) -> str:
        """Human wording in the units the user actually typed."""
        if self.at_hour is not None:
            return f"daily at {self.at_hour:02d}:{self.at_minute:02d}"
        if self.interval >= HOUR:
            return f"every {self.interval // HOUR}h"
        return f"every {self.interval // MINUTE}m"

    def due(self, now: float) -> bool:
        if self.last_run and now - self.last_run < self.interval:
            return False
        if self.at_hour is None:
            return True
        local = time.localtime(now)
        return (
            local.tm_hour == self.at_hour
            and local.tm_min == self.at_minute
            and (not self.last_run or now - self.last_run > self.interval - 60)
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "spec": self.spec,
            "prompt": self.prompt,
            "interval": self.interval,
            "at_hour": self.at_hour,
            "at_minute": self.at_minute,
            "created": self.created,
            "last_run": self.last_run,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Schedule:
        return cls(
            id=data["id"],
            chat_id=data["chat_id"],
            spec=data["spec"],
            prompt=data["prompt"],
            interval=int(data["interval"]),
            at_hour=data.get("at_hour"),
            at_minute=data.get("at_minute"),
            created=data.get("created", 0.0),
            last_run=data.get("last_run", 0.0),
        )


def parse(spec: str) -> tuple[int, int | None, int | None]:
    """Return (interval, hour, minute) for a schedule spec."""
    spec = spec.strip()

    if spec.lower() in ("hourly", "every hour", "every 1h"):
        return HOUR, None, None
    if spec.lower() in ("daily", "midnight"):
        return DAY, 0, 0

    match = EVERY.match(spec)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if amount < 1:
            raise ValueError("interval must be at least 1")
        return (amount * HOUR if unit.startswith("h") else amount * MINUTE), None, None

    match = DAILY.match(spec)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if not 0 <= hour < 24 or not 0 <= minute < 60:
            raise ValueError("time must be between 00:00 and 23:59")
        return DAY, hour, minute

    raise ValueError("use 'every 30m', 'hourly', or 'daily 09:00'")


class ScheduleStore:
    def __init__(self) -> None:
        self.items: list[Schedule] = []

    def add(self, chat_id: str, spec: str, prompt: str) -> Schedule:
        interval, hour, minute = parse(spec)
        schedule = Schedule(
            id=f"sch_{len(self.items) + 1}_{int(time.time()) % 100000}",
            chat_id=chat_id,
            spec=spec,
            prompt=prompt,
            interval=interval,
            at_hour=hour,
            at_minute=minute,
        )
        self.items.append(schedule)
        return schedule

    def remove(self, ref: str) -> Schedule:
        ref = ref.strip()
        for index, schedule in enumerate(self.items):
            if schedule.id == ref or schedule.id.endswith(f"_{ref}"):
                return self.items.pop(index)
        raise KeyError(ref)

    def for_chat(self, chat_id: str) -> list[Schedule]:
        return [s for s in self.items if s.chat_id == chat_id]

    def due(self, now: float | None = None) -> list[Schedule]:
        now = now if now is not None else time.time()
        ready = [s for s in self.items if s.due(now)]
        for schedule in ready:
            schedule.last_run = now
        return ready

    def to_list(self) -> list[dict]:
        return [s.to_dict() for s in self.items]

    def load(self, data: list[dict]) -> None:
        self.items = []
        for entry in data or []:
            try:
                self.items.append(Schedule.from_dict(entry))
            except (KeyError, TypeError, ValueError):
                continue

    def clear(self, chat_id: str) -> None:
        self.items = [s for s in self.items if s.chat_id != chat_id]
