"""Reconcile expired day orders against account-bound historical accounting.

Position-detail quantities in Shioaji 1.7 can be truncated to whole lots. Keep
bounds, never mistake a zero-lot row for zero shares. No prices imply fills.
"""
from __future__ import annotations
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo
from order_desk.signal import SignalError
from order_desk.validation import nonnegative_integer as integer

TAIPEI = ZoneInfo('Asia/Taipei')


def _day(value):
    text = str(value)
    if date.fromisoformat(text).isoformat() != text:
        raise SignalError('invalid accounting trade date')
    return text


def accounting_sources(snapshot):
    """Return buy-source bounds and exact historical sell quantities."""
    sources = defaultdict(lambda: [0, 0])
    today = _day(snapshot['end_date'])
    positions = snapshot['positions']
    seen = set()
    for p in positions:
        code = p['ticker']
        if code in seen:
            raise SignalError('duplicate accounting cash position')
        seen.add(code)
        shares = integer(p['shares'])
        lots = p['lots']
        if shares and not lots:
            raise SignalError('nonempty position has no accounting lots')
        bounds = []
        lot_keys = set()
        for lot in lots:
            if lot['ticker'] != code or _day(lot['date']) > today or not lot['dseq']:
                raise SignalError('invalid position lot identity/date')
            key = (lot['date'], code, lot['dseq'])
            if key in lot_keys:
                raise SignalError('duplicate position accounting lot')
            lot_keys.add(key)
            q = integer(lot['quantity'])
            if lot['unit'] == 'Share':
                bounds.append((key, q, q))
            elif lot['unit'] == 'CommonTruncated':
                bounds.append((key, q * 1000, q * 1000 + 999))
            else:
                raise SignalError('unsupported historical quantity unit')
        lo_total, hi_total = sum(x[1] for x in bounds), sum(x[2] for x in bounds)
        if not lo_total <= shares <= hi_total:
            raise SignalError('position lots do not cover exact broker shares')
        for key, lo, hi in bounds:
            # The exact aggregate share count can determine a single remaining
            # lot, but cannot arbitrarily distribute odd shares between lots.
            low = max(lo, shares - (hi_total - hi))
            high = min(hi, shares - (lo_total - lo))
            sources[key][0] += low
            sources[key][1] += high
    sells = defaultdict(int)
    seen = set()
    for sale in snapshot['sales']:
        day, code, seq = _day(sale['date']), sale['ticker'], sale['dseq']
        if not snapshot['begin_date'] <= day <= today or not seq:
            raise SignalError('historical sale outside requested range')
        key = (day, code, seq)
        if key in seen:
            raise SignalError('duplicate historical sale identity')
        seen.add(key)
        qty = integer(sale['shares'])
        if qty <= 0:
            raise SignalError('historical sale must have positive shares')
        details = sale['buy_sources']
        if sum(integer(x['shares']) for x in details) != qty:
            raise SignalError('realized buy-source shares do not cover sale')
        sells[key] += qty
        for lot in details:
            if lot['ticker'] != code or _day(lot['date']) > day or not lot['dseq']:
                raise SignalError('invalid realized buy-source identity/date')
            source_key = (lot['date'], code, lot['dseq'])
            sources[source_key][0] += integer(lot['shares'])
            sources[source_key][1] += integer(lot['shares'])
    return dict(sources), dict(sells)


def resolve_history(snapshot, ledger, pending, *, today):
    """Return evidence-backed synthetic terminal rows, never place any order."""
    _day(snapshot['begin_date'])
    if snapshot['begin_date'] >= today:
        raise SignalError('invalid historical date window')
    identities = [(o['trading_day'], o['broker_order_id']) for o in pending]
    if len(identities) != len(set(identities)):
        raise SignalError('duplicate pending historical order identity')
    binding = ledger.get('account_binding')
    if not binding or snapshot.get('account_binding') != binding:
        raise SignalError('historical accounting account mismatch')
    if snapshot.get('complete') is not True or snapshot.get('end_date') != today:
        raise SignalError('historical accounting coverage incomplete')
    if snapshot.get('positions_stable') is not True:
        raise SignalError('positions changed during historical accounting query')
    now = datetime.now(TAIPEI)
    fetched = datetime.fromisoformat(snapshot['fetched_at']).astimezone(TAIPEI)
    if fetched.date().isoformat() != today or not -30 <= (now - fetched).total_seconds() <= 120:
        raise SignalError('historical accounting snapshot is stale')
    sources, sells = accounting_sources(snapshot)
    known = ledger.get('order_history', []) + ledger.get('open_orders', [])
    results, unresolved = [], []
    payload = {k: v for k, v in snapshot.items() if k != 'fetched_at'}
    evidence_id = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    for order in pending:
        day, code, action = order['trading_day'], order['ticker'], order['action']
        reason = None
        if action not in {'Buy', 'Sell'} or order.get('order_lot') not in {'Common', 'IntradayOdd'}:
            reason = 'unsupported prior-day order side or lot'
        if not snapshot['begin_date'] <= day < today:
            reason = 'order not covered by historical date range'
        saved = order.get('broker_snapshot', {})
        kind = saved.get('order_type', order.get('order_type'))
        # Legacy Order Desk exclusively submitted ROD for these two lot types.
        legacy_rod = (kind is None and order.get('client_order_id') and order.get('custom_field')
                      and order.get('order_lot') in {'Common', 'IntradayOdd'})
        if kind != 'ROD' and not legacy_rod:
            reason = 'cannot establish that prior order was a day order'
        if (saved.get('broker_order_id') != order.get('broker_order_id')
                or saved.get('status') not in {'Submitted', 'PartFilled', 'Filling'}):
            reason = 'missing accepted original order identity'
        if any(saved.get(k) != order.get(k) for k in ('ticker', 'action', 'order_lot')):
            reason = 'saved original order identity does not match'
        saved_day = str(saved.get('trading_day') or saved.get('order_datetime') or '')[:10]
        if saved_day != day:
            reason = 'original accepted order date not established'
        control = [x for x in known if x.get('trading_day') == day and x.get('applied_shares', 0) > 0]
        # Confirm the broker's accounting has rolled forward using previously
        # observed fills. A completely empty/unproven history never means zero.
        if not control:
            reason = 'no confirmed fills to verify historical data freshness'
        expected = defaultdict(int)
        for x in control:
            expected[(x['ticker'], x['action'])] += integer(x['applied_shares'])
        for (ticker, side), count in expected.items():
            if side == 'Buy':
                observed = sum(v[1] for (d,c,_),v in sources.items() if d == day and c == ticker)
            else:
                observed = sum(v for (d,c,_),v in sells.items() if d == day and c == ticker)
            if observed < count:
                reason = 'historical data does not cover previously confirmed fills'
        if reason:
            unresolved.append({'broker_order_id': order['broker_order_id'], 'ticker':code, 'reason':reason})
            continue
        seq = saved.get('broker_order_number') or order.get('broker_order_number')
        pool = sources if action == 'Buy' else {k: [v,v] for k,v in sells.items()}
        candidates = [(key,value) for key,value in pool.items() if key[:2] == (day,code)]
        exact = [value for key,value in candidates if seq and key[2] == seq]
        if exact:
            low, high = exact[0]
            if low != high:
                reason = 'matching accounting lot has ambiguous odd-lot quantity'
            else:
                filled = low
        elif not candidates:
            filled = 0  # No remaining or realized buy source on this trade date.
        elif seq:
            filled = 0  # All accounting identities are known and none matches.
        else:
            reason = 'legacy order lacks broker order number; other same-day trades exist'
        if not reason and not integer(order.get('applied_shares',0)) <= filled <= integer(order['shares']):
            reason = 'historical fills conflict with applied or original shares'
        # One accounting identity must never be assigned to two program orders.
        if seq and sum(1 for x in known if x.get('trading_day') == day and x.get('ticker') == code
                       and (x.get('broker_snapshot',{}).get('broker_order_number') or x.get('broker_order_number')) == seq
                       and x.get('action') == action) != 1:
            reason = 'multiple program orders share historical accounting identity'
        if reason:
            unresolved.append({'broker_order_id':order['broker_order_id'],'ticker':code,'reason':reason})
            continue
        results.append({'broker_order_id':order['broker_order_id'],'ticker':code,'action':action,
            'order_lot':order['order_lot'],'trading_day':day,'filled_shares':filled,
            'status':'Filled' if filled == order['shares'] else 'Expired',
            'broker_order_number':seq or '', 'reconciliation_source':'historical_accounting',
            'evidence_id':evidence_id, 'remaining_shares':order['shares']-filled})
    return results, unresolved, evidence_id
