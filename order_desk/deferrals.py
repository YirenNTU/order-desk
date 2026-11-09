"""Durable, cycle-scoped treatment of broker-confirmed restricted orders."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from order_desk.signal import SignalError


def cycle_id(now=None):
    day = (now or datetime.now(ZoneInfo('Asia/Taipei'))).astimezone(ZoneInfo('Asia/Taipei')).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def signal_signature(entries):
    rows = [{"name": e['name'], "strategy": e.get('strategy'),
             "data_as_of": (e.get('signal') or {}).get('data_as_of'),
             "revision": (e.get('signal') or {}).get('revision'),
             "adjust_positions": e.get('adjust_positions', True)} for e in entries]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def frozen_tickers(ledger, cycle):
    return {d['ticker'] for d in ledger.get('deferred_orders', []) if d['cycle_id'] == cycle} | set(ledger.get('inventory_quarantine', {}))


def restriction_reason(order):
    if order.get('status') != 'Failed':
        return ''
    detail = order.get('broker_snapshot', {}).get('status_detail', {})
    message = str(detail.get('message') or detail.get('value') or '')
    return message if any(word in message for word in ('預收', '圈存')) else ''


def defer_restricted_orders(ledger, cycle):
    """Only confirmed failures may be deferred; keep active/unknown orders blocking."""
    candidates = [(o, restriction_reason(o)) for o in ledger['open_orders']]
    candidates = [(o, reason) for o, reason in candidates if reason]
    if not candidates:
        return False
    execution = ledger.get('execution', {})
    if not execution.get('budgets') or not execution.get('targets') or not execution.get('signal_signature'):
        raise SignalError('saved cycle budgets/targets missing; recover the original plan before reweighting')
    if execution.get('cycle_id') != cycle:
        raise SignalError('unfinished prior-cycle execution needs reconciliation before deferral')
    staged = copy.deepcopy(ledger)
    ids = {o['broker_order_id'] for o, _ in candidates}
    existing = {d['broker_order_id'] for d in staged.get('deferred_orders', [])}
    for order in staged['open_orders']:
        if order['broker_order_id'] not in ids:
            continue
        if order.get('client_order_id') not in execution.get('submitted', []):
            raise SignalError('failed order does not belong to the saved execution')
        if order['broker_order_id'] not in existing:
            staged.setdefault('deferred_orders', []).append({
                'broker_order_id': order['broker_order_id'], 'ticker': order['ticker'],
                'action': order['action'], 'unfilled_shares': order['shares'] - order.get('applied_shares', 0),
                'allocation': copy.deepcopy(order['allocation']),
                'reason': restriction_reason(order), 'cycle_id': cycle,
                'retry_from': (datetime.fromisoformat(cycle).date() + timedelta(days=7)).isoformat(),
                'retry_checks': [],
            })
        order['disposition'] = 'deferred_reweight'
        staged.setdefault('order_history', []).append(copy.deepcopy(order))
    staged['open_orders'] = [o for o in staged['open_orders'] if o['broker_order_id'] not in ids]
    staged['execution']['reweight_required'] = True
    ledger.clear()
    ledger.update(staged)
    return True


def record_retry_checks(ledger, preview):
    current = preview['cycle_id']
    for record in ledger.get('deferred_orders', []):
        if record['cycle_id'] >= current or any(r['cycle_id'] == current for r in record['retry_checks']):
            continue
        orders = [o for o in preview['plan'].orders if o.ticker == record['ticker']]
        record['retry_checks'].append({
            'cycle_id': current,
            'outcome': 'planned' if orders else 'no_trade_required_by_latest_target',
            'orders': [{'client_order_id': o.client_order_id, 'action': o.action, 'shares': o.shares} for o in orders],
        })
