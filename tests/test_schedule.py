from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import plistlib
import pytest

from order_desk.calendar import TWSE_HOLIDAY_URL, CalendarUnavailable, TwseCalendar
from order_desk.planner import build_order_plan
from order_desk.models import ExecutionPolicy, Quote
from order_desk.schedule import (
    due_actions,
    install_schedule,
    order_stamp_blocks_retry,
    pull_is_current,
    run_git_pull,
    schedule_enabled,
    schedule_slots,
    uninstall_schedule,
    week_id_for,
    within_order_session,
    write_order_stamp,
)
from order_desk.signal import load_signal


TAIPEI = ZoneInfo("Asia/Taipei")


def _weekdays_open(day: date) -> bool:
    return day.weekday() < 5


def test_monday_schedule_is_eight_and_nine_twenty():
    slots = schedule_slots(
        {
            "schedule": {
                "git_pull": {"weekday": "monday", "time": "08:00"},
                "order": {"weekday": "monday", "time": "09:20"},
            }
        }
    )
    assert slots["git_pull"] == {"day": "monday", "hour": 8, "minute": 0}
    assert slots["order"] == {"day": "monday", "hour": 9, "minute": 20}


def test_schedule_uses_taipei_time_when_the_computer_is_elsewhere():
    slots = schedule_slots({})
    before = datetime(2026, 10, 5, 7, 59, tzinfo=TAIPEI)
    assert due_actions(before, slots, pull_done=False, order_done=False, is_trading_day=_weekdays_open) == []
    # Sunday 17:00 in California is Monday 08:00 in Taipei.
    california = datetime(2026, 10, 4, 17, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert due_actions(
        california, slots, pull_done=False, order_done=False, is_trading_day=_weekdays_open
    ) == ["git_pull"]
    taipei_order = datetime(2026, 10, 5, 1, 20, tzinfo=ZoneInfo("UTC"))
    assert due_actions(
        taipei_order, slots, pull_done=True, order_done=False, is_trading_day=_weekdays_open
    ) == ["order"]
    new_york_morning = datetime(2026, 10, 5, 9, 20, tzinfo=ZoneInfo("America/New_York"))
    assert "order" not in due_actions(
        new_york_morning, slots, pull_done=True, order_done=False, is_trading_day=_weekdays_open
    )


def test_missing_holiday_calendar_does_not_treat_a_weekday_as_open(tmp_path: Path, monkeypatch):
    cache = tmp_path / "twse-holidays.json"
    cache.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": TWSE_HOLIDAY_URL,
                "year": 2026,
                "fetched_at": "2026-01-01T00:00:00Z",
                "holidays": ["2026-10-05"],
            }
        ),
        encoding="utf-8",
    )
    calendar = TwseCalendar(cache)
    assert calendar.is_trading_day(date(2026, 10, 6))
    assert not calendar.is_trading_day(date(2026, 10, 5))
    assert not calendar.is_trading_day(date(2026, 10, 4))
    missing = TwseCalendar(tmp_path / "missing.json")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    with pytest.raises(CalendarUnavailable):
        missing.is_trading_day(date(2026, 10, 6))


def test_holiday_monday_moves_the_jobs_to_the_next_open_day():
    slots = schedule_slots({})

    def market_open(day: date) -> bool:
        return day.weekday() < 5 and day.isoformat() != "2026-10-05"

    holiday_morning = datetime(2026, 10, 5, 8, 0, tzinfo=TAIPEI)
    assert due_actions(
        holiday_morning, slots, pull_done=False, order_done=False, is_trading_day=market_open
    ) == []
    tuesday_pull = datetime(2026, 10, 6, 8, 0, tzinfo=TAIPEI)
    assert due_actions(
        tuesday_pull, slots, pull_done=False, order_done=False, is_trading_day=market_open
    ) == ["git_pull"]
    tuesday_order = datetime(2026, 10, 6, 9, 20, tzinfo=TAIPEI)
    assert due_actions(
        tuesday_order, slots, pull_done=True, order_done=False, is_trading_day=market_open
    ) == ["order"]


def test_missed_monday_is_caught_up_later_in_the_week():
    slots = schedule_slots({})
    tuesday = datetime(2026, 10, 6, 9, 20, tzinfo=TAIPEI)
    assert due_actions(
        tuesday, slots, pull_done=False, order_done=False, is_trading_day=_weekdays_open
    ) == ["git_pull", "order"]
    wednesday = datetime(2026, 10, 7, 9, 20, tzinfo=TAIPEI)
    assert due_actions(
        wednesday, slots, pull_done=True, order_done=False, is_trading_day=_weekdays_open
    ) == ["order"]
    saturday = datetime(2026, 10, 10, 9, 20, tzinfo=TAIPEI)
    assert due_actions(
        saturday, slots, pull_done=False, order_done=False, is_trading_day=_weekdays_open
    ) == []


def test_completed_week_blocks_later_catchup(tmp_path: Path):
    slots = schedule_slots({})
    stamp = tmp_path / "last_order.json"
    write_order_stamp(
        stamp,
        week=week_id_for(date(2026, 10, 5)),
        trading_day="2026-10-05",
        status="completed",
    )
    assert order_stamp_blocks_retry(stamp, date(2026, 10, 5))
    assert order_stamp_blocks_retry(stamp, date(2026, 10, 7))
    wednesday = datetime(2026, 10, 7, 9, 20, tzinfo=TAIPEI)
    assert due_actions(
        wednesday,
        slots,
        pull_done=True,
        order_done=order_stamp_blocks_retry(stamp, date(2026, 10, 7)),
        is_trading_day=_weekdays_open,
    ) == []


def test_failed_order_retries_same_day_and_working_waits(tmp_path: Path):
    stamp = tmp_path / "last_order.json"
    write_order_stamp(
        stamp,
        week=week_id_for(date(2026, 10, 5)),
        trading_day="2026-10-05",
        status="failed",
    )
    assert not order_stamp_blocks_retry(stamp, date(2026, 10, 5))
    write_order_stamp(
        stamp,
        week=week_id_for(date(2026, 10, 5)),
        trading_day="2026-10-05",
        status="working",
    )
    assert order_stamp_blocks_retry(stamp, date(2026, 10, 5))
    assert not order_stamp_blocks_retry(stamp, date(2026, 10, 6))


def test_disabled_schedule_runs_nothing():
    slots = schedule_slots({"schedule": {"enabled": False}})
    assert schedule_enabled({"schedule": {"enabled": False}}) is False
    assert schedule_enabled({}) is True
    monday = datetime(2026, 10, 5, 9, 20, tzinfo=TAIPEI)
    assert due_actions(
        monday,
        slots,
        pull_done=False,
        order_done=False,
        is_trading_day=_weekdays_open,
        enabled=False,
    ) == []


def test_order_job_only_runs_inside_the_session_after_a_successful_pull(tmp_path: Path):
    stamp = tmp_path / "last_pull.json"
    assert pull_is_current(stamp, "2026-10-05") is False
    stamp.write_text(json.dumps({"ok": False, "trading_day": "2026-10-05"}), encoding="utf-8")
    assert pull_is_current(stamp, "2026-10-05") is False
    stamp.write_text(json.dumps({"ok": True, "trading_day": "2026-10-05"}), encoding="utf-8")
    assert pull_is_current(stamp, "2026-10-05") is True
    assert pull_is_current(stamp, "2026-10-06") is False
    assert within_order_session(datetime(2026, 10, 5, 9, 20, tzinfo=TAIPEI))
    assert not within_order_session(datetime(2026, 10, 5, 8, 0, tzinfo=TAIPEI))


def test_failed_pull_does_not_allow_the_order(tmp_path: Path):
    stamp = tmp_path / "logs" / "last_pull.json"
    code = run_git_pull(tmp_path, stamp, "2026-10-05")
    assert code != 0
    assert pull_is_current(stamp, "2026-10-05") is False


def test_install_writes_one_job_per_computer(tmp_path: Path):
    config_path = tmp_path / "order_desk" / "config.json"
    config_path.parent.mkdir()
    config = {
        "schedule": {
            "git_pull": {"weekday": "monday", "time": "08:00"},
            "order": {"weekday": "monday", "time": "09:20"},
        }
    }
    agents = tmp_path / "agents"
    written = install_schedule(
        config_path,
        config,
        python="/usr/bin/python3",
        agents_dir=agents,
        domain="gui/1",
        load=False,
    )
    assert len(written) == 1
    job = plistlib.loads(written[0].read_bytes())
    assert job["StartInterval"] == 60
    assert "--weekly-tick" in job["ProgramArguments"]
    assert str(config_path) in job["ProgramArguments"]
    assert written[0].is_file()
    uninstall_schedule(config_path, agents_dir=agents, domain="gui/1", load=False)
    assert not written[0].is_file()


def test_same_weights_resize_the_next_day_from_the_live_price(tmp_path: Path):
    now = datetime(2026, 10, 7, 1, 20, tzinfo=TAIPEI)
    payload = {
        "bundle_id": "11111111-1111-1111-1111-111111111111",
        "revision": 1,
        "effective_at": "2026-10-05T00:00:00+08:00",
        "expires_at": "2026-10-10T00:00:00+08:00",
        "data_as_of": "2026-10-02",
        "strategies": [{"strategy_id": "alpha", "weights": [{"ticker": "2330", "weight": 1}]}],
    }
    path = tmp_path / "signal.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    signal = load_signal(path, now=now)
    plan = build_order_plan(
        signal,
        {"alpha": 10_000},
        {"2330": Quote("2330", bid=190, ask=200, last=200, limit_up=220, limit_down=180)},
        {},
        {},
        20_000,
        ExecutionPolicy(lot_mode="odd", max_order_twd=20_000),
        mode="production",
        now=now,
    )
    assert plan.orders[0].shares == 50
    assert plan.orders[0].limit_price == 220
