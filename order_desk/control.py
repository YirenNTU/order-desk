"""Separate manual sessions from scheduled plans while sharing account state."""
from __future__ import annotations
import copy
import hashlib
import json
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


def retire_for_manual(ledger):
    """Archive resolved old work. The caller must freshly reconcile and audit."""
    orders = ledger.get('open_orders', [])
    if any(o.get('status') not in TERMINAL_ORDER_STATUSES for o in orders):
        raise SignalError('old orders are still working or unconfirmed; manual hold remains, no new orders')
    pending = ledger.get('execution')
    if pending:
        submitted = pending.get('submitted', [])
        records = ledger.get('order_history', []) + orders
        for key in submitted:
            matches = [o for o in records if o.get('client_order_id') == key]
            if len(matches) != 1 or matches[0].get('status') not in TERMINAL_ORDER_STATUSES:
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
    ledger.setdefault('order_history', []).extend(copy.deepcopy(orders))
    ledger['open_orders'] = []


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
