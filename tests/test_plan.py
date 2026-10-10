from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from order_desk.models import ExecutionPolicy, Quote
from order_desk.planner import build_order_plan
from order_desk.signal import SignalError, budgets_for, load_signal


NOW = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)


def _signal(path: Path, strategies: list[dict]) -> dict:
    payload = {
        "bundle_id": "11111111-1111-1111-1111-111111111111",
        "revision": 1,
        "effective_at": (NOW - timedelta(minutes=5)).isoformat(),
        "expires_at": (NOW + timedelta(hours=6)).isoformat(),
        "data_as_of": "2026-09-26",
        "strategies": strategies,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_signal(path, now=NOW)


def test_single_budget_applies_to_the_only_strategy(tmp_path: Path):
    signal = _signal(
        tmp_path / "book.json",
        [
            {
                "strategy_id": "superalpha-1",
                "weights": [{"ticker": "2330", "weight": 1}],
            }
        ],
    )
    assert budgets_for(signal, {"budget_twd": 1000000}) == {"superalpha-1": 1000000}


def test_several_strategies_need_separate_amounts(tmp_path: Path):
    signal = _signal(
        tmp_path / "book.json",
        [
            {"strategy_id": "a", "weights": [{"ticker": "2330", "weight": 1}]},
            {"strategy_id": "b", "weights": [{"ticker": "2317", "weight": 1}]},
        ],
    )
    with pytest.raises(SignalError, match="multiple strategies"):
        budgets_for(signal, {"budget_twd": 1000000})


def test_simulation_plan_uses_price_and_keeps_round_lots(tmp_path: Path):
    signal = _signal(
        tmp_path / "weekly-signal.json",
        [
            {
                "strategy_id": "superalpha-1",
                "weights": [{"ticker": "2330", "weight": 0.5}],
            }
        ],
    )
    quote = Quote("2330", bid=99, ask=100, last=100, limit_up=110, limit_down=90)
    plan = build_order_plan(
        signal,
        {"superalpha-1": 1_000_000},
        {"2330": quote},
        {},
        {},
        2_000_000,
        ExecutionPolicy(lot_mode="odd", max_order_twd=600_000),
        mode="simulation",
        now=NOW,
    )
    assert plan.orders[0].shares == 5000
    assert plan.orders[0].limit_price == 110
    assert plan.orders[0].order_lot == "Common"
    assert any("odd" in warning.lower() or "零股" in warning for warning in plan.warnings)


def test_buys_rest_at_limit_up_and_sells_rest_at_limit_down(tmp_path: Path):
    signal = _signal(
        tmp_path / "weekly-signal.json",
        [
            {
                "strategy_id": "superalpha-1",
                "weights": [
                    {"ticker": "2330", "weight": 0.5},
                    {"ticker": "2317", "weight": 0.5},
                ],
            }
        ],
    )
    quotes = {
        "2330": Quote("2330", bid=99, ask=100, last=100, limit_up=110, limit_down=90),
        "2317": Quote("2317", bid=49.5, ask=50, last=50, limit_up=55, limit_down=45),
    }
    plan = build_order_plan(
        signal,
        {"superalpha-1": 20_000},
        quotes,
        {"2317": 400},
        {"2317": 400},
        20_000,
        ExecutionPolicy(lot_mode="odd", max_order_twd=20_000),
        mode="production",
        now=NOW,
    )
    by_key = {(order.action, order.ticker): order for order in plan.orders}
    assert by_key[("Buy", "2330")].limit_price == 110
    assert by_key[("Sell", "2317")].limit_price == 45


def test_cash_must_cover_the_limit_up_reserve(tmp_path: Path):
    signal = _signal(
        tmp_path / "weekly-signal.json",
        [
            {
                "strategy_id": "superalpha-1",
                "weights": [{"ticker": "2330", "weight": 1}],
            }
        ],
    )
    quote = Quote("2330", bid=99, ask=100, last=100, limit_up=110, limit_down=90)
    with pytest.raises(Exception, match="available cash"):
        build_order_plan(
            signal,
            {"superalpha-1": 100_000},
            {"2330": quote},
            {},
            {},
            100_000,
            ExecutionPolicy(lot_mode="odd", max_order_twd=200_000, cash_check=True),
            mode="production",
            now=NOW,
        )


def test_cash_check_off_does_not_block_on_securities_balance(tmp_path: Path):
    signal = _signal(
        tmp_path / "weekly-signal.json",
        [
            {
                "strategy_id": "superalpha-1",
                "weights": [{"ticker": "2330", "weight": 1}],
            }
        ],
    )
    quote = Quote("2330", bid=99, ask=100, last=100, limit_up=110, limit_down=90)
    plan = build_order_plan(
        signal,
        {"superalpha-1": 100_000},
        {"2330": quote},
        {},
        {},
        1_000,
        ExecutionPolicy(lot_mode="odd", max_order_twd=200_000, cash_check=False),
        mode="production",
        now=NOW,
    )
    assert plan.orders[0].action == "Buy"
    assert plan.orders[0].limit_price == 110
