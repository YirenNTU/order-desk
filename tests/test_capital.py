from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from order_desk.books import book_internal, load_strategy_entries, plan_strategies
from order_desk.broker import BrokerWorker, FakeBroker
from order_desk.capital import load_account_ledger, save_account_ledger
from order_desk.execution import refresh_open_orders, submit_sells_then_buys
from order_desk.models import ExecutionPolicy, OrderIntent, Quote
from order_desk.planner import build_order_plan
from order_desk.signal import SignalError

NOW = datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc)


def _quote(ticker: str, price: float = 100) -> Quote:
    return Quote(ticker, bid=price, ask=price, last=price, limit_up=price * 1.1, limit_down=price * 0.9)


def _write_signal(path: Path, name: str, weights: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bundle_id": "11111111-1111-1111-1111-111111111111",
        "revision": 1,
        "effective_at": (NOW - timedelta(minutes=5)).isoformat(),
        "expires_at": (NOW + timedelta(hours=6)).isoformat(),
        "data_as_of": "2026-09-26",
        "strategies": [{"strategy_id": name, "weights": weights}],
    }
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")


def _intent(ticker: str, action: str, shares: int, price: float) -> OrderIntent:
    return OrderIntent(
        client_order_id=f"{ticker}-{action}-{shares}",
        ticker=ticker,
        action=action,  # type: ignore[arg-type]
        shares=shares,
        broker_quantity=shares,
        order_lot="IntradayOdd",
        limit_price=price,
        estimated_notional=shares * price,
    )


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy(lot_mode="odd", allow_sell_proceeds_for_buys=True, max_position_twd=50_000_000)


def test_cash_plus_stock_value_sizes_at_the_live_price(tmp_path: Path):
    _write_signal(
        tmp_path / "strategies" / "alpha" / "signal.json",
        "alpha",
        [{"ticker": "2330", "weight": 1, "close": 50}],
    )
    config = {
        "strategies": [
            {
                "name": "alpha",
                "signal_path": "strategies/alpha/signal.json",
                "extra_twd": 100_000,
                "cash_out_twd": 0,
            }
        ]
    }
    entries = load_strategy_entries(config, tmp_path, now=NOW)
    preview = plan_strategies(
        entries,
        {"strategies": {"alpha": {"2330": 1000}}, "open_orders": []},
        {"2330": _quote("2330", 100)},
        {"2330": 1000},
        150_000,
        _policy(),
        mode="simulation",
        now=NOW,
    )
    assert preview["targets"]["alpha"]["2330"] == 2000


def test_overlap_keeps_each_strategy_and_personal_shares(tmp_path: Path):
    root = tmp_path
    _write_signal(
        root / "strategies" / "alpha" / "signal.json",
        "alpha",
        [
            {"ticker": "2330", "weight": 0.5, "close": 100},
            {"ticker": "2317", "weight": 0.5, "close": 100},
        ],
    )
    _write_signal(
        root / "strategies" / "beta" / "signal.json",
        "beta",
        [{"ticker": "2330", "weight": 1, "close": 100}],
    )
    config = {
        "strategies": [
            {"name": "alpha", "signal_path": "strategies/alpha/signal.json", "extra_twd": 0, "cash_out_twd": 0},
            {"name": "beta", "signal_path": "strategies/beta/signal.json", "extra_twd": 0, "cash_out_twd": 0},
        ]
    }
    ledger = {"strategies": {"alpha": {"2330": 2000}, "beta": {"2330": 1000}}, "open_orders": []}
    preview = plan_strategies(
        load_strategy_entries(config, root, now=NOW),
        ledger,
        {"2330": _quote("2330"), "2317": _quote("2317")},
        {"2330": 3500, "9999": 500},
        200_000,
        _policy(),
        mode="simulation",
        now=NOW,
    )
    assert preview["targets"]["alpha"] == {"2330": 1000, "2317": 1000}
    assert preview["targets"]["beta"] == {"2330": 1000}
    assert [(order.action, order.ticker, order.shares) for order in preview["plan"].orders] == [
        ("Sell", "2330", 1000),
        ("Buy", "2317", 1000),
    ]
    assert preview["allocations"][preview["plan"].orders[0].client_order_id] == [
        {"strategy": "alpha", "shares": 1000, "applied": 0}
    ]
    assert preview["plan"].external_positions == {"2330": 500, "9999": 500}


def test_overlapping_targets_transfer_inside_the_ledger(tmp_path: Path):
    root = tmp_path
    _write_signal(
        root / "strategies" / "alpha" / "signal.json",
        "alpha",
        [
            {"ticker": "2330", "weight": 0.5, "close": 100},
            {"ticker": "2317", "weight": 0.5, "close": 100},
        ],
    )
    _write_signal(
        root / "strategies" / "beta" / "signal.json",
        "beta",
        [{"ticker": "2330", "weight": 1, "close": 100}],
    )
    config = {
        "strategies": [
            {"name": "alpha", "signal_path": "strategies/alpha/signal.json"},
            {"name": "beta", "signal_path": "strategies/beta/signal.json", "extra_twd": 100_000},
        ]
    }
    preview = plan_strategies(
        load_strategy_entries(config, root, now=NOW),
        {"strategies": {"alpha": {"2330": 2000}}, "open_orders": []},
        {"2330": _quote("2330"), "2317": _quote("2317")},
        {"2330": 2000},
        150_000,
        _policy(),
        mode="simulation",
        now=NOW,
    )
    assert preview["booked"] == {"alpha": {"2330": 1000}, "beta": {"2330": 1000}}
    assert [(order.action, order.ticker, order.shares) for order in preview["plan"].orders] == [
        ("Buy", "2317", 1000)
    ]
    assert book_internal({"alpha": {"2330": 2000}}, {"alpha": {"2330": 1000}, "beta": {"2330": 1000}}) == {
        "alpha": {"2330": 1000},
        "beta": {"2330": 1000},
    }


def test_cash_out_reduces_only_that_strategy(tmp_path: Path):
    root = tmp_path
    _write_signal(
        root / "strategies" / "alpha" / "signal.json",
        "alpha",
        [{"ticker": "2330", "weight": 1, "close": 100}],
    )
    config = {
        "strategies": [
            {
                "name": "alpha",
                "signal_path": "strategies/alpha/signal.json",
                "extra_twd": 0,
                "cash_out_twd": 40_000,
            }
        ]
    }
    preview = plan_strategies(
        load_strategy_entries(config, root, now=NOW),
        {"strategies": {"alpha": {"2330": 1000}}, "open_orders": []},
        {"2330": _quote("2330")},
        {"2330": 1000},
        0,
        _policy(),
        mode="production",
        now=NOW,
    )
    assert preview["targets"]["alpha"] == {"2330": 600}
    assert preview["plan"].orders[0].action == "Sell"
    assert preview["plan"].orders[0].shares == 400


def test_removed_strategy_is_not_silently_sold(tmp_path: Path):
    root = tmp_path
    _write_signal(
        root / "strategies" / "alpha" / "signal.json",
        "alpha",
        [{"ticker": "2330", "weight": 1, "close": 100}],
    )
    config = {"strategies": [{"name": "alpha", "signal_path": "strategies/alpha/signal.json"}]}
    with pytest.raises(SignalError, match="missing from config"):
        plan_strategies(
            load_strategy_entries(config, root, now=NOW),
            {"strategies": {"alpha": {"2330": 1000}, "old": {"2317": 1000}}, "open_orders": []},
            {"2330": _quote("2330"), "2317": _quote("2317")},
            {"2330": 1000, "2317": 1000},
            0,
            _policy(),
            mode="simulation",
            now=NOW,
        )


def test_live_price_sizes_the_shares_even_when_the_file_has_an_old_close():
    signal = {
        "bundle_id": "11111111-1111-1111-1111-111111111111",
        "revision": 1,
        "effective_at": (NOW - timedelta(minutes=5)).isoformat(),
        "expires_at": (NOW + timedelta(hours=6)).isoformat(),
        "data_as_of": "2026-09-26",
        "strategies": [
            {"strategy_id": "alpha", "weights": [{"ticker": "2330", "weight": 1, "close": 50}]}
        ],
    }
    plan = build_order_plan(
        signal,
        {"alpha": 100_000},
        {"2330": _quote("2330", 100)},
        {},
        {},
        150_000,
        ExecutionPolicy(lot_mode="odd"),
        mode="simulation",
        now=NOW,
    )
    assert plan.orders[0].shares == 1000


def test_send_attributes_fills_to_the_strategy():
    fake = FakeBroker(positions={"2330": 1000}, cash=0, auto_fill=True)
    worker = BrokerWorker(fake)
    ledger = {"strategies": {"alpha": {"2330": 1000}}, "open_orders": []}
    allocations = {
        "2330-Sell-400": [{"strategy": "alpha", "shares": 400, "applied": 0}],
        "2317-Buy-400": [{"strategy": "alpha", "shares": 400, "applied": 0}],
    }
    try:
        worker.call("connect")
        status = submit_sells_then_buys(
            worker,
            [_intent("2330", "Sell", 400, 100), _intent("2317", "Buy", 400, 100)],
            ledger,
            allocations,
            cash_reserve_twd=0,
            wait_seconds=0,
            poll_seconds=0,
        )
    finally:
        worker.close()
    assert status == "completed"
    assert [order["action"] for order in fake.orders.values()] == ["Sell", "Buy"]
    assert ledger["strategies"] == {"alpha": {"2330": 600, "2317": 400}}


def test_working_sell_blocks_the_buy():
    fake = FakeBroker(positions={"2330": 1000}, cash=1_000_000, auto_fill=False)
    worker = BrokerWorker(fake)
    ledger = {"strategies": {"alpha": {"2330": 1000}}, "open_orders": []}
    allocations = {
        "2330-Sell-400": [{"strategy": "alpha", "shares": 400, "applied": 0}],
        "2317-Buy-400": [{"strategy": "alpha", "shares": 400, "applied": 0}],
    }
    try:
        worker.call("connect")
        status = submit_sells_then_buys(
            worker,
            [_intent("2330", "Sell", 400, 100), _intent("2317", "Buy", 400, 100)],
            ledger,
            allocations,
            cash_reserve_twd=0,
            wait_seconds=0,
            poll_seconds=0,
        )
    finally:
        worker.close()
    assert status == "sells_working"
    assert [order["action"] for order in fake.orders.values()] == ["Sell"]
    assert ledger["strategies"] == {"alpha": {"2330": 1000}}


def test_buys_wait_when_sell_proceeds_do_not_cover_them():
    fake = FakeBroker(positions={"2330": 10}, cash=0, auto_fill=True)
    worker = BrokerWorker(fake)
    ledger = {"strategies": {"alpha": {"2330": 10}}, "open_orders": []}
    allocations = {
        "2330-Sell-10": [{"strategy": "alpha", "shares": 10, "applied": 0}],
        "2317-Buy-10": [{"strategy": "alpha", "shares": 10, "applied": 0}],
    }
    try:
        worker.call("connect")
        status = submit_sells_then_buys(
            worker,
            [_intent("2330", "Sell", 10, 100), _intent("2317", "Buy", 10, 150)],
            ledger,
            allocations,
            cash_reserve_twd=0,
            wait_seconds=0,
            poll_seconds=0,
            cash_check=True,
        )
    finally:
        worker.close()
    assert status == "buys_waiting_for_cash"
    assert [order["action"] for order in fake.orders.values()] == ["Sell"]
    assert ledger["strategies"] == {}


def test_cash_check_off_sends_buys_without_securities_cash():
    fake = FakeBroker(positions={"2330": 10}, cash=0, auto_fill=True)
    worker = BrokerWorker(fake)
    ledger = {"strategies": {"alpha": {"2330": 10}}, "open_orders": []}
    allocations = {
        "2330-Sell-10": [{"strategy": "alpha", "shares": 10, "applied": 0}],
        "2317-Buy-10": [{"strategy": "alpha", "shares": 10, "applied": 0}],
    }
    try:
        worker.call("connect")
        status = submit_sells_then_buys(
            worker,
            [_intent("2330", "Sell", 10, 100), _intent("2317", "Buy", 10, 150)],
            ledger,
            allocations,
            cash_reserve_twd=0,
            wait_seconds=0,
            poll_seconds=0,
            cash_check=False,
        )
    finally:
        worker.close()
    assert status == "completed"
    assert [order["action"] for order in fake.orders.values()] == ["Sell", "Buy"]


def test_each_strategy_has_its_own_ledger_file(tmp_path: Path):
    root = tmp_path
    order = {
        "broker_order_id": "abc",
        "ticker": "2317",
        "action": "Buy",
        "shares": 10,
        "order_lot": "IntradayOdd",
        "applied_shares": 0,
        "status": "Submitted",
        "allocation": [
            {"strategy": "alpha", "shares": 4, "applied": 0},
            {"strategy": "beta", "shares": 6, "applied": 0},
        ],
    }
    ledger = {
        "strategies": {"alpha": {"2330": 24}, "beta": {"2317": 6}},
        "open_orders": [order],
    }
    paths: dict[str, Path] = {}
    save_account_ledger(root, ledger, paths)
    assert json.loads((root / "strategies" / "alpha" / "ledger.json").read_text())["positions"] == {
        "2330": 24
    }
    assert json.loads((root / "strategies" / "beta" / "ledger.json").read_text())["positions"] == {
        "2317": 6
    }
    loaded, loaded_paths = load_account_ledger(root)
    assert loaded["strategies"] == ledger["strategies"]
    assert loaded["open_orders"] == [order]
    assert set(loaded_paths) == {"alpha", "beta"}


class _Rows:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def call(self, method: str):
        assert method == "reconcile_orders"
        return self.rows


def _open_buy(trading_day: str) -> dict:
    return {
        "broker_order_id": "abc",
        "ticker": "2330",
        "action": "Buy",
        "shares": 6,
        "order_lot": "IntradayOdd",
        "applied_shares": 0,
        "status": "Submitted",
        "trading_day": trading_day,
        "allocation": [{"strategy": "alpha", "shares": 6, "applied": 0}],
    }


def test_same_day_miss_is_not_sent_again():
    ledger = {"strategies": {"alpha": {"2330": 24}}, "open_orders": [_open_buy("2026-10-02")]}
    confirmed = refresh_open_orders(
        _Rows([{"broker_order_id": "abc", "status": "Cancelled", "filled_quantity": 0}]),
        ledger,
        today="2026-10-02",
    )
    assert confirmed
    assert ledger["open_orders"][0]["status"] == "Cancelled"
    assert ledger["strategies"] == {"alpha": {"2330": 24}}


def test_next_day_missed_buy_is_released_for_a_new_plan():
    ledger = {"strategies": {"alpha": {"2330": 24}}, "open_orders": [_open_buy("2026-10-02")]}
    confirmed = refresh_open_orders(_Rows([]), ledger, today="2026-10-05")
    assert confirmed
    assert ledger["open_orders"] == []
    assert ledger["strategies"] == {"alpha": {"2330": 24}}


def test_legacy_ledger_moves_into_strategy_folders(tmp_path: Path):
    root = tmp_path
    (root / "ledger.json").write_text(
        json.dumps({"strategies": {"alpha": {"2330": 24}}, "open_orders": []}),
        encoding="utf-8",
    )
    ledger, paths = load_account_ledger(root)
    save_account_ledger(root, ledger, paths)
    assert not (root / "ledger.json").exists()
    assert load_account_ledger(root)[0]["strategies"] == {"alpha": {"2330": 24}}
