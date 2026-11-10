"""Several strategies, one account, separate share records."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from order_desk.control import frozen_working_stocks, retained_buy_reservations
from order_desk.capital import combined_shares, target_value
from order_desk.deferrals import cycle_id, frozen_tickers, signal_signature
from order_desk.models import ExecutionPolicy, OrderPlan, Quote
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
        adjust_positions = _adjust_positions(row)
        if not adjust_positions:
            entries.append(
                {
                    "name": name,
                    "path": signal_path,
                    "signal": None,
                    "strategy": None,
                    "extra_twd": 0,
                    "cash_out_twd": 0,
                    "adjust_positions": False,
                }
            )
            continue
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
                "adjust_positions": True,
            }
        )
    return entries


def _adjust_positions(row: Mapping[str, Any]) -> bool:
    if "adjust_positions" not in row:
        return True
    return bool(row.get("adjust_positions"))


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
    for ticker, shares in combined_shares(ledger["strategies"]).items():
        if shares > broker_positions.get(ticker, 0) and ticker not in ledger.get('inventory_quarantine', {}):
            raise SignalError(f"{ticker}: managed ledger {shares} exceeds broker position {broker_positions.get(ticker, 0)}; reconcile fills/manual trading first")
    current_cycle = cycle_id(now)
    frozen = frozen_tickers(ledger, current_cycle) | frozen_working_stocks(ledger)
    pending = ledger.get('execution', {})
    reweight = bool(pending.get('reweight_required'))
    same_cycle = pending.get('cycle_id') == current_cycle
    if reweight and same_cycle and pending.get('signal_signature') != signal_signature(entries):
        raise SignalError('signal changed since saved execution; review remaining plan before reweighting')
    notes: list[str] = []
    if reweight and not same_cycle:
        notes.append('new rebalance cycle: old freeze released; latest weights use the unfinished execution capital cap, without reapplying cash inputs')
    reservations = retained_buy_reservations(ledger, policy.cash_fee_rate, policy.cash_min_fee_twd)
    budgets: dict[str, float] = {}
    active: list[dict[str, Any]] = []
    paused_held: dict[str, dict[str, int]] = {}
    for entry in entries:
        name = entry["name"]
        held = {
            str(ticker): int(shares)
            for ticker, shares in ledger["strategies"].get(name, {}).items()
        }
        if not entry.get("adjust_positions", True):
            notes.append(f"{name}: adjust_positions off, holdings left unchanged")
            if held:
                paused_held[name] = held
            continue
        if reweight:
            if name not in pending.get('budgets', {}):
                raise SignalError(f"{name}: original cycle budget missing")
            value = float(pending['budgets'][name])
            note = f"{name}: retained original cycle budget {value:,.2f}; sold proceeds are not lost and cash inputs are not applied twice"
        elif name in ledger.get('manual_session', {}).get('carry_budgets', {}):
            from order_desk.capital import _nonnegative
            base = _nonnegative(name, 'retained capital', ledger['manual_session']['carry_budgets'][name])
            extra = _nonnegative(name, 'extra_twd', entry['extra_twd'])
            withdrawal = _nonnegative(name, 'cash_out_twd', entry['cash_out_twd'])
            value = base + extra - withdrawal
            if value < 0:
                raise SignalError(f'{name}: withdrawal exceeds retained capital plus new funds')
            note = f'{name}: manual plan retained capital {base:,.2f} + new extra {extra:,.2f} - new withdrawal {withdrawal:,.2f} = {value:,.2f}'
        else:
            value, note = target_value(
                name, held, quotes, entry["extra_twd"], entry["cash_out_twd"]
            )
            if reservations.get(name):
                value += reservations[name]
                note += f"; plus retained uninvested buy capital {reservations[name]:,.2f}, reserved below"
        notes.append(note)
        if value <= 0 and not held:
            notes.append(f"{name}: skipped, no holdings and no extra money")
            continue
        budgets[name] = value
        active.append(entry)
    if not active:
        if not paused_held and not any(
            ledger["strategies"].get(entry["name"]) for entry in entries
        ):
            raise SignalError("no strategy has holdings or extra money")
        result = _paused_only_preview(entries, ledger, notes, paused_held, mode=mode)
        result.update(budgets={}, cycle_id=current_cycle, signal_signature=signal_signature(entries), inventory_quarantine=sorted(ledger.get('inventory_quarantine', {})))
        return result

    planning_budgets = dict(budgets)
    for name, budget in budgets.items():
        reserve = reservations.get(name, 0.0)
        if reserve:
            if reserve > budget + 1e-6:
                raise SignalError(f'{name}: retained buy reservation {reserve:,.2f} exceeds post-withdrawal budget {budget:,.2f}; reduce withdrawal or resolve the retained order')
            planning_budgets[name] = max(0.0,budget-reserve)
            notes.append(f'{name}: post-withdrawal budget {budget:,.2f} - retained buy reservation {reserve:,.2f} = allocation budget {planning_budgets[name]:,.2f}; existing frozen holdings are deducted next')
    if frozen_working_stocks(ledger):
        notes.append('working stocks frozen for this manual session: '+', '.join(sorted(frozen_working_stocks(ledger))))
    current = now or datetime.now(timezone.utc)
    bundle = _merge_bundle(active, current)
    if reweight or frozen:
        context = {'source': bundle['bundle_id'], 'cycle': current_cycle,
                   'frozen': sorted(frozen), 'budgets': budgets, 'current': ledger['strategies']}
        bundle['bundle_id'] = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()[:32]
    managed = combined_shares(
        {entry["name"]: ledger["strategies"].get(entry["name"], {}) for entry in active}
    )
    try:
        plan = build_order_plan(
            bundle,
            planning_budgets,
            quotes,
            managed,
            broker_positions,
            available_cash,
            policy,
            mode=mode,
            now=current,
            frozen_tickers=frozen,
            inventory_quarantine=set(ledger.get('inventory_quarantine', {})),
            sleeve_current=ledger['strategies'],
            fixed_targets={
                entry['name']: pending['targets'][entry['name']]
                for entry in active
                if reweight and same_cycle
                and not reservations.get(entry['name'])
                and entry['name'] not in pending.get('reallocate_strategies', [])
                and not (set(ledger['strategies'].get(entry['name'], {})) & frozen)
                and not any(str(w['ticker']) in frozen for w in entry['strategy']['weights'])
            },
        )
    except PlanningError as exc:
        raise SignalError(str(exc)) from exc
    before = {entry["name"]: dict(ledger["strategies"].get(entry["name"], {})) for entry in active}
    targets = {name: dict(shares) for name, shares in plan.sleeve_targets.items()}
    booked = book_internal(before, targets)
    residuals = signed_diff(booked, targets)
    allocations = tag_orders(plan.orders, residuals)
    for name, positions in paused_held.items():
        booked[name] = dict(positions)
        before[name] = dict(positions)
        targets[name] = dict(positions)
    return {
        "plan": plan,
        "budgets": budgets,
        "retained_buy_reservations": reservations,
        "inventory_quarantine": sorted(ledger.get('inventory_quarantine', {})),
        "cycle_id": current_cycle,
        "signal_signature": signal_signature(entries),
        "notes": notes,
        "before": before,
        "targets": targets,
        "booked": booked,
        "allocations": allocations,
        "paths": {entry["name"]: entry["path"] for entry in entries},
    }


def _paused_only_preview(
    entries: list[dict[str, Any]],
    ledger: Mapping[str, Any],
    notes: list[str],
    paused_held: Mapping[str, Mapping[str, int]],
    *,
    mode: str,
) -> dict[str, Any]:
    held = {name: dict(positions) for name, positions in paused_held.items()}
    plan = OrderPlan(
        bundle_id="paused",
        revision=0,
        mode=mode,  # type: ignore[arg-type]
        data_as_of="",
        targets={},
        sleeve_targets={name: dict(positions) for name, positions in held.items()},
        managed_before=combined_shares(held) if held else {},
        external_positions={},
        orders=(),
        estimated_buy_twd=0.0,
        estimated_sell_twd=0.0,
        total_budget_twd=0.0,
        cash_reserve_twd=0.0,
        warnings=("all configured strategies have adjust_positions off",),
    )
    return {
        "plan": plan,
        "notes": notes,
        "before": held,
        "targets": held,
        "booked": held,
        "allocations": {},
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
