"""Per-computer weekly pull and order schedule.

Each user installs this on their own Mac. The shared GitHub files supply
the week's weights. Account keys, order size, and the ledger stay on that computer.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from collections.abc import Callable
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from order_desk.signal import SignalError

TAIPEI = ZoneInfo("Asia/Taipei")
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
ORDER_STATUSES = {"completed", "working", "failed"}


def taipei_now() -> datetime:
    return datetime.now(TAIPEI)


def within_order_session(now: datetime) -> bool:
    local = now.astimezone(TAIPEI)
    minutes = local.hour * 60 + local.minute
    return 9 * 60 <= minutes <= 13 * 60 + 25


def week_id_for(day: date) -> str:
    monday = day - timedelta(days=day.weekday())
    return monday.isoformat()


def schedule_enabled(config: Mapping[str, Any]) -> bool:
    raw = config.get("schedule")
    if not isinstance(raw, dict):
        return True
    if "enabled" not in raw:
        return True
    return bool(raw.get("enabled"))


def schedule_slots(config: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    raw = config.get("schedule")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "git_pull": _slot(raw.get("git_pull"), "monday", "08:00"),
        "order": _slot(raw.get("order"), "monday", "09:20"),
    }


def pull_stamp_path(root: Path) -> Path:
    return root / "logs" / "last_pull.json"


def order_stamp_path(root: Path) -> Path:
    return root / "logs" / "last_order.json"


def due_actions(
    now: datetime,
    slots: Mapping[str, Mapping[str, int | str]],
    *,
    pull_done: bool,
    order_done: bool,
    is_trading_day: Callable[[date], bool],
    enabled: bool = True,
) -> list[str]:
    """Return jobs for open sessions from the week's first day through Friday."""
    if not enabled:
        return []
    local = now.astimezone(TAIPEI)
    actions: list[str] = []
    if (
        _is_catchup_day(local.date(), slots["git_pull"], is_trading_day)
        and _time_reached(local, slots["git_pull"])
        and not pull_done
    ):
        actions.append("git_pull")
    if (
        _is_catchup_day(local.date(), slots["order"], is_trading_day)
        and _time_reached(local, slots["order"])
        and within_order_session(local)
        and not order_done
    ):
        actions.append("order")
    return actions


def pull_is_current(path: Path, today: str) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("ok") is True and payload.get("trading_day") == today


def order_stamp_blocks_retry(path: Path, today: date) -> bool:
    """True when this week's order is done, or still working on the same day."""
    payload = _read_stamp(path)
    if payload is None:
        return False
    week = week_id_for(today)
    if str(payload.get("week", "")) != week:
        # Legacy stamps used ok+trading_day without week/status.
        if payload.get("ok") is True and payload.get("trading_day") == today.isoformat():
            return True
        return False
    status = str(payload.get("status", ""))
    if status == "completed":
        return True
    if status == "working" and payload.get("trading_day") == today.isoformat():
        return True
    return False


def write_order_stamp(
    path: Path,
    *,
    week: str,
    trading_day: str,
    status: str,
    detail: str = "",
) -> None:
    if status not in ORDER_STATUSES:
        raise SignalError(f"order stamp status must be one of {sorted(ORDER_STATUSES)}")
    payload: dict[str, Any] = {
        "ok": status == "completed",
        "week": week,
        "trading_day": trading_day,
        "status": status,
    }
    if detail:
        payload["detail"] = detail[-2000:]
    _write_stamp(path, payload)


def run_git_pull(repo: Path, stamp_path: Path, today: str) -> int:
    result = subprocess.run(
        ["git", "-C", str(repo), "pull", "--ff-only"],
        check=False,
        capture_output=True,
        text=True,
    )
    detail = (result.stdout + result.stderr).strip()
    _write_stamp(
        stamp_path,
        {"ok": result.returncode == 0, "trading_day": today, "detail": detail[-2000:]},
    )
    print(detail or ("git pull finished" if result.returncode == 0 else "git pull failed"))
    return result.returncode


def install_schedule(
    config_path: Path,
    config: Mapping[str, Any],
    *,
    python: str,
    agents_dir: Path,
    domain: str,
    load: bool = True,
) -> list[Path]:
    """Install one checker. It fires on Asia/Taipei time, not the Mac time zone."""
    root = config_path.parent
    schedule_slots(config)
    agents_dir.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    for job in ("git_pull", "order"):
        _remove_agent(agents_dir, domain, _label(config_path, job), load=load)
    label = _label(config_path, "tick")
    plist_path = agents_dir / f"{label}.plist"
    payload = {
        "Label": label,
        "WorkingDirectory": str(root),
        "ProgramArguments": [python, "-m", "order_desk", "--weekly-tick", "--config", str(config_path)],
        "StartInterval": 60,
        "StandardOutPath": str(root / "logs" / "schedule.log"),
        "StandardErrorPath": str(root / "logs" / "schedule.log"),
    }
    plist_path.write_bytes(plistlib.dumps(payload))
    if load:
        _launchctl_load(domain, label, plist_path)
    return [plist_path]


def uninstall_schedule(
    config_path: Path,
    *,
    agents_dir: Path,
    domain: str,
    load: bool = True,
) -> list[str]:
    """Remove this computer's LaunchAgent tick (and any legacy pull/order jobs)."""
    removed: list[str] = []
    for job in ("git_pull", "order", "tick"):
        label = _label(config_path, job)
        _remove_agent(agents_dir, domain, label, load=load)
        removed.append(label)
    return removed


def git_root(start: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SignalError("order desk is not inside a git checkout")
    return Path(result.stdout.strip())


def _slot(raw: Any, default_day: str, default_time: str) -> dict[str, int]:
    if not isinstance(raw, dict):
        raw = {}
    day = str(raw.get("weekday", default_day)).lower()
    if day not in WEEKDAYS:
        raise SignalError(f"schedule weekday must be a day name, got {day}")
    hour, minute = _clock(str(raw.get("time", default_time)))
    return {"day": day, "hour": hour, "minute": minute}


def _time_reached(local: datetime, slot: Mapping[str, int | str]) -> bool:
    current = local.hour * 60 + local.minute
    scheduled = int(slot["hour"]) * 60 + int(slot["minute"])
    return current >= scheduled


def _is_session_day(
    today: date,
    slot: Mapping[str, int | str],
    is_trading_day: Callable[[date], bool],
) -> bool:
    """True when today is the first TWSE session on or after the configured weekday."""
    earliest = WEEKDAYS[str(slot["day"])]
    monday = today - timedelta(days=today.weekday())
    start = monday + timedelta(days=earliest)
    friday = monday + timedelta(days=4)
    if today < start or today > friday:
        return False
    day = start
    while day <= friday:
        if is_trading_day(day):
            return day == today
        day += timedelta(days=1)
    return False


def _is_catchup_day(
    today: date,
    slot: Mapping[str, int | str],
    is_trading_day: Callable[[date], bool],
) -> bool:
    """True on trading days from the week's first open session through Friday."""
    earliest = WEEKDAYS[str(slot["day"])]
    monday = today - timedelta(days=today.weekday())
    start = monday + timedelta(days=earliest)
    friday = monday + timedelta(days=4)
    if today < start or today > friday or not is_trading_day(today):
        return False
    day = start
    while day <= friday:
        if is_trading_day(day):
            return today >= day
        day += timedelta(days=1)
    return False


def _clock(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise SignalError(f"schedule time must be HH:MM, got {value}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise SignalError(f"schedule time must be HH:MM, got {value}")
    return hour, minute


def _label(config_path: Path, job: str) -> str:
    digest = hashlib.sha256(str(config_path).encode("utf-8")).hexdigest()[:8]
    slug = {"git_pull": "pull", "order": "order", "tick": "tick"}[job]
    return f"com.quantplatform.orderdesk.{digest}.{slug}"


def _remove_agent(agents_dir: Path, domain: str, label: str, *, load: bool) -> None:
    if load:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=False, capture_output=True)
    plist_path = agents_dir / f"{label}.plist"
    if plist_path.is_file():
        plist_path.unlink()


def _read_stamp(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_stamp(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _launchctl_load(domain: str, label: str, plist_path: Path) -> None:
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=False, capture_output=True)
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SignalError(f"could not install {label}: {detail}")
    subprocess.run(["launchctl", "enable", f"{domain}/{label}"], check=False, capture_output=True)
