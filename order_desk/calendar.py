"""Fail-closed TWSE holiday calendar backed by the official OpenAPI endpoint."""

from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


TWSE_HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"


class CalendarUnavailable(RuntimeError):
    """The official calendar could not be validated for the requested year."""


def _holiday_date(value: str, year: int) -> date:
    text = value.strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) == 8:
        return datetime.strptime(digits, "%Y%m%d").date()
    if len(digits) == 7:
        roc_year = int(digits[:3]) + 1911
        return date(roc_year, int(digits[3:5]), int(digits[5:7]))
    match = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", text)
    if match:
        return date(year, int(match.group(1)), int(match.group(2)))
    match = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", text)
    if match:
        return date(year, int(match.group(1)), int(match.group(2)))
    raise CalendarUnavailable(f"unrecognized TWSE holiday date: {value!r}")


class TwseCalendar:
    def __init__(self, cache_path: str | Path, timeout_seconds: int = 10):
        self.cache_path = Path(cache_path)
        self.timeout_seconds = timeout_seconds

    def _read_cache(self) -> dict[str, Any] | None:
        if not self.cache_path.is_file():
            return None
        try:
            value = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != 1
            or value.get("source") != TWSE_HOLIDAY_URL
            or not isinstance(value.get("year"), int)
            or not isinstance(value.get("holidays"), list)
        ):
            return None
        return value

    def refresh(self, year: int) -> dict[str, Any]:
        request = urllib.request.Request(
            TWSE_HOLIDAY_URL,
            headers={"User-Agent": "Quant-Platform-Order-Desk/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read())
        except Exception as exc:
            raise CalendarUnavailable("TWSE holiday API is unavailable") from exc
        if not isinstance(payload, list) or not payload:
            raise CalendarUnavailable("TWSE holiday API returned no rows")
        holidays: set[str] = set()
        for row in payload:
            if not isinstance(row, dict):
                raise CalendarUnavailable("TWSE holiday API returned an invalid row")
            raw_date = row.get("Date") or row.get("date") or row.get("日期")
            if not isinstance(raw_date, str):
                raise CalendarUnavailable("TWSE holiday API row has no date")
            parsed = _holiday_date(raw_date, year)
            if parsed.year != year:
                raise CalendarUnavailable(f"TWSE holiday response is for {parsed.year}, not {year}")
            holidays.add(parsed.isoformat())
        value = {
            "schema_version": 1,
            "source": TWSE_HOLIDAY_URL,
            "year": year,
            "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "holidays": sorted(holidays),
        }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(dir=self.cache_path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary_name, self.cache_path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
        return value

    def ensure_year(self, year: int) -> dict[str, Any]:
        cached = self._read_cache()
        if cached is not None and cached["year"] == year:
            return cached
        return self.refresh(year)

    def is_trading_day(self, value: date) -> bool:
        if value.weekday() >= 5:
            return False
        calendar = self.ensure_year(value.year)
        return value.isoformat() not in set(calendar["holidays"])
