"""Per-strategy share ledger.

The account can hold several strategies plus shares bought outside them.
Only shares recorded under a strategy name are rebalanced.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from order_desk.models import Quote
from order_desk.planner import _price_for_size
from order_desk.signal import SignalError

TERMINAL_ORDER_STATUSES = {"Filled", "Cancelled", "Failed"}


def empty_ledger() -> dict[str, Any]:
    return {"strategies": {}, "open_orders": []}


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return empty_ledger()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SignalError(f"cannot read ledger: {path}") from exc
    if not isinstance(payload, dict):
        raise SignalError("ledger must be a JSON object")
    if "positions" in payload and "strategies" not in payload:
        raise SignalError("ledger is missing per-strategy share records")
    strategies = payload.get("strategies", {})
    open_orders = payload.get("open_orders", [])
    if not isinstance(strategies, dict) or not isinstance(open_orders, list):
        raise SignalError("ledger strategies and open_orders have the wrong shape")
    clean: dict[str, dict[str, int]] = {}
    for name, positions in strategies.items():
        if not isinstance(positions, dict):
            raise SignalError(f"{name}: ledger positions must be a ticker map")
        held: dict[str, int] = {}
        for ticker, shares in positions.items():
            if isinstance(shares, bool) or not isinstance(shares, int) or shares < 0:
                raise SignalError(f"{name} {ticker}: shares must be a non-negative integer")
            if shares:
                held[str(ticker)] = shares
        if held:
            clean[str(name)] = held
    return {"strategies": clean, "open_orders": [dict(item) for item in open_orders]}


def save_ledger(path: Path, ledger: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(
            {"strategies": ledger["strategies"], "open_orders": ledger["open_orders"]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def load_account_ledger(root: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    """Load one ledger file per strategy folder into the combined in-memory book."""
    strategy_root = root / "strategies"
    files = sorted(strategy_root.glob("*/ledger.json")) if strategy_root.is_dir() else []
    legacy = root / "ledger.json"
    if legacy.is_file() and files:
        raise SignalError("ledger.json and per-strategy ledgers both exist")
    if legacy.is_file():
        ledger = load_ledger(legacy)
        paths = {name: strategy_root / name / "ledger.json" for name in _ledger_names(ledger)}
        return ledger, paths

    combined = empty_ledger()
    paths: dict[str, Path] = {}
    seen: dict[str, str] = {}
    for path in files:
        name = path.parent.name
        positions, orders = _read_strategy_ledger(path, name)
        paths[name] = path
        if positions:
            combined["strategies"][name] = positions
        for order in orders:
            order_id = str(order.get("broker_order_id", ""))
            if not order_id:
                raise SignalError(f"{name}: open order is missing broker_order_id")
            blob = json.dumps(order, sort_keys=True, ensure_ascii=False)
            previous = seen.get(order_id)
            if previous is not None:
                if previous != blob:
                    raise SignalError(f"{order_id}: strategy ledgers disagree")
                continue
            seen[order_id] = blob
            combined["open_orders"].append(order)
    return combined, paths


def save_account_ledger(root: Path, ledger: Mapping[str, Any], paths: dict[str, Path]) -> None:
    """Write each strategy's positions and orders into its own ledger file."""
    names = _ledger_names(ledger)
    for name in names:
        paths.setdefault(name, root / "strategies" / name / "ledger.json")
    for name, path in paths.items():
        positions = dict(ledger["strategies"].get(name, {}))
        orders = [
            dict(order)
            for order in ledger["open_orders"]
            if _order_belongs_to(order, name)
        ]
        _write_strategy_ledger(path, positions, orders)
    legacy = root / "ledger.json"
    if legacy.is_file():
        legacy.unlink()


def _ledger_names(ledger: Mapping[str, Any]) -> set[str]:
    names = {str(name) for name in ledger["strategies"]}
    for order in ledger["open_orders"]:
        legs = order.get("allocation") or []
        if not legs:
            raise SignalError(f"{order.get('ticker', '?')}: open order has no strategy allocation")
        names.update(str(leg["strategy"]) for leg in legs)
    return names


def _order_belongs_to(order: Mapping[str, Any], name: str) -> bool:
    return any(str(leg.get("strategy")) == name for leg in order.get("allocation", []))


def _read_strategy_ledger(path: Path, name: str) -> tuple[dict[str, int], list[dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SignalError(f"cannot read ledger: {path}") from exc
    if not isinstance(payload, dict) or "strategies" in payload:
        raise SignalError(f"{name}: ledger file should hold only this strategy")
    positions = payload.get("positions", {})
    orders = payload.get("open_orders", [])
    if not isinstance(positions, dict) or not isinstance(orders, list):
        raise SignalError(f"{name}: ledger positions and open_orders have the wrong shape")
    held: dict[str, int] = {}
    for ticker, shares in positions.items():
        if isinstance(shares, bool) or not isinstance(shares, int) or shares < 0:
            raise SignalError(f"{name} {ticker}: shares must be a non-negative integer")
        if shares:
            held[str(ticker)] = shares
    clean_orders: list[dict[str, Any]] = []
    for order in orders:
        if not isinstance(order, dict) or not _order_belongs_to(order, name):
            raise SignalError(f"{name}: open order is not allocated to this strategy")
        clean_orders.append(dict(order))
    return held, clean_orders


def _write_strategy_ledger(path: Path, positions: Mapping[str, int], orders: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"positions": positions, "open_orders": orders}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def holdings_value(positions: Mapping[str, int], quotes: Mapping[str, Quote]) -> float:
    total = 0.0
    for ticker, shares in positions.items():
        amount = int(shares)
        if amount <= 0:
            continue
        quote = quotes.get(str(ticker))
        if quote is None:
            raise SignalError(f"{ticker}: quote required to value current holdings")
        total += amount * _price_for_size(quote)
    return total


def combined_shares(strategies: Mapping[str, Mapping[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for positions in strategies.values():
        for ticker, shares in positions.items():
            amount = int(shares)
            if amount:
                total[str(ticker)] = total.get(str(ticker), 0) + amount
    return total


def target_value(
    name: str,
    positions: Mapping[str, int],
    quotes: Mapping[str, Quote],
    extra_twd: float,
    cash_out_twd: float,
) -> tuple[float, str]:
    """Current marked value, plus new money, minus a withdrawal."""
    extra = _nonnegative(name, "extra_twd", extra_twd)
    cash_out = _nonnegative(name, "cash_out_twd", cash_out_twd)
    value = holdings_value(positions, quotes)
    target = value + extra - cash_out
    if target < -1e-6:
        raise SignalError(
            f"{name}: cash_out_twd {cash_out:,.0f} exceeds holdings {value:,.0f} plus extra {extra:,.0f}"
        )
    note = f"{name}: holdings {value:,.0f} + extra {extra:,.0f} - cash out {cash_out:,.0f} = {target:,.0f}"
    return max(0.0, target), note


def apply_fill(positions: dict[str, int], ticker: str, action: str, shares: int) -> None:
    if shares < 0:
        raise SignalError(f"{ticker}: fill shares cannot be negative")
    current = int(positions.get(ticker, 0))
    updated = current + shares if action == "Buy" else current - shares
    if updated < 0:
        raise SignalError(f"{ticker}: fill exceeds the strategy ledger")
    if updated == 0:
        positions.pop(ticker, None)
    else:
        positions[ticker] = updated


def filled_shares(row: Mapping[str, Any], order_lot: str) -> int:
    if "filled_shares" in row:
        return int(row["filled_shares"])
    quantity = int(row.get("filled_quantity", 0))
    return quantity * 1000 if order_lot == "Common" else quantity


def _nonnegative(name: str, label: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SignalError(f"{name}: {label} must be a number")
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise SignalError(f"{name}: {label} cannot be negative")
    return amount
