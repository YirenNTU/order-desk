"""Separate manual sessions from scheduled plans while sharing account state."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from zoneinfo import ZoneInfo
import uuid
from datetime import datetime, timezone
from order_desk.capital import TERMINAL_ORDER_STATUSES
from order_desk.signal import SignalError


def config_signature(config):
    # Schedule timing is independent; order-affecting settings must be reviewed.
    value = {k:v for k,v in config.items() if k != 'schedule'}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def origin(ledger):
    # Existing plans have no reliable source label. Never silently adopt one.
    return ledger.get('execution', {}).get('origin', 'legacy')


def manual_hold(ledger):
    return bool(ledger.get('manual_session')) or (bool(ledger.get('execution')) and origin(ledger) == 'manual')


def begin_manual(ledger, *, new_request=False):
    session = ledger.get('manual_session')
    replace_active = (new_request and session and session.get('state') == 'ready'
                      and ledger.get('execution') and origin(ledger) == 'manual')
    if not session or session.get('state') == 'completed' or replace_active:
        if session:
            ledger.setdefault('manual_session_history', []).append(copy.deepcopy(session))
        session = {'id':uuid.uuid4().hex, 'state':'waiting_for_reconciliation',
                   'created_at':datetime.now(timezone.utc).isoformat(), 'carry_budgets':{}}
        ledger['manual_session'] = session
    return session


def retire_for_manual(ledger, *, scope=None):
    """Archive resolved old work. The caller must freshly reconcile and audit."""
    orders = ledger.get('open_orders', [])
    retained = [o for o in orders if o.get('status') not in TERMINAL_ORDER_STATUSES]
    if retained and scope is None:
        raise SignalError('old orders are still working or unconfirmed; manual hold remains, no new orders')
    for order in retained:
        _validate_retained_order(order, scope, ledger)
    pending = ledger.get('execution')
    if pending:
        submitted = pending.get('submitted', [])
        records = ledger.get('order_history', []) + orders
        for key in submitted:
            matches = [o for o in records if o.get('client_order_id') == key]
            if len(matches) != 1 or (matches[0].get('status') not in TERMINAL_ORDER_STATUSES and matches[0] not in retained):
                raise SignalError('submitted old order has no unique final record; cannot retire plan')
        if not pending.get('budgets'):
            raise SignalError('old plan capital missing; cannot discard sale proceeds')
    # Validate everything before altering the execution or its final records.
    session = ledger['manual_session']
    if pending:
        session['carry_budgets'].update(copy.deepcopy(pending['budgets']))
        archived = copy.deepcopy(pending)
        archived.update(outcome='superseded_by_manual', superseded_by=session['id'],
                        retired_at=datetime.now(timezone.utc).isoformat())
        ledger.setdefault('execution_history', []).append(archived)
        del ledger['execution']
    ledger.setdefault('order_history', []).extend(copy.deepcopy([o for o in orders if o not in retained]))
    if retained:
        session['retained_scope'] = copy.deepcopy(scope)
    for order in retained:
        order['retained_for_manual'] = session['id']
    ledger['open_orders'] = retained


def release_manual(ledger):
    if ledger.get('execution') or ledger.get('open_orders'):
        raise SignalError('unfinished plan/orders remain; schedule cannot adopt manual work')
    session = ledger.get('manual_session')
    if session and session.get('state') != 'completed':
        raise SignalError('manual plan not completed; finish or explicitly review it before resuming schedule')
    if session:
        archived = ledger.pop('manual_session')
        archived['released_at'] = datetime.now(timezone.utc).isoformat()
        ledger.setdefault('manual_session_history', []).append(archived)



def retention_scope(entries, ledger):
    active = {e['name'] for e in entries if e.get('adjust_positions', True)}
    tickers = {str(w['ticker']) for e in entries if e['name'] in active
               for w in (e.get('strategy') or {}).get('weights', [])}
    for name in active:
        tickers.update(ledger.get('strategies', {}).get(name, {}))
    return {'paused_strategies':sorted(e['name'] for e in entries if e['name'] not in active),
            'active_tickers':sorted(tickers)}


def _validate_retained_order(order, scope, ledger):
    from order_desk.validation import nonnegative_integer
    from order_desk.capital import filled_shares, quantity_to_shares
    if scope is None or order.get('status') not in {'Submitted', 'PartFilled', 'Filling'}:
        raise SignalError('retained order is working with an unverified status; reconcile before new orders')
    today = datetime.now(ZoneInfo('Asia/Taipei')).date().isoformat()
    if order.get('trading_day') != today:
        raise SignalError('retained prior-day order needs its final result')
    if order.get('action') not in {'Buy','Sell'} or order.get('order_lot') not in {'Common','IntradayOdd'}:
        raise SignalError('retained order type is unsupported')
    if order.get('ticker') in scope['active_tickers']:
        raise SignalError('retained stock overlaps an active strategy; resolve that old order first')
    saved = order.get('broker_snapshot', {})
    if any(saved.get(k) != order.get(k) for k in ['broker_order_id','ticker','action','order_lot','status']):
        raise SignalError('retained order lacks matching broker identity/status')
    day = str(saved.get('trading_day') or saved.get('order_datetime') or today)[:10]
    if day != today:
        raise SignalError('retained broker order has a different date')
    qty = nonnegative_integer(order['shares']);applied = nonnegative_integer(order.get('applied_shares',0))
    if not qty or applied >= qty or filled_shares(saved,order['order_lot']) != applied:
        raise SignalError('retained order fill/remaining quantity is inconsistent')
    broker_qty = (quantity_to_shares(saved['quantity'],order['order_lot']) if 'quantity' in saved
                  else nonnegative_integer(saved.get('shares')))
    if broker_qty != qty:
        raise SignalError('retained order quantity was modified; reconcile first')
    price = saved.get('limit_price')
    if isinstance(price,bool) or not isinstance(price,(int,float)) or not math.isfinite(price) or price <= 0:
        raise SignalError('retained order has no verifiable price bound')
    legs = order.get('allocation', [])
    if (not legs or sum(nonnegative_integer(a['shares']) for a in legs) != qty
            or sum(nonnegative_integer(a.get('applied',0)) for a in legs) != applied):
        raise SignalError('retained strategy allocation is incomplete')
    owners = set()
    for a in legs:
        if a['strategy'] not in scope['paused_strategies'] or a['strategy'] in owners:
            raise SignalError('retained order must belong only to paused strategies')
        owners.add(a['strategy'])
        remaining = nonnegative_integer(a['shares'])-nonnegative_integer(a.get('applied',0))
        if remaining < 0:
            raise SignalError('retained allocation fill exceeds shares')
        if order['action']=='Sell' and remaining > ledger.get('strategies',{}).get(a['strategy'],{}).get(order['ticker'],0):
            raise SignalError('retained sell exceeds the original strategy holdings')


def validate_retained_orders(ledger, *, scope=None, candidate_tickers=(), candidate_strategies=()):
    session = ledger.get('manual_session', {})
    scope = scope if scope is not None else session.get('retained_scope')
    reserved_sells = {}
    for order in ledger.get('open_orders', []):
        if not order.get('retained_for_manual'):
            continue
        if order['retained_for_manual'] != session.get('id'):
            raise SignalError('retained order belongs to a different manual session')
        _validate_retained_order(order, scope, ledger)
        if order['action'] == 'Sell':
            for a in order['allocation']:
                key = (a['strategy'],order['ticker'])
                reserved_sells[key] = reserved_sells.get(key,0) + a['shares'] - a.get('applied',0)
                if reserved_sells[key] > ledger.get('strategies',{}).get(key[0],{}).get(key[1],0):
                    raise SignalError('combined retained sells exceed strategy holdings')
        if (order['ticker'] in candidate_tickers
                or any(a['strategy'] in candidate_strategies for a in order['allocation'])):
            raise SignalError('new order overlaps retained stock or strategy')


def blocking_orders(ledger):
    validate_retained_orders(ledger)
    return [o for o in ledger.get('open_orders', []) if not o.get('retained_for_manual')]
