"""Conservative cash checks; pending sale receipts are never spendable cash."""
from __future__ import annotations
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from order_desk.capital import filled_shares, quantity_to_shares
from order_desk.validation import nonnegative_integer
from order_desk.signal import SignalError

TAIPEI = ZoneInfo('Asia/Taipei')
TERMINAL = {'Filled', 'Cancelled', 'Failed'}


def money(value, label):
    if isinstance(value, bool):
        raise SignalError(f'{label}: invalid amount')
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SignalError(f'{label}: invalid amount') from exc
    if not math.isfinite(result) or result < 0:
        raise SignalError(f'{label}: invalid amount')
    return result


def buy_cost(notional, fee_rate=0.001425, minimum_fee=20.0):
    notional = money(notional, 'buy notional')
    rate = money(fee_rate, 'fee rate')
    minimum = money(minimum_fee, 'minimum fee')
    if notional == 0:
        return 0.0
    return math.ceil(notional + max(minimum, math.ceil(notional * rate)))


def _stamp(raw):
    try:
        parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=TAIPEI) if parsed.tzinfo is None else parsed.astimezone(TAIPEI)
    except (ValueError, TypeError) as exc:
        raise SignalError('cash balance timestamp unavailable') from exc


def assess_cash(snapshot, ledger, orders, *, reserve=0.0, fee_rate=0.001425, minimum_fee=20.0, now=None):
    now = now or datetime.now(TAIPEI)
    today = now.astimezone(TAIPEI).date().isoformat()
    balance = snapshot['balance']
    if balance.get('fetch_status', 'Fetched') not in {'Fetched', 'Success'}:
        raise SignalError('cash balance query is not complete')
    if balance.get('errmsg'):
        raise SignalError('broker balance query returned an error')
    stamp = _stamp(balance.get('date'))
    age = (now - stamp).total_seconds()
    if not -30 <= age <= 120 or stamp.date().isoformat() != today:
        raise SignalError('cash balance is stale; fresh query required')
    bank = money(balance['acc_balance'], 'account balance')
    reserve = money(reserve, 'cash reserve')
    settlements = {}
    dates = set()
    dates_by_t = {}
    for row in snapshot['settlements']:
        t = nonnegative_integer(row.get('T'), 'settlement T')
        day = str(row.get('date', ''))
        if t not in {0, 1, 2} or t in settlements or day in dates:
            raise SignalError('duplicate or unsupported settlement period')
        try:
            parsed_day = datetime.fromisoformat(day).date().isoformat()
        except ValueError as exc:
            raise SignalError('invalid settlement date') from exc
        if (t == 0 and parsed_day != today) or (t > 0 and parsed_day <= today):
            raise SignalError('settlement dates are not current')
        value = float(row['amount'])
        if isinstance(row['amount'], bool) or not math.isfinite(value):
            raise SignalError('invalid settlement amount')
        settlements[t] = abs(value)
        dates.add(day)
        dates_by_t[t] = parsed_day
    if len(dates_by_t) == 3 and not dates_by_t[0] < dates_by_t[1] < dates_by_t[2]:
        raise SignalError('settlement periods out of order')
    if snapshot.get('mode') == 'production' and set(settlements) != {0, 1, 2}:
        raise SignalError('complete T/T+1/T+2 settlement data required')
    commitments = {}
    for row in snapshot['orders']:
        if row.get('action') != 'Buy':
            continue
        day = str(row.get('trading_day') or row.get('order_datetime') or today)[:10]
        if day != today:
            continue
        key = str(row.get('broker_order_id') or '')
        if not key or key in commitments:
            raise SignalError('invalid or duplicate cash order identity')
        filled = filled_shares(row, row.get('order_lot', 'IntradayOdd'))
        status = row.get('status')
        shares = filled if status in TERMINAL else quantity_to_shares(row['quantity'], row.get('order_lot'))
        if status == 'Filled' and filled == 0:
            raise SignalError('filled cash order has no confirmed quantity')
        price = money(row.get('limit_price'), 'cash order price') if shares else 0
        if shares and price <= 0:
            raise SignalError('cash obligation of market order cannot be bounded')
        commitments[key] = buy_cost(shares * price, fee_rate, minimum_fee)
    # Newly sent orders may not yet appear in the broker query. The durable ledger
    # reserves them too; match by order ID so the same order is not counted twice.
    for row in ledger.get('open_orders', []) + ledger.get('order_history', []):
        if row.get('action') != 'Buy' or row.get('trading_day') != today:
            continue
        if row.get('status') == 'NotSubmitted':
            continue
        key = str(row.get('broker_order_id') or '')
        if not key:
            raise SignalError('saved cash obligation has no order identity')
        shares = nonnegative_integer(row.get('applied_shares', 0) if row.get('status') in TERMINAL else row['shares'])
        raw_price = row.get('limit_price', row.get('broker_snapshot', {}).get('limit_price'))
        price = money(raw_price, 'saved order price') if shares else 0
        if shares and price <= 0:
            raise SignalError('saved cash obligation cannot be valued')
        commitments[key] = max(commitments.get(key, 0), buy_cost(shares * price, fee_rate, minimum_fee))
    earlier_hold = settlements.get(0, 0) + settlements.get(1, 0)
    today_hold = max(settlements.get(2, 0), sum(commitments.values()))
    available = max(0.0, bank - reserve - earlier_hold - today_hold)
    required = sum(buy_cost(o.estimated_notional, fee_rate, minimum_fee) for o in orders if o.action == 'Buy')
    return {'checked_at': now.isoformat(), 'status': 'allowed' if required <= available else 'insufficient',
            'bank_balance_twd': bank, 'earlier_settlement_hold_twd': earlier_hold,
            'today_commitment_hold_twd': today_hold, 'cash_reserve_twd': reserve,
            'available_twd': available, 'required_twd': required,
            'shortfall_twd': max(0.0, required - available),
            'reason': 'conservative balance less unsettled amounts and existing buy commitments; future sale receipts not credited'}


def check_cash(worker, ledger, orders, **options):
    try:
        report = assess_cash(worker.call('cash_snapshot'), ledger, orders, **options)
    except Exception as exc:
        report = {'checked_at': datetime.now(TAIPEI).isoformat(), 'status': 'unavailable',
                  'reason': f'cash check could not verify funds: {type(exc).__name__}: {exc}'}
    ledger['cash_safety'] = report
    ledger.setdefault('cash_safety_history', []).append(dict(report))
    return report
