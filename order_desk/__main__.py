"""Preview or send orders from local weekly strategy files."""

from __future__ import annotations

import argparse
import json
import os
import sys
import sqlite3
import hashlib
import copy
import subprocess
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from order_desk.audit import audit_with_history, record_audit, require_clear_audit
from order_desk.cash import check_cash, money
from order_desk.calendar import CalendarUnavailable, TwseCalendar
from order_desk.books import (
    apply_book,
    load_strategy_entries,
    multi_strategy_mode,
    plan_strategies,
)
from order_desk.broker import BrokerError, BrokerWorker, ShioajiBroker
from order_desk.capital import account_lock, atomic_json, TERMINAL_ORDER_STATUSES, load_account_ledger, save_account_ledger
from order_desk.inventory import ReplanRequired, sync_quarantine, reconcile_actual_inventory
from order_desk.deferrals import cycle_id, defer_restricted_orders, frozen_tickers, record_retry_checks
from order_desk.execution import cycle_already_completed, refresh_open_orders, prepare_next_day_execution, retry_terminal_orders, submit_sells_then_buys
from order_desk.models import ExecutionPolicy, Quote
from order_desk.planner import PlanningError, build_order_plan
from order_desk.schedule import (
    due_actions,
    git_root,
    install_schedule,
    order_stamp_blocks_retry,
    order_stamp_path,
    pull_is_current,
    pull_stamp_path,
    run_git_pull,
    schedule_enabled,
    schedule_slots,
    taipei_now,
    uninstall_schedule,
    week_id_for,
    within_order_session,
    write_order_stamp,
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
    parser.add_argument("--retry-terminal", action="store_true", help="explicitly retry confirmed failed/cancelled remainders of the saved plan")
    parser.add_argument("--reconcile-only", action="store_true", help="record broker results without placing any orders")
    parser.add_argument("--resume-schedule", action="store_true", help="enable this account schedule and check remaining orders now; requires --confirm APPROVE")
    parser.add_argument("--audit", action="store_true", help="read-only broker/ledger double check; never sends or changes holdings")
    parser.add_argument("--confirm", default="", help="must be APPROVE when using --send")
    parser.add_argument(
        "--install-schedule",
        action="store_true",
        help="install this computer's weekly git pull and order checker",
    )
    parser.add_argument(
        "--uninstall-schedule",
        action="store_true",
        help="remove this computer's weekly LaunchAgent",
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
    if args.resume_schedule:
        return _resume_schedule(config_path, config, args.confirm)
    if args.install_schedule:
        return _install_schedule(config_path, config)
    if args.uninstall_schedule:
        return _uninstall_schedule(config_path)
    if args.weekly_tick:
        return _weekly_tick(config_path, config)
    if args.weekly_pull:
        return _weekly_pull(root)
    if args.weekly_order:
        refused = _refuse_weekly_order(root, config)
        if refused:
            return refused
        args.send = True
        args.confirm = "APPROVE"
    try:
        with account_lock(root):
            if args.weekly_order:
                saved, _ = load_account_ledger(root)
                if cycle_already_completed(saved, cycle_id()):
                    print('this cycle is already completed in the account ledger; no new orders')
                    return 0
            return _run_account(args, config, root)
    except (OSError, sqlite3.Error, SignalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _run_account(args, config, root):
    for _ in range(3):
        result = _run_account_once(args, config, root)
        if result != 3:
            return result
    print('inventory kept changing; progress saved, retry on the next invocation', file=sys.stderr)
    return 1


def _run_account_once(args, config, root):
    _load_env(root / ".env")
    _load_env(root.parent / "trading_client" / ".env")
    restricted_policy = config.get('restricted_order_policy', 'freeze_reweight')
    if restricted_policy not in {'stop', 'freeze_reweight'}:
        raise SignalError('restricted_order_policy must be stop or freeze_reweight')
    mode = str(config.get("mode", "simulation"))
    several = multi_strategy_mode(config)
    ledger, ledger_paths = (
        load_account_ledger(root) if several else ({"strategies": {}, "open_orders": []}, {})
    )
    if not several and args.send:
        raise SignalError("sending requires per-strategy ledgers; migrate legacy config first")
    binding = {"mode": mode, "account": hashlib.sha256(os.getenv("SJ_ACCOUNT_ID", "").encode()).hexdigest()}
    if ledger.get("account_binding") and ledger["account_binding"] != binding:
        raise SignalError("ledger belongs to a different account or mode")
    ledger["account_binding"] = binding
    inventory_policy = config.get('inventory_mismatch_policy', 'reconcile_actual')
    if inventory_policy not in {'stop', 'quarantine', 'reconcile_actual'}:
        raise SignalError('inventory_mismatch_policy must be stop, quarantine or reconcile_actual')
    ledger['inventory_mismatch_policy'] = inventory_policy
    worker = None
    try:
        policy = _policy(config, several)
        worker = _broker(mode)
        if args.audit:
            report = audit_with_history(worker, ledger, root)
            record_audit(root, report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            worker.close()
            return 1 if report["blockers"] else 0
        if args.reconcile_only:
            if not several:
                raise SignalError('reconciliation requires per-strategy ledgers')
            confirmed = refresh_open_orders(worker, ledger)
            save_account_ledger(root, ledger, ledger_paths)
            report = audit_with_history(worker, ledger, root)
            record_audit(root, report)
            terminal = all(o.get('status') in TERMINAL_ORDER_STATUSES for o in ledger['open_orders'])
            print('reconciliation only; no orders sent')
            worker.close()
            return 0 if confirmed and terminal and not report['blockers'] else 1
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
            if args.retry_terminal:
                if not args.send or args.confirm != "APPROVE":
                    raise SignalError("--retry-terminal requires --send --confirm APPROVE")
                retry_terminal_orders(ledger)
                save_account_ledger(root, ledger, ledger_paths)
            if restricted_policy == 'freeze_reweight' and not args.retry_terminal:
                if defer_restricted_orders(ledger, cycle_id()):
                    save_account_ledger(root, ledger, ledger_paths)
                    print('restricted orders recorded for next rebalance; recalculating tradable capital')
            if ledger["open_orders"] or not confirmed:
                _print_open_orders(ledger, confirmed)
                worker.close()
                return 0
        if several and prepare_next_day_execution(ledger):
            save_account_ledger(root, ledger, ledger_paths)
        if several and inventory_policy in {'quarantine', 'reconcile_actual'}:
            _checked_audit(worker, ledger, root, replan_on_change=False)
            save_account_ledger(root, ledger, ledger_paths)
        if several and ledger.get("execution") and not ledger['execution'].get('reweight_required'):
            if not args.send:
                print("unfinished execution retained; use --send --confirm APPROVE to resume its original orders")
                worker.close()
                return 0
            if args.confirm != "APPROVE":
                raise SignalError("--send requires --confirm APPROVE")
            _checked_audit(worker, ledger, root, tickers=[o['ticker'] for o in ledger['execution']['orders']])
            status = submit_sells_then_buys(
                worker, [], ledger, {}, cash_reserve_twd=policy.cash_reserve_twd,
                wait_seconds=float(config.get("sell_wait_seconds", 30)),
                poll_seconds=float(config.get("sell_poll_seconds", 2)), cash_check=policy.cash_check,
                cash_fee_rate=policy.cash_fee_rate, cash_min_fee_twd=policy.cash_min_fee_twd,
                persist=lambda: save_account_ledger(root, ledger, ledger_paths),
                preflight=lambda: _preflight(worker, ledger, root))
            if restricted_policy == 'freeze_reweight' and defer_restricted_orders(ledger, cycle_id()):
                save_account_ledger(root, ledger, ledger_paths)
                status = 'reweight_required'
            _print_submit_status(status, ledger)
            worker.close()
            return 0 if status in {'completed', 'completed_with_deferrals', 'reweight_required'} else 1
        if several:
            preview = _plan_several(worker, config, root, policy, mode, ledger)
            if inventory_policy in {'quarantine', 'reconcile_actual'}:
                save_account_ledger(root, ledger, ledger_paths)
        else:
            preview = _plan_legacy(worker, config, root, policy, mode)
    except ReplanRequired as exc:
        save_account_ledger(root, ledger, ledger_paths)
        if worker is not None:
            worker.close()
        print(str(exc))
        return 3
    except (OSError, KeyError, json.JSONDecodeError, SignalError, PlanningError, BrokerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if several and not args.audit:
            save_account_ledger(root, ledger, ledger_paths)
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
        report = _checked_audit(worker, ledger, root, tickers=[o.ticker for o in preview['plan'].orders],
                                expected_quarantine=set(preview.get('inventory_quarantine', [])))
        if {r["ticker"]: r["broker_shares"] for r in report["positions"] if r["broker_shares"]} != preview["broker_positions"]:
            raise ReplanRequired('broker positions changed after planning; rebuilding the remaining plan')
        print("account double-check passed; details: logs/account_audit.json")
        if ledger.get('execution'):
            ledger.setdefault('execution_history', []).append(copy.deepcopy(ledger['execution']))
        record_retry_checks(ledger, preview)
        ledger["execution"] = {
            "trading_day": taipei_now().date().isoformat(),
            "cycle_id": preview['cycle_id'], 'budgets': preview['budgets'],
            'targets': preview['targets'], 'signal_signature': preview['signal_signature'],
            "orders": [asdict(o) for o in preview["plan"].orders],
            "allocations": preview["allocations"], "submitted": []}
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
            cash_check=policy.cash_check,
            cash_fee_rate=policy.cash_fee_rate, cash_min_fee_twd=policy.cash_min_fee_twd,
            persist=lambda: save_account_ledger(root, ledger, ledger_paths),
            preflight=lambda: _preflight(worker, ledger, root),
        )
        if restricted_policy == 'freeze_reweight' and defer_restricted_orders(ledger, cycle_id()):
            status = 'reweight_required'
        save_account_ledger(root, ledger, ledger_paths)
        _print_submit_status(status, ledger)
        return 0 if status != "buys_waiting_for_cash" else 1
    except ReplanRequired as exc:
        save_account_ledger(root, ledger, ledger_paths)
        print(str(exc))
        return 3
    except (SignalError, BrokerError) as exc:
        if several:
            save_account_ledger(root, ledger, ledger_paths)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        worker.close()


def _checked_audit(worker, ledger, root, *, tickers=(), replan_on_change=True, expected_quarantine=None):
    report = audit_with_history(worker, ledger, root, tickers=tickers)
    record_audit(root, report)
    isolated = set()
    changed = False
    if ledger.get('inventory_mismatch_policy') in {'quarantine', 'reconcile_actual'}:
        # Never mask unknown orders or broken broker observations with a holdings adjustment.
        require_clear_audit(report, excluded_tickers=set(report.get('inventory_issues', {})))
        if ledger.get('inventory_mismatch_policy') == 'reconcile_actual' and report.get('inventory_issues'):
            quote_tickers = sorted(report['inventory_issues'])
            quotes = worker.call('quotes', quote_tickers)
            changed = reconcile_actual_inventory(ledger, report, quotes)
            if changed:
                report = audit_with_history(worker, ledger, root, tickers=tickers)
                record_audit(root, report)
        changed = sync_quarantine(ledger, report) or changed
        isolated = set(ledger.get('inventory_quarantine', {}))
    require_clear_audit(report, excluded_tickers=isolated)
    if (changed and replan_on_change) or (expected_quarantine is not None and expected_quarantine != isolated):
        raise ReplanRequired('account inventory changed; rebuilding remaining orders from reconciled capital')
    return report


def _preflight(worker, ledger, root):
    tickers = [o['ticker'] for o in ledger.get('execution', {}).get('orders', [])]
    _checked_audit(worker, ledger, root, tickers=tickers)


def _plan_several(worker, config, root, policy, mode, ledger):
    if mode == "dry_run":
        raise SignalError("dry_run has no market prices; set mode to simulation")
    entries = load_strategy_entries(config, root)
    tickers = sorted(
        {
            str(item["ticker"])
            for entry in entries
            if entry.get("adjust_positions", True) and entry.get("strategy")
            for item in entry["strategy"]["weights"]
        }
        | {
            str(ticker)
            for positions in ledger["strategies"].values()
            for ticker in positions
        }
    )
    quotes: dict[str, Quote] = worker.call("quotes", tickers) if tickers else {}
    if ledger.get('inventory_mismatch_policy') in {'quarantine', 'reconcile_actual'}:
        report = _checked_audit(worker, ledger, root, tickers=tickers, replan_on_change=False)
        positions = {r['ticker']: r['broker_shares'] for r in report['positions']}
    else:
        positions = {}
        for item in worker.call('positions'):
            positions[item.ticker] = positions.get(item.ticker, 0) + item.shares
    # Cash shortage must not prevent the sell leg. Check buying funds separately
    # for preview and again immediately before every new buy.
    preview = plan_strategies(entries, ledger, quotes, positions, 0.0,
                              replace(policy, cash_check=False), mode=mode)
    preview['broker_positions'] = {t: q for t, q in positions.items() if q}
    if policy.cash_check and any(o.action == 'Buy' for o in preview['plan'].orders):
        preview['cash_safety'] = check_cash(worker, ledger, preview['plan'].orders,
            reserve=policy.cash_reserve_twd, fee_rate=policy.cash_fee_rate,
            minimum_fee=policy.cash_min_fee_twd)

    return preview


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
    positions = {}
    for item in worker.call("positions"):
        positions[item.ticker] = positions.get(item.ticker, 0) + item.shares
    cash = float(worker.call("available_cash")) if policy.cash_check else 0.0
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
        cash_reserve_twd=money(config.get("cash_reserve_twd", 0), "cash reserve"),
        max_order_twd=float(config.get("max_order_twd", 50_000_000)),
        max_daily_twd=float(config.get("max_daily_twd", 50_000_000)),
        max_position_twd=float(config.get("max_position_twd", 50_000_000)),
        max_turnover=float(config.get("max_turnover", 2)),
        allow_sell_proceeds_for_buys=several,
        cash_check=config.get('mode') == 'production' or bool(config.get('cash_check', True)),
        cash_fee_rate=money(config.get('cash_fee_rate', 0.001425), 'cash fee rate'),
        cash_min_fee_twd=money(config.get('cash_min_fee_twd', 20), 'cash minimum fee'),
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
    cash = preview.get('cash_safety')
    if cash:
        print(f"cash safety: {cash['status']}; bank_balance={cash.get('bank_balance_twd', 'unverified')}, settlement_buying_power={cash.get('available_twd', 'unverified')}, confirmed_receivables={cash.get('confirmed_receivable_credit_twd', 'unverified')}, required={cash.get('required_twd', 'unverified')}")
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
    if status == 'buys_waiting_for_cash':
        report = ledger.get('cash_safety', {})
        print(f"cash safety: {report.get('status', 'unavailable')}; bank_balance={report.get('bank_balance_twd', 'unverified')}, settlement_buying_power={report.get('available_twd', 'unverified')}, confirmed_receivables={report.get('confirmed_receivable_credit_twd', 'unverified')}, required={report.get('required_twd', 'unverified')}, shortfall={report.get('shortfall_twd', 'unverified')}")
        print(report.get('reason', 'funds unavailable'))
    messages = {
        "completed": "sells finished before buys; all orders are done",
        "completed_with_deferrals": "tradable orders completed; restricted holdings retained and recorded for next rebalance",
        "reweight_required": "restricted orders deferred; next invocation reweights remaining capital before further orders",
        "sells_working": "sells are still working; buys were not sent",
        "buys_waiting_for_cash": "cash safety details saved in ledger cash_safety; sells finished, but available cash does not cover the buys",
        "buys_working": "sells finished and buys were sent; some buys are still working",
    }
    print(messages.get(status, status))
    for name, positions in sorted(ledger["strategies"].items()):
        for ticker, shares in sorted(positions.items()):
            print(f"ledger {name} {ticker} {shares}")


def _resume_schedule(config_path: Path, config: dict, confirm: str) -> int:
    if confirm != 'APPROVE':
        print('error: --resume-schedule requires --confirm APPROVE', file=sys.stderr)
        return 1
    try:
        with account_lock(config_path.parent):
            # Install while paused; a failed install must not enable an existing job.
            paused = copy.deepcopy(config)
            paused.setdefault('schedule', {})['enabled'] = False
            atomic_json(config_path, paused)
            if _install_schedule(config_path, paused) != 0:
                return 1
            config.setdefault('schedule', {})['enabled'] = True
            atomic_json(config_path, config)
    except (OSError, SignalError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    print('schedule enabled; checking this session and saved progress')
    return _weekly_tick(config_path, config)


def _fresh_process(config_path: Path, flag: str) -> int:
    return subprocess.run([sys.executable, '-m', 'order_desk', '--config', str(config_path), flag],
                          cwd=config_path.parent, check=False).returncode


def _closing_reconciliation(config_path: Path, config: dict, now) -> int:
    root = config_path.parent
    if not schedule_enabled(config):
        return 0
    ledger, _ = load_account_ledger(root)
    if not ledger.get('open_orders') and not ledger.get('execution'):
        return 0
    stamp = root / 'logs' / 'last_close_check.json'
    if stamp.exists():
        try:
            prior = json.loads(stamp.read_text())
            if prior.get('day') == now.date().isoformat():
                if prior.get('complete') or now.timestamp() - float(prior.get('at', 0)) < 300:
                    return 0
        except (ValueError, OSError):
            pass
    code = _fresh_process(config_path, '--reconcile-only')
    atomic_json(stamp, {'day': now.date().isoformat(), 'at': now.timestamp(), 'complete': code == 0})
    return code


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
    print("installed; Taipei time, first open day through Friday catch-up")
    print("change schedule times in config.json without reinstalling; set schedule.enabled false to pause")
    return 0


def _uninstall_schedule(config_path: Path) -> int:
    try:
        removed = uninstall_schedule(
            config_path,
            agents_dir=Path.home() / "Library" / "LaunchAgents",
            domain=f"gui/{os.getuid()}",
        )
    except (OSError, SignalError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for label in removed:
        print(f"removed {label}")
    return 0


def _weekly_tick(config_path: Path, config: dict) -> int:
    root = config_path.parent
    now = taipei_now()
    today = now.date()
    today_text = today.isoformat()
    calendar = TwseCalendar(root / "logs" / "twse-holidays.json")
    try:
        if not schedule_enabled(config):
            return 0
        if now.hour * 60 + now.minute >= 13 * 60 + 40 and calendar.is_trading_day(today):
            return _closing_reconciliation(config_path, config, now)
        actions = due_actions(
            now,
            schedule_slots(config),
            pull_done=pull_is_current(pull_stamp_path(root), today_text),
            order_done=order_stamp_blocks_retry(order_stamp_path(root), today),
            is_trading_day=calendar.is_trading_day,
            enabled=schedule_enabled(config),
        )
    except CalendarUnavailable as exc:
        _note_once(
            root / "logs" / "calendar_block.json",
            today_text,
            f"TWSE holiday calendar unavailable; nothing was run ({exc})",
        )
        return 1
    if not actions:
        return 0
    if "git_pull" in actions:
        _weekly_pull(root)
    if "order" not in actions:
        return 0
    if not pull_is_current(pull_stamp_path(root), today_text):
        print("today's git pull did not succeed; no orders were sent")
        return 1
    code = _fresh_process(config_path, "--weekly-order")
    status = _weekly_order_status(root, code)
    write_order_stamp(
        order_stamp_path(root),
        week=week_id_for(today),
        trading_day=today_text,
        status=status,
    )
    print(f"weekly order status: {status}")
    return 0 if status != "failed" else max(code, 1)


def _weekly_order_status(root: Path, code: int) -> str:
    try:
        ledger, _ = load_account_ledger(root)
    except SignalError:
        return "failed"
    if ledger.get("open_orders") or ledger.get("execution"):
        return "working"
    if code != 0:
        return "failed"
    return "completed_with_deferrals" if frozen_tickers(ledger, cycle_id()) else "completed"


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


def _refuse_weekly_order(root: Path, config: dict | None = None) -> int:
    now = taipei_now()
    today = now.date().isoformat()
    config = config or {}
    if not schedule_enabled(config):
        print('schedule is paused; no orders were sent')
        return 1
    try:
        if not TwseCalendar(root / 'logs' / 'twse-holidays.json').is_trading_day(now.date()):
            print('market is closed; no orders were sent')
            return 1
    except CalendarUnavailable:
        return 1
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
