"""Send sells, wait until they finish, then send buys."""

from __future__ import annotations

import time
import copy
import uuid
from dataclasses import asdict, replace
from collections.abc import Callable
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from order_desk.broker import BrokerError
from order_desk.inventory import ReplanRequired
from order_desk.deferrals import cycle_id, frozen_tickers

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
    working = copy.deepcopy(ledger)
    confirmed = _refresh_orders(worker, working, today=today)
    ledger.clear()
    ledger.update(working)
    return confirmed


def _refresh_orders(worker, ledger, *, today=None):
    open_orders = ledger["open_orders"]
    if not open_orders:
        return True
    current_day = today or taipei_today()
    rows = worker.call("reconcile_orders")
    by_id = {}
    for row in rows:
        key = str(row.get("broker_order_id", ""))
        if key in by_id:
            raise SignalError("duplicate broker order identity; reconcile manually")
        if key:
            by_id[key] = row
    still_open: list[dict[str, Any]] = []
    confirmed = True
    for order in open_orders:
        row = by_id.get(str(order.get("broker_order_id", "")))
        if row is None and order.get("custom_field") and order.get("trading_day") == current_day:
            matches = [r for r in rows if r.get("custom_field") == order["custom_field"]]
            if len(matches) > 1:
                raise SignalError("ambiguous submitted order; reconcile manually")
            if matches:
                row = matches[0]
        # Today's query cannot prove yesterday's missing order was unfilled.
        # A previously saved terminal broker snapshot is conclusive evidence.
        if row is None and _placed_before_today(order, current_day):
            saved = order.get('broker_snapshot', {})
            if (str(saved.get('status', '')).split('.')[-1] in TERMINAL_ORDER_STATUSES
                    and str(saved.get('broker_order_id', '')) == str(order.get('broker_order_id', ''))):
                row = saved
        if row is None:
            order["status"] = "Unconfirmed"
            still_open.append(order)
            confirmed = False
            continue
        for key in ("ticker", "action", "order_lot"):
            if key in row and str(row[key]).split(".")[-1] != str(order[key]):
                raise SignalError(f"{order['ticker']}: broker order identity mismatch ({key})")
        row_day = str(row.get('trading_day') or row.get('order_datetime') or '')[:10]
        if row_day and row_day != order.get("trading_day"):
            raise SignalError("broker order trading day mismatch")
        order["broker_order_id"] = str(row["broker_order_id"])
        filled = filled_shares(row, str(order["order_lot"]))
        applied = int(order.get("applied_shares", 0))
        if filled < applied:
            raise SignalError(f"{order['ticker']}: broker fill moved backward")
        if filled > int(order["shares"]):
            raise SignalError(f"{order['ticker']}: broker fill exceeds the order")
        if filled > applied:
            _apply_allocated_fill(ledger, order, filled)
        order["broker_snapshot"] = copy.deepcopy(row)
        status = str(row.get("status", "")).split(".")[-1]
        order["status"] = status
        if status not in TERMINAL_ORDER_STATUSES:
            still_open.append(order)
            continue
        if int(order.get("applied_shares", 0)) >= int(order["shares"]):
            ledger.setdefault("order_history", []).append(copy.deepcopy(order))
            continue
        if _placed_before_today(order, current_day):
            ledger.setdefault("order_history", []).append(copy.deepcopy(order))
            continue
        still_open.append(order)
    ledger["open_orders"] = still_open
    return confirmed


def prepare_next_day_execution(ledger, *, today=None):
    """Reprice only the remaining target after all earlier orders are resolved."""
    today = today or taipei_today()
    pending = ledger.get('execution')
    if not pending or pending.get('trading_day', today) >= today:
        return False
    if ledger.get('open_orders'):
        raise SignalError('prior-day orders still need confirmed final results')
    if not pending.get('budgets') or not pending.get('targets') or not pending.get('signal_signature'):
        raise SignalError('prior-day original budgets and targets missing; recover the saved plan first')
    if pending.get('retry_prepared_for') == today:
        return False
    pending['reweight_required'] = True
    pending['retry_prepared_for'] = today
    ledger.setdefault('daily_retry_history', []).append({
        'previous_trading_day': pending['trading_day'], 'retry_day': today,
        'reason': 'confirmed prior orders reconciled; reprice remaining targets',
        'budgets': copy.deepcopy(pending['budgets']),
        'targets': copy.deepcopy(pending['targets']),
    })
    return True


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
    persist: Callable[[], None] | None = None,
    preflight: Callable[[], None] | None = None,
) -> str:
    """Place sells first. Buys go out only after every sell is filled and cash covers them."""
    if ledger["open_orders"]:
        raise SignalError("open orders are still working; no new orders were sent")
    pending = ledger.get("execution")
    if pending:
        if pending.get('reweight_required'):
            raise SignalError('deferred orders require a reweighted plan; old buys cannot be resumed')
        if pending["trading_day"] != taipei_today():
            raise SignalError("unfinished execution from a prior day; reconcile and review remaining targets before sending")
        orders = [OrderIntent(**row) for row in pending["orders"]]
        allocations = pending["allocations"]
    else:
        for order in orders:
            legs = allocations.get(order.client_order_id, [])
            if not legs or sum(int(leg["shares"]) for leg in legs) != order.shares or any(int(leg["shares"]) <= 0 for leg in legs):
                raise SignalError(f"{order.ticker}: invalid strategy allocation")
        pending = {"trading_day": taipei_today(), "orders": [asdict(o) for o in orders],
                   "allocations": copy.deepcopy(allocations), "submitted": []}
        ledger["execution"] = pending
    if any(order.ticker in ledger.get('inventory_quarantine', {}) for order in orders):
        ledger['execution']['reweight_required'] = True
        raise ReplanRequired('saved orders include quarantined inventory; rebuild the remaining plan')
    sells = [order for order in orders if order.action == "Sell" and order.client_order_id not in pending["submitted"]]
    buys = [order for order in orders if order.action == "Buy" and order.client_order_id not in pending["submitted"]]

    def remember() -> None:
        if persist is not None:
            persist()

    remember()
    for order in sells:
        _submit_one(worker, ledger, order, allocations, pending, remember)
    if not _refresh_or_keep(worker, ledger):
        remember()
        return "sells_working"
    remember()
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while any(order["action"] == "Sell" for order in ledger["open_orders"]):
        if time.monotonic() >= deadline:
            break
        time.sleep(max(0.0, poll_seconds))
        if not _refresh_or_keep(worker, ledger):
            remember()
            return "sells_working"
        remember()
    if any(order["action"] == "Sell" for order in ledger["open_orders"]):
        return "sells_working"
    if buys and preflight is not None:
        preflight()
    buy_twd = sum(order.estimated_notional for order in buys)
    if cash_check and buys:
        try:
            buying_power = max(0.0, float(worker.call("available_cash")) - cash_reserve_twd)
        except BrokerError:
            return "buys_waiting_for_cash"
        if buy_twd > buying_power + 1e-6:
            return "buys_waiting_for_cash"
    for order in buys:
        _submit_one(worker, ledger, order, allocations, pending, remember)
    if not _refresh_or_keep(worker, ledger):
        remember()
        return "buys_working"
    remember()
    if ledger["open_orders"]:
        return "buys_working"
    completed = ledger.pop("execution", None)
    if completed:
        completed['outcome'] = 'completed_with_deferrals' if frozen_tickers(ledger, cycle_id()) else 'completed'
        ledger.setdefault('execution_history', []).append(copy.deepcopy(completed))
    remember()
    return 'completed_with_deferrals' if frozen_tickers(ledger, cycle_id()) else 'completed'


def _submit_one(worker, ledger, order, allocations, pending, remember):
    pending = ledger["execution"]  # reconciliation replaces the staged ledger atomically
    used = {o.get("custom_field") for o in ledger.get("order_history", []) + ledger["open_orders"]}
    token = uuid.uuid4().hex
    while token[:6] in used:
        token = uuid.uuid4().hex
    result = type("Pending", (), {"broker_order_id": "pending:" + token, "status": "Submitting"})()
    _track(ledger, order, result, allocations)
    tracked = ledger["open_orders"][-1]
    tracked["client_order_id"] = order.client_order_id
    tracked["custom_field"] = token[:6]
    tracked["limit_price"] = order.limit_price
    pending["submitted"].append(order.client_order_id)
    remember()  # Must succeed before the broker can receive the order.
    try:
        result = worker.call("place_order", replace(order, client_order_id=token))
    except Exception:
        tracked["status"] = "SubmissionUnknown"
        remember()
        raise
    if not result.broker_order_id:
        tracked["status"] = "SubmissionUnknown"
        remember()
        raise SignalError("broker returned no order identity; manual reconciliation required")
    tracked["broker_order_id"] = str(result.broker_order_id)
    tracked["status"] = str(result.status).split(".")[-1]
    # Record an immediate fill even if the following status query disconnects.
    filled = int(getattr(result, "filled_shares", 0))
    if filled:
        if filled < 0 or filled > order.shares:
            raise SignalError("invalid immediate broker fill")
        staged = copy.deepcopy(ledger)
        _apply_allocated_fill(staged, staged["open_orders"][-1], filled)
        ledger["strategies"] = staged["strategies"]
        tracked.update(staged["open_orders"][-1])
    remember()


def _refresh_or_keep(worker: Any, ledger: dict[str, Any]) -> bool:
    """Refresh fills. A dropped connection leaves the already accepted orders in place."""
    try:
        return refresh_open_orders(worker, ledger)
    except BrokerError:
        return False
    return True


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


def retry_terminal_orders(ledger):
    """Explicitly requeue only broker-confirmed terminal remainders, never unknowns."""
    pending = ledger.get("execution")
    if not pending:
        raise SignalError("no saved execution to retry")
    staged = copy.deepcopy(ledger)
    execution = staged["execution"]
    for tracked in staged["open_orders"]:
        if tracked["status"] not in {"Cancelled", "Failed"}:
            raise SignalError("only confirmed cancelled/failed orders can be retried")
        key = tracked.get("client_order_id")
        if key not in execution["submitted"]:
            raise SignalError("terminal order has no saved plan identity")
        remainder = int(tracked["shares"]) - int(tracked.get("applied_shares", 0))
        for order in execution["orders"]:
            if order["client_order_id"] == key:
                order["shares"] = remainder
                order["broker_quantity"] = remainder // 1000 if order["order_lot"] == "Common" else remainder
                order["estimated_notional"] = remainder * order["limit_price"]
        execution["allocations"][key] = [
            {"strategy": leg["strategy"], "shares": int(leg["shares"]) - int(leg.get("applied", 0))}
            for leg in tracked["allocation"] if int(leg["shares"]) > int(leg.get("applied", 0))]
        execution["submitted"].remove(key)
        staged.setdefault("order_history", []).append(copy.deepcopy(tracked))
    staged["open_orders"] = []
    ledger.clear()
    ledger.update(staged)
