"""Recurring schedules."""

from __future__ import annotations

import time

import pytest

from bridgekit.schedule import DAY, HOUR, MINUTE, ScheduleStore, parse


def test_parse_every_minutes():
    assert parse("every 30m") == (30 * MINUTE, None, None)


def test_parse_every_hours():
    assert parse("every 2 hours") == (2 * HOUR, None, None)
    assert parse("every 1h") == (HOUR, None, None)


def test_parse_shorthands():
    assert parse("hourly") == (HOUR, None, None)
    assert parse("daily") == (DAY, 0, 0)
    assert parse("midnight") == (DAY, 0, 0)


def test_parse_daily_time():
    assert parse("daily 09:00") == (DAY, 9, 0)
    assert parse("daily 23:59") == (DAY, 23, 59)


def test_parse_is_case_insensitive():
    assert parse("EVERY 30M") == (30 * MINUTE, None, None)


@pytest.mark.parametrize("spec", ["cron * * * *", "every 0m", "daily 99:99", "nonsense", ""])
def test_bad_specs_are_rejected(spec):
    with pytest.raises(ValueError):
        parse(spec)


def test_descriptions_use_human_units():
    store = ScheduleStore()
    assert store.add("42", "every 30m", "x").description == "every 30m"
    assert store.add("42", "every 2 hours", "x").description == "every 2h"
    assert store.add("42", "daily 09:00", "x").description == "daily at 09:00"


def test_due_only_once_per_interval():
    store = ScheduleStore()
    store.add("42", "every 30m", "check")
    now = time.time()
    assert len(store.due(now)) == 1
    assert store.due(now + 60) == [], "must not fire again immediately"
    assert len(store.due(now + 31 * MINUTE)) == 1, "fires once the interval passes"


def test_daily_waits_for_the_right_minute():
    store = ScheduleStore()
    schedule = store.add("42", "daily 09:00", "report")
    # Pick a time that is not 09:00.
    now = time.time()
    assert store.due(now) == [], "must not fire at the wrong time"

    import time as _time

    today = _time.localtime(now)
    target = _time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 9, 0, 0, 0, 0, -1))
    if target > now:
        assert schedule.due(target)
        assert schedule.due(target) is False, "only once on the day"


def test_schedules_survive_a_reload():
    store = ScheduleStore()
    store.add("42", "every 30m", "check the build")
    store.add("43", "daily 09:00", "morning report")

    loaded = ScheduleStore()
    loaded.load(store.to_list())
    assert len(loaded.items) == 2
    assert loaded.for_chat("42")[0].prompt == "check the build"
    assert loaded.for_chat("43")[0].description == "daily at 09:00"


def test_remove_by_id_and_suffix():
    store = ScheduleStore()
    schedule = store.add("42", "hourly", "x")
    assert store.remove(schedule.id).id == schedule.id
    with pytest.raises(KeyError):
        store.remove("nope")


def test_remove_by_short_suffix():
    store = ScheduleStore()
    schedule = store.add("42", "hourly", "x")
    short = schedule.id.split("_", 2)[2]
    assert store.remove(short).id == schedule.id


def test_corrupt_entries_are_skipped():
    store = ScheduleStore()
    store.load(
        [
            {"nope": 1},
            {"id": "a", "chat_id": "1", "spec": "hourly", "prompt": "p", "interval": 3600},
        ]
    )
    assert len(store.items) == 1


def test_clear_by_chat():
    store = ScheduleStore()
    store.add("42", "hourly", "a")
    store.add("43", "hourly", "b")
    store.clear("42")
    assert len(store.for_chat("42")) == 0
    assert len(store.for_chat("43")) == 1


# ------------------------------------------------------------------ command


def test_schedule_command_lists_and_adds(ctx):
    from bridgekit.commands import run

    assert "No schedules" in run(ctx, "/schedule")
    added = run(ctx, "/schedule every 30m check the build")
    assert "every 30m" in added
    assert "check the build" in run(ctx, "/schedule list")


def test_schedule_command_removes(ctx):
    from bridgekit.commands import run

    added = run(ctx, "/schedule hourly tick")
    schedule_id = next(
        word for word in added.replace(":", " ").split() if word.startswith("sch_")
    )
    assert "Removed" in run(ctx, f"/schedule rm {schedule_id}")
    assert "No schedules" in run(ctx, "/schedule list")


def test_schedule_command_rejects_a_bad_spec(ctx):
    from bridgekit.commands import CommandError, run

    with pytest.raises(CommandError, match="Bad schedule"):
        run(ctx, "/schedule cron * * * * do something")


def test_schedule_with_no_prompt_is_rejected(ctx):
    from bridgekit.commands import CommandError, run

    with pytest.raises(CommandError):
        run(ctx, "/schedule every 30m")


def test_schedules_persist_through_state(ctx):
    from bridgekit.commands import run

    run(ctx, "/schedule hourly tick")
    reloaded = type(ctx.state)(ctx.state.path, ctx.state.default_dir)
    reloaded.load()
    assert len(reloaded.load_schedules()) == 1
