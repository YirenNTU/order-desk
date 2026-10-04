"""Preview or send orders from local weekly strategy files."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from order_desk.calendar import CalendarUnavailable, TwseCalendar
from order_desk.books import (
    apply_book,
    load_strategy_entries,
    multi_strategy_mode,
    plan_strategies,
)
from order_desk.broker import BrokerError, BrokerWorker, ShioajiBroker
from order_desk.capital import load_account_ledger, save_account_ledger
from order_desk.execution import refresh_open_orders, submit_sells_then_buys
from order_desk.models import ExecutionPolicy, Quote
from order_desk.planner import PlanningError, build_order_plan
from order_desk.schedule import (
    due_actions,
    git_root,
    install_schedule,
    order_stamp_path,
    pull_is_current,
    pull_stamp_path,
    run_git_pull,
    schedule_slots,
    taipei_now,
    within_order_session,
)
from order_desk.signal import SignalError, budgets_for, load_signal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument(
        "--send",
        action="store_true",
        help="place the planned orders; default is preview only",
    )
    parser.add_argument("--confirm", default="", help="must be APPROVE when using --send")
    parser.add_argument(
        "--install-schedule",
        action="store_true",
        help="install this computer's Monday git pull and order jobs",
    )
    parser.add_argument("--weekly-pull", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--weekly-order", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--weekly-tick", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    root = config_path.parent
    if args.install_schedule:
        return _install_schedule(config_path, config)
    if args.weekly_tick:
        return _weekly_tick(config_path, config)
    if args.weekly_pull:
        return _weekly_pull(root)
    if args.weekly_order:
        refused = _refuse_weekly_order(root)
        if refused:
            return refused
        args.send = True
        args.confirm = "APPROVE"
    _load_env(root / ".env")
    _load_env(root.parent / "trading_client" / ".env")
    mode = str(config.get("mode", "simulation"))
    several = multi_strategy_mode(config)
    ledger, ledger_paths = (
        load_account_ledger(root) if several else ({"strategies": {}, "open_orders": []}, {})
    )
    if several:
        save_account_ledger(root, ledger, ledger_paths)
    worker = None
    try:
        policy = _policy(config, several)
        worker = _broker(mode)
        if several and ledger["open_orders"]:
            prior = list(ledger["open_orders"])
            confirmed = refresh_open_orders(worker, ledger)
            save_account_ledger(root, ledger, ledger_paths)
            for order in prior:
                if order.get("status") != "Expired":
                    continue
                remaining = int(order["shares"]) - int(order.get("applied_shares", 0))
                print(
                    f"{order['ticker']} {order['action']} {remaining} shares missed yesterday; "
                    "the next plan recalculates them from the live price"
                )
            if ledger["open_orders"] or not confirmed:
                _print_open_orders(ledger, confirmed)
                worker.close()
                return 0
        if several:
            preview = _plan_several(worker, config, root, policy, mode, ledger)
        else:
            preview = _plan_legacy(worker, config, root, policy, mode)
    except (OSError, KeyError, json.JSONDecodeError, SignalError, PlanningError, BrokerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if worker is not None:
            worker.close()
        return 1

    _print_preview(preview)
    if not args.send:
        print("preview only; nothing was sent")
        worker.close()
        return 0
    if args.confirm != "APPROVE":
        print("error: --send requires --confirm APPROVE", file=sys.stderr)
        worker.close()
        return 1
    try:
        if not several:
            for order in preview["plan"].orders:
                result = worker.call("place_order", order)
                print(
                    f"sent {order.ticker} {order.action} {order.shares} "
                    f"{order.order_lot} -> {result.status} {result.broker_order_id}"
                )
            return 0
        apply_book(ledger, preview["booked"])
        save_account_ledger(root, ledger, ledger_paths)
        status = submit_sells_then_buys(
            worker,
            preview["plan"].orders,
            ledger,
            preview["allocations"],
            cash_reserve_twd=policy.cash_reserve_twd,
            wait_seconds=float(config.get("sell_wait_seconds", 30)),
            poll_seconds=float(config.get("sell_poll_seconds", 2)),
        )
        save_account_ledger(root, ledger, ledger_paths)
        _print_submit_status(status, ledger)
        return 0 if status != "buys_waiting_for_cash" else 1
    except (SignalError, BrokerError) as exc:
        if several:
            save_account_ledger(root, ledger, ledger_paths)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        worker.close()


def _plan_several(worker, config, root, policy, mode, ledger):
    if mode == "dry_run":
        raise SignalError("dry_run has no market prices; set mode to simulation")
    entries = load_strategy_entries(config, root)
    tickers = sorted(
        {
            str(item["ticker"])
            for entry in entries
            for item in entry["strategy"]["weights"]
        }
        | {
            str(ticker)
            for positions in ledger["strategies"].values()
            for ticker in positions
        }
    )
    quotes: dict[str, Quote] = worker.call("quotes", tickers) if tickers else {}
    positions = {item.ticker: item.shares for item in worker.call("positions")}
    cash = float(worker.call("available_cash"))
    worker.call("settlements")
    return plan_strategies(
        entries,
        ledger,
        quotes,
        positions,
        cash,
        policy,
        mode=mode,
    )


def _plan_legacy(worker, config, root, policy, mode):
    if mode == "dry_run":
        raise SignalError("dry_run has no market prices; set mode to simulation")
    signal_path = Path(str(config["signal_path"])).expanduser()
    if not signal_path.is_absolute():
        signal_path = (root / signal_path).resolve()
    signal = load_signal(signal_path)
    budgets = budgets_for(signal, config)
    tickers = sorted(
        {
            str(weight["ticker"])
            for strategy in signal["strategies"]
            if float(budgets.get(strategy["strategy_id"], 0)) > 0
            for weight in strategy["weights"]
        }
    )
    quotes: dict[str, Quote] = worker.call("quotes", tickers) if tickers else {}
    positions = {item.ticker: item.shares for item in worker.call("positions")}
    cash = float(worker.call("available_cash"))
    worker.call("settlements")
    plan = build_order_plan(
        signal,
        budgets,
        quotes,
        positions,
        positions,
        cash,
        policy,
        mode=mode,
    )
    return {"plan": plan, "notes": [f"signal: {signal_path}"], "before": {}, "targets": {}, "paths": {}}


def _policy(config: dict, several: bool) -> ExecutionPolicy:
    return ExecutionPolicy(
        lot_mode=config.get("lot_mode", "odd"),
        limit_offset_bps=int(config.get("limit_offset_bps", 0)),
        cash_reserve_twd=float(config.get("cash_reserve_twd", 0)),
        max_order_twd=float(config.get("max_order_twd", 50_000_000)),
        max_daily_twd=float(config.get("max_daily_twd", 50_000_000)),
        max_position_twd=float(config.get("max_position_twd", 50_000_000)),
        max_turnover=float(config.get("max_turnover", 2)),
        allow_sell_proceeds_for_buys=several,
    )


def _broker(mode: str) -> BrokerWorker:
    broker = ShioajiBroker(mode)
    worker = BrokerWorker(broker)
    try:
        worker.call("connect")
    except Exception:
        worker.close()
        raise
    return worker


def _print_preview(preview: dict) -> None:
    plan = preview["plan"]
    for note in preview["notes"]:
        print(note)
    names = sorted(set(preview["before"]) | set(preview["targets"]))
    for name in names:
        print(name)
        tickers = sorted(
            set(preview["before"].get(name, {})) | set(preview["targets"].get(name, {}))
        )
        if not tickers:
            print("  (no shares)")
        for ticker in tickers:
            held = int(preview["before"].get(name, {}).get(ticker, 0))
            target = int(preview["targets"].get(name, {}).get(ticker, 0))
            print(f"  {ticker} held {held} -> {target}")
    print(f"data_as_of: {plan.data_as_of}  budget: {plan.total_budget_twd:,.0f} TWD")
    print(f"{'ticker':<8} {'side':<4} {'shares':>8} {'lot':<12} {'limit':>10} {'twd':>12}")
    if not plan.orders:
        print("(no orders)")
    for order in plan.orders:
        print(
            f"{order.ticker:<8} {order.action:<4} {order.shares:8d} "
            f"{order.order_lot:<12} {order.limit_price:10.2f} {order.estimated_notional:12.0f}"
        )
    if plan.external_positions:
        print("left untouched:")
        for ticker, shares in sorted(plan.external_positions.items()):
            print(f"  {ticker} {shares}")
    for warning in plan.warnings:
        print(f"warning: {warning}")


def _print_open_orders(ledger: dict, confirmed: bool) -> None:
    if not confirmed:
        print("a stored order could not be confirmed at the broker; nothing new was planned")
    elif all(str(order.get("status")) in {"Cancelled", "Failed"} for order in ledger["open_orders"]):
        print("unfilled orders stay until the next day; nothing new was planned")
    else:
        print("orders are still working; nothing new was planned")
    for order in ledger["open_orders"]:
        print(
            f"{order['ticker']} {order['action']} "
            f"filled {order.get('applied_shares', 0)}/{order['shares']} "
            f"status={order.get('status', '')}"
        )


def _print_submit_status(status: str, ledger: dict) -> None:
    messages = {
        "completed": "sells finished before buys; all orders are done",
        "sells_working": "sells are still working; buys were not sent",
        "buys_waiting_for_cash": "sells finished, but available cash does not cover the buys",
        "buys_working": "sells finished and buys were sent; some buys are still working",
    }
    print(messages.get(status, status))
    for name, positions in sorted(ledger["strategies"].items()):
        for ticker, shares in sorted(positions.items()):
            print(f"ledger {name} {ticker} {shares}")


def _install_schedule(config_path: Path, config: dict) -> int:
    try:
        written = install_schedule(
            config_path,
            config,
            python=sys.executable,
            agents_dir=Path.home() / "Library" / "LaunchAgents",
            domain=f"gui/{os.getuid()}",
        )
    except (OSError, SignalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"installed {path}")
    print("installed; Taipei time, on the week's first day the market is open")
    return 0


def _weekly_tick(config_path: Path, config: dict) -> int:
    root = config_path.parent
    now = taipei_now()
    today = now.date().isoformat()
    calendar = TwseCalendar(root / "logs" / "twse-holidays.json")
    try:
        actions = due_actions(
            now,
            schedule_slots(config),
            pull_done=pull_is_current(pull_stamp_path(root), today),
            order_done=pull_is_current(order_stamp_path(root), today),
            is_trading_day=calendar.is_trading_day,
        )
    except CalendarUnavailable as exc:
        _note_once(
            root / "logs" / "calendar_block.json",
            today,
            f"TWSE holiday calendar unavailable; nothing was run ({exc})",
        )
        return 1
    if not actions:
        return 0
    if "git_pull" in actions:
        _weekly_pull(root)
    if "order" not in actions:
        return 0
    if not pull_is_current(pull_stamp_path(root), today):
        print("today's git pull did not succeed; no orders were sent")
        return 1
    order_stamp_path(root).parent.mkdir(parents=True, exist_ok=True)
    order_stamp_path(root).write_text(
        json.dumps({"ok": True, "trading_day": today}) + "\n",
        encoding="utf-8",
    )
    return main(["--config", str(config_path), "--weekly-order"])


def _note_once(path: Path, today: str, message: str) -> None:
    if path.is_file():
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("trading_day") == today:
                return
        except (OSError, json.JSONDecodeError):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"trading_day": today}) + "\n", encoding="utf-8")
    print(message)


def _weekly_pull(root: Path) -> int:
    try:
        repo = git_root(root)
    except SignalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return run_git_pull(repo, pull_stamp_path(root), taipei_now().date().isoformat())


def _refuse_weekly_order(root: Path) -> int:
    today = taipei_now().date().isoformat()
    if not pull_is_current(pull_stamp_path(root), today):
        print("today's git pull did not succeed; no orders were sent")
        return 1
    if not within_order_session(datetime.now(ZoneInfo("Asia/Taipei"))):
        print("outside the odd-lot session; no orders were sent")
        return 1
    return 0


def _load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    raise SystemExit(main())
