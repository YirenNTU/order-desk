"""Several strategies, one account, separate share records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from order_desk.capital import combined_shares, target_value
from order_desk.models import ExecutionPolicy, Quote
from order_desk.planner import PlanningError, build_order_plan
from order_desk.signal import SignalError, load_signal


def multi_strategy_mode(config: Mapping[str, Any]) -> bool:
    return isinstance(config.get("strategies"), list)


def load_strategy_entries(
    config: Mapping[str, Any], root: Path, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    rows = config.get("strategies")
    if not isinstance(rows, list) or not rows:
        raise SignalError("config strategies must list at least one strategy")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise SignalError("each strategy config must be an object")
        name = str(row.get("name", "")).strip()
        if not name:
            raise SignalError("each strategy needs a name")
        if name in seen:
            raise SignalError(f"{name}: duplicate strategy name")
        seen.add(name)
        signal_path = Path(str(row.get("signal_path", ""))).expanduser()
        if not signal_path.is_absolute():
            signal_path = (root / signal_path).resolve()
        signal = load_signal(signal_path, now=now)
        strategies = signal["strategies"]
        if len(strategies) != 1:
            raise SignalError(f"{name}: signal file must contain one strategy")
        strategy = strategies[0]
        if str(strategy["strategy_id"]) != name:
            raise SignalError(f"{name}: signal strategy_id does not match the config name")
        entries.append(
            {
                "name": name,
                "path": signal_path,
                "signal": signal,
                "strategy": strategy,
                "extra_twd": row.get("extra_twd", 0),
                "cash_out_twd": row.get("cash_out_twd", 0),
            }
        )
    return entries


def plan_strategies(
    entries: list[dict[str, Any]],
    ledger: Mapping[str, Any],
    quotes: Mapping[str, Quote],
    broker_positions: Mapping[str, int],
    available_cash: float,
    policy: ExecutionPolicy,
    *,
    mode: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Value each strategy at the live price, add its cash, then size shares at that price."""
    configured = {entry["name"] for entry in entries}
    unknown = sorted(set(ledger["strategies"]) - configured)
    if unknown:
        raise SignalError(
            "ledger has strategies missing from config: " + ", ".join(unknown)
        )
    notes: list[str] = []
    budgets: dict[str, float] = {}
    active: list[dict[str, Any]] = []
    for entry in entries:
        name = entry["name"]
        held = {
            str(ticker): int(shares)
            for ticker, shares in ledger["strategies"].get(name, {}).items()
        }
        value, note = target_value(
            name, held, quotes, entry["extra_twd"], entry["cash_out_twd"]
        )
        notes.append(note)
        if value <= 0 and not held:
            notes.append(f"{name}: skipped, no holdings and no extra money")
            continue
        budgets[name] = value
        active.append(entry)
    if not active:
        raise SignalError("no strategy has holdings or extra money")

    current = now or datetime.now(timezone.utc)
    bundle = _merge_bundle(active, current)
    managed = combined_shares(
        {entry["name"]: ledger["strategies"].get(entry["name"], {}) for entry in active}
    )
    try:
        plan = build_order_plan(
            bundle,
            budgets,
            quotes,
            managed,
            broker_positions,
            available_cash,
            policy,
            mode=mode,
            now=current,
        )
    except PlanningError as exc:
        raise SignalError(str(exc)) from exc
    before = {entry["name"]: dict(ledger["strategies"].get(entry["name"], {})) for entry in active}
    targets = {name: dict(shares) for name, shares in plan.sleeve_targets.items()}
    booked = book_internal(before, targets)
    residuals = signed_diff(booked, targets)
    allocations = tag_orders(plan.orders, residuals)
    return {
        "plan": plan,
        "notes": notes,
        "before": before,
        "targets": targets,
        "booked": booked,
        "allocations": allocations,
        "paths": {entry["name"]: entry["path"] for entry in entries},
    }


def book_internal(
    current: Mapping[str, Mapping[str, int]],
    targets: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int]]:
    """Move overlapping shares between strategies. The market order is only the net."""
    booked = {
        name: {ticker: int(shares) for ticker, shares in positions.items() if int(shares)}
        for name, positions in current.items()
    }
    names = sorted(set(current) | set(targets))
    for name in names:
        booked.setdefault(name, {})
    tickers = sorted(
        {
            ticker
            for book in list(current.values()) + list(targets.values())
            for ticker in book
        }
    )
    for ticker in tickers:
        sellers: list[list[Any]] = []
        buyers: list[list[Any]] = []
        for name in names:
            delta = int(targets.get(name, {}).get(ticker, 0)) - int(booked[name].get(ticker, 0))
            if delta < 0:
                sellers.append([name, -delta])
            elif delta > 0:
                buyers.append([name, delta])
        for seller in sellers:
            for buyer in buyers:
                move = min(seller[1], buyer[1])
                if not move:
                    continue
                _add(booked, seller[0], ticker, -move)
                _add(booked, buyer[0], ticker, move)
                seller[1] -= move
                buyer[1] -= move
    return {name: positions for name, positions in booked.items() if positions}


def signed_diff(
    current: Mapping[str, Mapping[str, int]],
    targets: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int]]:
    residuals: dict[str, dict[str, int]] = {}
    for name in sorted(set(current) | set(targets)):
        deltas: dict[str, int] = {}
        tickers = set(current.get(name, {})) | set(targets.get(name, {}))
        for ticker in tickers:
            delta = int(targets.get(name, {}).get(ticker, 0)) - int(current.get(name, {}).get(ticker, 0))
            if delta:
                deltas[str(ticker)] = delta
        if deltas:
            residuals[name] = deltas
    return residuals


def tag_orders(orders: Any, residuals: Mapping[str, Mapping[str, int]]) -> dict[str, list[dict[str, Any]]]:
    remaining = {name: dict(deltas) for name, deltas in residuals.items()}
    tags: dict[str, list[dict[str, Any]]] = {}
    for order in orders:
        need = int(order.shares)
        legs: list[dict[str, Any]] = []
        for name in sorted(remaining):
            signed = int(remaining[name].get(order.ticker, 0))
            if order.action == "Sell" and signed < 0:
                take = min(need, -signed)
                signed_after = signed + take
            elif order.action == "Buy" and signed > 0:
                take = min(need, signed)
                signed_after = signed - take
            else:
                continue
            if not take:
                continue
            legs.append({"strategy": name, "shares": take, "applied": 0})
            if signed_after == 0:
                remaining[name].pop(order.ticker, None)
            else:
                remaining[name][order.ticker] = signed_after
            need -= take
        if need:
            raise SignalError(f"{order.ticker}: order does not match per-strategy share records")
        tags[order.client_order_id] = legs
    leftover = {name: deltas for name, deltas in remaining.items() if deltas}
    if leftover:
        raise SignalError("strategy share changes were not fully assigned to orders")
    return tags


def apply_book(ledger: dict[str, Any], booked: Mapping[str, Mapping[str, int]]) -> None:
    ledger["strategies"] = {
        name: {ticker: int(shares) for ticker, shares in positions.items() if int(shares)}
        for name, positions in booked.items()
        if any(int(shares) for shares in positions.values())
    }


def _add(booked: dict[str, dict[str, int]], name: str, ticker: str, delta: int) -> None:
    updated = int(booked[name].get(ticker, 0)) + delta
    if updated < 0:
        raise SignalError(f"{name} {ticker}: strategy ledger cannot go negative")
    if updated == 0:
        booked[name].pop(ticker, None)
    else:
        booked[name][ticker] = updated


def _merge_bundle(entries: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    effective = max(_stamp(entry["signal"]["effective_at"]) for entry in entries)
    expires = min(_stamp(entry["signal"]["expires_at"]) for entry in entries)
    if effective >= expires:
        raise SignalError("strategy signals do not share an execution window")
    identity = [
        {
            "name": entry["name"],
            "data_as_of": entry["signal"]["data_as_of"],
            "revision": entry["signal"]["revision"],
            "weights": entry["strategy"]["weights"],
        }
        for entry in entries
    ]
    bundle_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]
    dates = {str(entry["signal"]["data_as_of"]) for entry in entries}
    data_as_of = next(iter(dates)) if len(dates) == 1 else ", ".join(sorted(dates))
    current = now.astimezone(timezone.utc)
    if current < effective or current >= expires:
        raise SignalError("strategy signals do not share an execution window")
    return {
        "bundle_id": bundle_id,
        "revision": max(int(entry["signal"]["revision"]) for entry in entries),
        "effective_at": effective.isoformat(),
        "expires_at": expires.isoformat(),
        "data_as_of": data_as_of,
        "strategies": [entry["strategy"] for entry in entries],
    }


def _stamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SignalError("signal timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)
