"""Pure target-sizing, sleeve netting, and fail-closed order planning."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Mapping

from order_desk.models import (
    ExecutionPolicy,
    OrderIntent,
    OrderPlan,
    Quote,
)


class PlanningError(ValueError):
    """Raised when no safe order plan can be produced."""


def stock_tick(price: float) -> Decimal:
    value = Decimal(str(price))
    if value < 10:
        return Decimal("0.01")
    if value < 50:
        return Decimal("0.05")
    if value < 100:
        return Decimal("0.1")
    if value < 500:
        return Decimal("0.5")
    if value < 1000:
        return Decimal("1")
    return Decimal("5")


def legal_limit_price(raw: float, quote: Quote, action: str) -> float:
    if not math.isfinite(raw) or raw <= 0:
        raise PlanningError(f"{quote.ticker}: invalid raw order price")
    bounded = min(max(raw, quote.limit_down), quote.limit_up)
    tick = stock_tick(bounded)
    units = Decimal(str(bounded)) / tick
    rounding = ROUND_CEILING if action == "Buy" else ROUND_FLOOR
    result = units.to_integral_value(rounding=rounding) * tick
    result = min(max(result, Decimal(str(quote.limit_down))), Decimal(str(quote.limit_up)))
    return float(result)


def _price_for_size(quote: Quote) -> float:
    for value in (quote.last, (quote.bid + quote.ask) / 2):
        if math.isfinite(value) and value > 0:
            return value
    raise PlanningError(f"{quote.ticker}: no positive sizing price")


def _validate_quote(quote: Quote) -> None:
    values = (quote.bid, quote.ask, quote.last, quote.limit_up, quote.limit_down)
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise PlanningError(f"{quote.ticker}: incomplete quote")
    if quote.bid > quote.ask or quote.limit_down >= quote.limit_up:
        raise PlanningError(f"{quote.ticker}: inconsistent quote")
    if not quote.limit_down <= quote.last <= quote.limit_up:
        raise PlanningError(f"{quote.ticker}: last price outside daily limits")


def _client_order_id(
    bundle_id: str, revision: int, ticker: str, action: str, lot: str
) -> str:
    raw = f"{bundle_id}:{revision}:{ticker}:{action}:{lot}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _orders_for_delta(
    bundle_id: str,
    revision: int,
    ticker: str,
    delta_shares: int,
    quote: Quote,
    policy: ExecutionPolicy,
    *,
    odd_lots_enabled: bool,
) -> list[OrderIntent]:
    if delta_shares == 0:
        return []
    action = "Buy" if delta_shares > 0 else "Sell"
    shares = abs(delta_shares)
    raw_limit = quote.limit_up if action == "Buy" else quote.limit_down
    limit_price = legal_limit_price(raw_limit, quote, action)
    common_shares = shares // 1000 * 1000
    odd_shares = shares % 1000 if odd_lots_enabled else 0
    if not odd_lots_enabled and shares % 1000:
        raise PlanningError(f"{ticker}: non-round-lot delta in common-lot mode")
    intents: list[OrderIntent] = []
    for index, offset in enumerate(range(0, common_shares, 499_000)):
        chunk = min(499_000, common_shares - offset)
        lot_key = 'Common' if common_shares <= 499_000 else f'Common:{index}'
        intents.append(OrderIntent(
            client_order_id=_client_order_id(bundle_id, revision, ticker, action, lot_key),
            ticker=ticker, action=action, shares=chunk, broker_quantity=chunk // 1000,
            order_lot='Common', limit_price=limit_price,
            estimated_notional=round(chunk * limit_price, 2)))
    if odd_shares:
        intents.append(
            OrderIntent(
                client_order_id=_client_order_id(
                    bundle_id, revision, ticker, action, "IntradayOdd"
                ),
                ticker=ticker,
                action=action,
                shares=odd_shares,
                broker_quantity=odd_shares,
                order_lot="IntradayOdd",
                limit_price=limit_price,
                estimated_notional=round(odd_shares * limit_price, 2),
            )
        )
    return intents


def build_order_plan(
    bundle: Mapping[str, Any],
    budgets: Mapping[str, float],
    quotes: Mapping[str, Quote],
    managed_positions: Mapping[str, int],
    broker_positions: Mapping[str, int],
    available_cash: float,
    policy: ExecutionPolicy,
    *,
    mode: str = "dry_run",
    now: datetime | None = None,
    frozen_tickers: set[str] | None = None,
    inventory_quarantine: set[str] | None = None,
    sleeve_current: Mapping[str, Mapping[str, int]] | None = None,
    fixed_targets: Mapping[str, Mapping[str, int]] | None = None,
) -> OrderPlan:
    if mode not in {"dry_run", "simulation", "production"}:
        raise PlanningError("invalid execution mode")
    if isinstance(policy.max_turnover, bool) or not math.isfinite(policy.max_turnover) or policy.max_turnover < 0:
        raise PlanningError('max_turnover must be finite and nonnegative')
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    effective = datetime.fromisoformat(str(bundle["effective_at"]).replace("Z", "+00:00"))
    expires = datetime.fromisoformat(str(bundle["expires_at"]).replace("Z", "+00:00"))
    if current < effective.astimezone(timezone.utc) or current >= expires.astimezone(timezone.utc):
        raise PlanningError("signal bundle is outside its execution window")
    if available_cash < 0:
        raise PlanningError("available cash cannot be negative")

    required_tickers = {
        str(weight["ticker"])
        for strategy in bundle["strategies"]
        for weight in strategy["weights"]
        if float(budgets.get(strategy["strategy_id"], 0)) > 0
    }
    missing_quotes = sorted(required_tickers - set(quotes))
    if missing_quotes:
        raise PlanningError(f"missing quotes: {', '.join(missing_quotes)}")
    for ticker in required_tickers:
        _validate_quote(quotes[ticker])

    odd_lots_enabled = policy.lot_mode == "odd" and mode == "production"
    warnings: list[str] = []
    if policy.lot_mode == "odd" and mode != "production":
        warnings.append("Simulation/dry-run uses common lots because SinoPac simulation rejects odd lots.")

    quarantined = inventory_quarantine or set()
    frozen = (frozen_tickers or set()) | quarantined
    sleeve_current = sleeve_current or {}
    fixed_targets = fixed_targets or {}
    sleeve_targets: dict[str, dict[str, int]] = {}
    total_budget = 0.0
    for strategy in bundle["strategies"]:
        strategy_id = str(strategy["strategy_id"])
        budget = float(budgets.get(strategy_id, 0.0))
        if not math.isfinite(budget) or budget < 0:
            raise PlanningError(f"{strategy_id}: budget is invalid")
        total_budget += budget
        if strategy_id in fixed_targets:
            sleeve_targets[strategy_id] = dict(fixed_targets[strategy_id])
            continue
        held = sleeve_current.get(strategy_id, {})
        locked = {t: int(q) for t, q in held.items() if t in frozen and int(q)}
        for ticker in locked:
            if ticker not in quotes:
                raise PlanningError(f"{ticker}: quote required to value frozen holdings")
            _validate_quote(quotes[ticker])
        locked_value = sum(q * _price_for_size(quotes[t]) for t, q in locked.items())
        if locked_value > budget + 1e-6:
            raise PlanningError(f"{strategy_id}: frozen holdings exceed cycle budget; withdrawal/reweight needs review")
        tradable_budget = max(0.0, budget - locked_value)
        weights = [item for item in strategy['weights'] if str(item['ticker']) not in frozen]
        affected = bool(locked) or len(weights) != len(strategy['weights'])
        denominator = sum(float(item['weight']) for item in weights) if affected else 1.0
        if not math.isfinite(denominator) or denominator < 0:
            raise PlanningError(f"{strategy_id}: invalid remaining weights")
        sleeve = dict(locked)
        if affected:
            warnings.append(f"{strategy_id}: budget {budget:,.2f} - frozen holdings {locked_value:,.2f} = tradable {tradable_budget:,.2f}; remaining weights normalized")
        if budget == 0:
            warnings.append(f"{strategy_id}: target value is zero, so managed shares are sold.")
        if denominator == 0:
            warnings.append(f"{strategy_id}: no tradable positive weights; {tradable_budget:,.2f} remains unallocated")
        for item in weights if denominator else []:
            ticker = str(item['ticker'])
            weight = float(item['weight'])
            if not math.isfinite(weight) or weight < 0:
                raise PlanningError(f"{strategy_id}: invalid weight")
            if tradable_budget == 0 or weight == 0:
                continue
            shares = int((tradable_budget * weight / denominator) // _price_for_size(quotes[ticker]))
            if not odd_lots_enabled:
                shares = shares // 1000 * 1000
            if shares > 0:
                sleeve[ticker] = shares
        sleeve_targets[strategy_id] = sleeve
    if total_budget <= 0 and not any(int(shares) for shares in managed_positions.values()):
        raise PlanningError("all strategy budgets are zero")

    targets: dict[str, int] = {}
    for sleeve in sleeve_targets.values():
        for ticker, shares in sleeve.items():
            targets[ticker] = targets.get(ticker, 0) + shares

    managed = {str(key): int(value) for key, value in managed_positions.items() if int(value)}
    broker = {str(key): int(value) for key, value in broker_positions.items() if int(value)}
    external: dict[str, int] = {}
    for ticker in set(managed) | set(broker):
        if managed.get(ticker, 0) > broker.get(ticker, 0) and ticker not in quarantined:
            raise PlanningError(
                f"{ticker}: managed ledger exceeds broker position; reconcile manual trading first"
            )
        difference = broker.get(ticker, 0) - managed.get(ticker, 0)
        if difference > 0:
            external[ticker] = difference
    if external:
        warnings.append("External account holdings are excluded from sellable managed inventory.")

    orders: list[OrderIntent] = []
    for ticker in sorted(set(targets) | set(managed)):
        if ticker in quarantined:
            if targets.get(ticker, 0) != managed.get(ticker, 0):
                raise PlanningError(f"{ticker}: quarantined inventory must remain unchanged")
            warnings.append(f"{ticker}: inventory discrepancy quarantined; no orders for this symbol")
            continue
        if ticker not in quotes:
            raise PlanningError(f"{ticker}: quote required for an existing managed position")
        _validate_quote(quotes[ticker])
        target_value = targets.get(ticker, 0) * _price_for_size(quotes[ticker])
        if target_value > policy.max_position_twd + 1e-6:
            raise PlanningError(f"{ticker}: target exceeds max_position_twd")
        delta = targets.get(ticker, 0) - managed.get(ticker, 0)
        orders.extend(
            _orders_for_delta(
                str(bundle["bundle_id"]),
                int(bundle["revision"]),
                ticker,
                delta,
                quotes[ticker],
                policy,
                odd_lots_enabled=odd_lots_enabled,
            )
        )

    for order in orders:
        if order.estimated_notional > policy.max_order_twd + 1e-6:
            raise PlanningError(f"{order.ticker}: order exceeds max_order_twd")
    buy_twd = sum(o.estimated_notional for o in orders if o.action == "Buy")
    sell_twd = sum(o.estimated_notional for o in orders if o.action == "Sell")
    gross = buy_twd + sell_twd
    if gross > policy.max_daily_twd + 1e-6:
        raise PlanningError("gross orders exceed max_daily_twd")
    # A withdrawal shrinks the target budget but still requires selling the
    # original holdings. Measure turnover against the larger portfolio side,
    # excluding all external and paused-strategy positions from the basis.
    current_managed_value = 0.0
    for ticker, shares in managed.items():
        if ticker not in quotes:
            raise PlanningError(f'{ticker}: quote required for turnover valuation')
        _validate_quote(quotes[ticker])
        current_managed_value += shares * _price_for_size(quotes[ticker])
    turnover_basis = max(current_managed_value, total_budget)
    if not math.isfinite(turnover_basis) or turnover_basis < 0:
        raise PlanningError('invalid turnover valuation')
    turnover_ratio = gross / turnover_basis if turnover_basis else 0.0
    if (gross > 0 and turnover_basis == 0) or turnover_ratio > policy.max_turnover + 1e-9:
        raise PlanningError(
            f'planned turnover exceeds max_turnover: gross={gross:,.2f}, '
            f'basis={turnover_basis:,.2f} (managed_before={current_managed_value:,.2f}, '
            f'target_budget={total_budget:,.2f}), ratio={turnover_ratio:.4f}, limit={policy.max_turnover:g}'
        )
    buying_power = max(0.0, available_cash - policy.cash_reserve_twd)
    if policy.allow_sell_proceeds_for_buys:
        buying_power += sell_twd
    if policy.cash_check and buy_twd > buying_power + 1e-6:
        raise PlanningError("planned buys exceed conservative available cash")

    ordered = tuple(
        sorted(orders, key=lambda item: (0 if item.action == "Sell" else 1, item.ticker, item.order_lot))
    )
    return OrderPlan(
        bundle_id=str(bundle["bundle_id"]),
        revision=int(bundle["revision"]),
        mode=mode,  # type: ignore[arg-type]
        data_as_of=str(bundle["data_as_of"]),
        targets=targets,
        sleeve_targets=sleeve_targets,
        managed_before=managed,
        external_positions=external,
        orders=ordered,
        estimated_buy_twd=round(buy_twd, 2),
        estimated_sell_twd=round(sell_twd, 2),
        total_budget_twd=round(total_budget, 2),
        cash_reserve_twd=round(policy.cash_reserve_twd, 2),
        warnings=tuple(warnings),
        turnover_basis_twd=round(turnover_basis, 2),
        turnover_ratio=turnover_ratio,
        max_turnover=policy.max_turnover,
    )
