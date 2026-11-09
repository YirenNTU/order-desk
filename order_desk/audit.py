"""Reconcile strategy inventory separately from manual/external holdings."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict
from order_desk.capital import combined_shares, atomic_json, quantity_to_shares
from order_desk.signal import SignalError


def _stamp(row, observed_at):
    item = dict(row)
    if not item.get('observed_trading_day'):
        value = str(item.get('order_datetime') or item.get('trading_day') or '')
        item['observed_trading_day'] = value[:10] if value else datetime.fromisoformat(observed_at.replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Taipei')).date().isoformat()
    return item


def _key(row):
    return json.dumps([row.get('observed_trading_day'), row.get('broker_order_id'), row.get('ticker'), row.get('action'), row.get('order_lot')])


def _fills(row):
    from order_desk.capital import filled_shares
    return filled_shares(row, row.get('order_lot', 'IntradayOdd'))


def _baseline(report):
    return {'checked_at': report['checked_at'],
            'external_shares': {r['ticker']: r['unallocated_shares'] for r in report['positions']},
            'orders': [_stamp(r, report['checked_at']) for r in report.get('orders', [])]}


def load_audit_context(root):
    path = root / 'logs' / 'account_audit.sqlite3'
    if not path.exists():
        return None, []
    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'inventory_baseline' in tables:
            row = conn.execute('SELECT payload FROM inventory_baseline WHERE id=1').fetchone()
            baseline = json.loads(row[0]) if row else None
        else:
            baseline = None
        if 'observed_orders' in tables and baseline is not None:
            return baseline, [json.loads(r[0]) for r in conn.execute('SELECT payload FROM observed_orders')]
        # Migrate older audit history without reinterpreting its holdings as new trades.
        observed = {}
        if 'audits' in tables:
            for payload, in conn.execute('SELECT payload FROM audits ORDER BY id'):
                report = json.loads(payload)
                if baseline is None and report.get('positions') and not report.get('blockers'):
                    baseline = report.get('inventory_baseline') or _baseline(report)
                for item in report.get('orders', []):
                    row = _stamp(item, report['checked_at'])
                    observed[_key(row)] = row
        return baseline, list(observed.values())


def audit_with_history(worker, ledger, root, *, tickers=()):
    baseline, observed = load_audit_context(root)
    return audit_account(worker, ledger, tickers=tickers, baseline=baseline, observed_orders=observed)


def audit_account(worker, ledger, *, tickers=(), baseline=None, observed_orders=()):
    checked_at = datetime.now(timezone.utc).isoformat()
    actual = defaultdict(int)
    for row in worker.call('positions'):
        actual[row.ticker] += row.shares
    managed = combined_shares(ledger['strategies'])
    relevant = set(tickers) | set(managed) | set(ledger.get('inventory_quarantine', {})) | {o['ticker'] for o in ledger.get('execution', {}).get('orders', [])}
    tracked = ledger.get('open_orders', []) + ledger.get('order_history', [])
    known = defaultdict(list)
    for row in tracked:
        known[str(row['broker_order_id'])].append(row)
    observations = {_key(r): dict(r) for r in observed_orders}
    live_keys = set()
    observation_errors = []
    current_day = datetime.fromisoformat(checked_at).astimezone(ZoneInfo('Asia/Taipei')).date().isoformat()
    for item in worker.call('reconcile_orders'):
        row = _stamp(item, checked_at)
        key = _key(row)
        if key in live_keys:
            observation_errors.append(f"{row.get('ticker')}: duplicate broker order observation")
            continue
        previous = observations.get(key)
        if previous is not None and _fills(row) < _fills(previous):
            observation_errors.append(f"{row.get('ticker')}: cumulative fill moved backward since last audit")
        else:
            observations[key] = row
        live_keys.add(key)
    base_orders = {_key(r): r for r in (baseline or {}).get('orders', [])}
    external = dict((baseline or {}).get('external_shares', {}))
    if baseline is not None:
        for ticker, adjustment in ledger.get('external_inventory_adjustments', {}).items():
            external[ticker] = external.get(ticker, 0) + int(adjustment)
    orders, blockers = [], observation_errors
    reserved = defaultdict(int)
    for key, row in observations.items():
        ticker = str(row.get('ticker', ''))
        matches = [o for o in known.get(str(row.get('broker_order_id', '')), [])
                   if o['ticker'] == ticker
                   and all(field not in row or field not in o or str(row[field]).split('.')[-1] == str(o[field]).split('.')[-1] for field in ('action', 'order_lot'))
                   and (not o.get('trading_day') or o['trading_day'] == row['observed_trading_day'])]
        item = dict(row)
        item['live_observation'] = key in live_keys
        item['source'] = 'program' if matches else 'external'
        status = str(row.get('status', '')).split('.')[-1]
        if not matches:
            if row.get('custom_field'):
                item['source'] = 'unattributed_program_tag'
                if ticker in relevant:
                    blockers.append(f'{ticker}: unrecognized program-tagged order; reconcile missing program record')
            if baseline is not None:
                if key not in base_orders and row['observed_trading_day'] < datetime.fromisoformat(baseline['checked_at'].replace('Z', '+00:00')).astimezone(ZoneInfo('Asia/Taipei')).date().isoformat():
                    if ticker in relevant:
                        blockers.append(f'{ticker}: newly discovered pre-baseline trade needs reconciliation')
                else:
                    delta = _fills(row) - _fills(base_orders.get(key, {}))
                    if delta < 0:
                        blockers.append(f'{ticker}: external cumulative fill moved backward')
                    elif delta:
                        action = str(row.get('action', '')).split('.')[-1]
                        if action not in {'Buy', 'Sell'}:
                            blockers.append(f'{ticker}: manual fill direction unavailable')
                        else:
                            external[ticker] = external.get(ticker, 0) + (delta if action == 'Buy' else -delta)
            if (key in live_keys or row['observed_trading_day'] == current_day) and ticker in relevant and status not in {'Filled', 'Cancelled', 'Failed'}:
                action = str(row.get('action', '')).split('.')[-1]
                if action == 'Sell' and 'quantity' in row:
                    total = quantity_to_shares(row['quantity'], row.get('order_lot'))
                    reserved[ticker] += max(0, total - _fills(row))
                elif action != 'Buy':
                    blockers.append(f'{ticker}: active external sell quantity/direction cannot be verified')
        orders.append(item)
    comparisons = []
    inventory_issues = {}
    def inventory_issue(ticker, message):
        blockers.append(message)
        inventory_issues.setdefault(ticker, []).append(message)
    for ticker in sorted(set(managed) | set(actual) | set(external)):
        inferred = actual[ticker] - managed.get(ticker, 0)
        expected_external = external.get(ticker, 0) if baseline is not None else inferred
        strategy_at_broker = actual[ticker] - expected_external
        comparisons.append({'ticker': ticker, 'broker_shares': actual[ticker],
                            'program_shares': managed.get(ticker, 0), 'unallocated_shares': inferred,
                            'external_shares': expected_external, 'strategy_shares_after_external': strategy_at_broker,
                            'difference_shares': strategy_at_broker - managed.get(ticker, 0)})
        if inferred < 0:
            inventory_issue(ticker, f'{ticker}: program ledger {managed.get(ticker, 0)} exceeds broker shares {actual[ticker]}')
        if ticker in relevant:
            if expected_external < 0:
                inventory_issue(ticker, f'{ticker}: manual sells exceed external holdings; strategy inventory may have been used')
            if strategy_at_broker != managed.get(ticker, 0):
                inventory_issue(ticker, f'{ticker}: broker minus external = {strategy_at_broker}, strategy ledger = {managed.get(ticker, 0)}; reconcile unexplained inventory change')
            if reserved[ticker] > max(0, expected_external):
                inventory_issue(ticker, f'{ticker}: pending manual sells exceed external holdings')
    report = {'checked_at': checked_at, 'positions': comparisons, 'orders': orders, 'blockers': blockers,
              'deferred_orders': ledger.get('deferred_orders', []), 'inventory_issues': inventory_issues,
              'note': 'Normal manual trades update external inventory only. Compare broker minus external with strategy shares; do not rewrite strategy holdings to force agreement.'}
    report['inventory_baseline'] = baseline or (_baseline(report) if not blockers else None)
    return report


def require_clear_audit(report, *, excluded_tickers=()):
    isolated = {message for ticker in excluded_tickers
                for message in report.get('inventory_issues', {}).get(ticker, [])}
    remaining = [message for message in report['blockers'] if message not in isolated]
    if remaining:
        raise SignalError('account double-check failed: ' + '; '.join(remaining))


def record_audit(root, report):
    path = root / 'logs' / 'account_audit.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('CREATE TABLE IF NOT EXISTS audits (id INTEGER PRIMARY KEY, checked_at TEXT NOT NULL, payload TEXT NOT NULL)')
        conn.execute('INSERT INTO audits(checked_at, payload) VALUES (?, ?)', (report['checked_at'], payload))
        conn.execute('CREATE TABLE IF NOT EXISTS inventory_baseline (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        if report.get('inventory_baseline'):
            conn.execute('INSERT OR IGNORE INTO inventory_baseline(id,payload) VALUES(1,?)', (json.dumps(report['inventory_baseline'],ensure_ascii=False),))
        conn.execute('CREATE TABLE IF NOT EXISTS observed_orders (identity TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        for item in report.get('orders', []):
            row = _stamp(item, report['checked_at'])
            conn.execute('INSERT OR REPLACE INTO observed_orders(identity,payload) VALUES (?,?)', (_key(row),json.dumps(row,ensure_ascii=False)))
    atomic_json(root / 'logs' / 'account_audit.json', report)
