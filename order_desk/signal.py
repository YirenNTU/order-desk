"""Load a target-weight JSON file. No public key is required."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class SignalError(ValueError):
    """The signal file cannot be used."""


def load_signal(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SignalError(f"cannot read signal file: {path}") from exc
    if not isinstance(payload, dict):
        raise SignalError("signal file must be a JSON object")
    required = (
        "bundle_id",
        "revision",
        "effective_at",
        "expires_at",
        "data_as_of",
        "strategies",
    )
    missing = [key for key in required if key not in payload]
    if missing:
        raise SignalError(f"signal file missing fields: {', '.join(missing)}")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    effective = _time(payload["effective_at"], "effective_at")
    expires = _time(payload["expires_at"], "expires_at")
    if current < effective:
        raise SignalError(f"signal is not effective until {payload['effective_at']}")
    if current >= expires:
        raise SignalError(f"signal expired at {payload['expires_at']}")
    strategies = payload["strategies"]
    if not isinstance(strategies, list) or not strategies:
        raise SignalError("signal strategies must be a non-empty list")
    return payload


def budgets_for(signal: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, float]:
    """One amount per strategy. A single budget_twd applies only when the file has one strategy."""
    ids = [str(item["strategy_id"]) for item in signal["strategies"]]
    raw = config.get("budgets")
    if isinstance(raw, dict) and raw:
        missing = [strategy_id for strategy_id in ids if strategy_id not in raw]
        if missing:
            raise SignalError(
                "config budgets missing strategy ids: " + ", ".join(missing)
            )
        return {strategy_id: float(raw[strategy_id]) for strategy_id in ids}
    if "budget_twd" in config:
        if len(ids) != 1:
            raise SignalError(
                "this signal has multiple strategies; set budgets.<strategy_id> for each"
            )
        return {ids[0]: float(config["budget_twd"])}
    raise SignalError("config needs budget_twd or budgets")


def _time(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise SignalError(f"{name} must be a timestamp string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SignalError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)
