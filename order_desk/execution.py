"""Send sells, wait until they finish, then send buys."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from order_desk.capital import (
    TERMINAL_ORDER_STATUSES,
    apply_fill,
    filled_shares,
)
from order_desk.models import OrderIntent
from order_desk.signal import SignalError


def taipei_today() -> str:
    return datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat()


def refresh_open_orders(worker: Any, ledger: dict[str, Any], *, today: str | None = None) -> bool:
    """Apply newly reported fills. Return False when a stored order cannot be confirmed."""
    open_orders = ledger["open_orders"]
    if not open_orders:
        return True
    current_day = today or taipei_today()
    rows = worker.call("reconcile_orders")
    by_id = {str(row.get("broker_order_id", "")): row for row in rows}
    still_open: list[dict[str, Any]] = []
    confirmed = True
    for order in open_orders:
        row = by_id.get(str(order.get("broker_order_id", "")))
        if row is None:
            if _placed_before_today(order, current_day):
                order["status"] = "Expired"
                continue
            order["status"] = "Unconfirmed"
            still_open.append(order)
            confirmed = False
            continue
        filled = filled_shares(row, str(order["order_lot"]))
        applied = int(order.get("applied_shares", 0))
        if filled < applied:
            raise SignalError(f"{order['ticker']}: broker fill moved backward")
        if filled > int(order["shares"]):
            raise SignalError(f"{order['ticker']}: broker fill exceeds the order")
        if filled > applied:
            _apply_allocated_fill(ledger, order, filled)
        status = str(row.get("status", "")).split(".")[-1]
        order["status"] = status
        if status not in TERMINAL_ORDER_STATUSES:
            still_open.append(order)
            continue
        if int(order.get("applied_shares", 0)) >= int(order["shares"]):
            continue
        if _placed_before_today(order, current_day):
            order["status"] = "Expired"
            continue
        still_open.append(order)
    ledger["open_orders"] = still_open
    return confirmed


def _placed_before_today(order: Mapping[str, Any], today: str) -> bool:
    placed_on = str(order.get("trading_day", ""))
    return bool(placed_on) and placed_on < today


def submit_sells_then_buys(
    worker: Any,
    orders: tuple[OrderIntent, ...] | list[OrderIntent],
    ledger: dict[str, Any],
    allocations: Mapping[str, list[dict[str, Any]]],
    *,
    cash_reserve_twd: float,
    wait_seconds: float,
    poll_seconds: float,
    cash_check: bool = False,
) -> str:
    """Place sells first. Buys go out only after every sell is filled and cash covers them."""
    if ledger["open_orders"]:
        raise SignalError("open orders are still working; no new orders were sent")
    sells = [order for order in orders if order.action == "Sell"]
    buys = [order for order in orders if order.action == "Buy"]
    for order in sells:
        result = worker.call("place_order", order)
        _track(ledger, order, result, allocations)
    refresh_open_orders(worker, ledger)
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while any(order["action"] == "Sell" for order in ledger["open_orders"]):
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.0, poll_seconds))
        refresh_open_orders(worker, ledger)
    if any(order["action"] == "Sell" for order in ledger["open_orders"]):
        return "sells_working"
    buy_twd = sum(order.estimated_notional for order in buys)
    buying_power = max(0.0, float(worker.call("available_cash")) - cash_reserve_twd)
    if cash_check and buy_twd > buying_power + 1e-6:
        return "buys_waiting_for_cash"
    for order in buys:
        result = worker.call("place_order", order)
        _track(ledger, order, result, allocations)
    refresh_open_orders(worker, ledger)
    if ledger["open_orders"]:
        return "buys_working"
    return "completed"


def _track(
    ledger: dict[str, Any],
    order: OrderIntent,
    result: Any,
    allocations: Mapping[str, list[dict[str, Any]]],
) -> None:
    legs = allocations.get(order.client_order_id)
    if not legs:
        raise SignalError(f"{order.ticker}: order has no strategy allocation")
    ledger["open_orders"].append(
        {
            "broker_order_id": str(result.broker_order_id),
            "ticker": order.ticker,
            "action": order.action,
            "shares": order.shares,
            "order_lot": order.order_lot,
            "applied_shares": 0,
            "status": str(result.status).split(".")[-1],
            "trading_day": taipei_today(),
            "allocation": [
                {"strategy": str(leg["strategy"]), "shares": int(leg["shares"]), "applied": 0}
                for leg in legs
            ],
        }
    )


def _apply_allocated_fill(ledger: dict[str, Any], order: dict[str, Any], filled_now: int) -> None:
    applied = int(order.get("applied_shares", 0))
    delta = filled_now - applied
    for leg in order.get("allocation", []):
        room = int(leg["shares"]) - int(leg.get("applied", 0))
        take = min(room, delta)
        if take <= 0:
            continue
        bucket = ledger["strategies"].setdefault(str(leg["strategy"]), {})
        apply_fill(bucket, str(order["ticker"]), str(order["action"]), take)
        leg["applied"] = int(leg.get("applied", 0)) + take
        delta -= take
    if delta:
        raise SignalError(f"{order['ticker']}: fill exceeds the strategy allocation")
    order["applied_shares"] = filled_now
    ledger["strategies"] = {
        name: positions
        for name, positions in ledger["strategies"].items()
        if positions
    }
