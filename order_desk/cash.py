"""Signed settlement checks with separate filled and unfilled obligations."""
from __future__ import annotations
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from order_desk.capital import filled_shares, quantity_to_shares
from order_desk.validation import nonnegative_integer
from order_desk.signal import SignalError

TAIPEI = ZoneInfo('Asia/Taipei')
TERMINAL = {'Filled', 'Cancelled', 'Failed', 'Expired'}


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


def confirm_settlement_posting(ledger, settlement_date, amount_twd, *, confirmed_at=None):
    """Record an explicit account-owner confirmation; this never infers a debit."""
    binding = ledger.get('account_binding')
    if not isinstance(binding, dict) or not binding.get('account') or binding.get('mode') != 'production':
        raise SignalError('posting confirmation requires a bound production account')
    at = confirmed_at or datetime.now(TAIPEI)
    day = datetime.fromisoformat(settlement_date).date().isoformat()
    if day != settlement_date or day != at.astimezone(TAIPEI).date().isoformat():
        raise SignalError('posting confirmation must be for the current settlement date')
    amount = money(amount_twd, 'confirmed bank debit')
    if amount <= 0:
        raise SignalError('confirmed bank debit must be positive')
    confirmations = ledger.setdefault('settlement_posting_confirmations', [])
    for row in confirmations:
        if (row.get('account_binding') == binding and row.get('settlement_date') == day
                and row.get('amount_twd') == amount
                and row.get('source') == 'account_owner_confirmed_bank_debit'):
            return row
    row = {'account_binding': dict(binding), 'settlement_date': day, 'amount_twd': amount,
           'confirmed_at': at.isoformat(), 'source': 'account_owner_confirmed_bank_debit'}
    confirmations.append(row)
    return row


def _posted_t0_debit(ledger, today, amount, bank_stamp):
    binding = ledger.get('account_binding')
    if amount <= 0 or not isinstance(binding, dict) or not binding.get('account') or binding.get('mode') != 'production':
        return None
    for row in ledger.get('settlement_posting_confirmations', []):
        if (row.get('account_binding') != binding or row.get('settlement_date') != today
                or row.get('source') != 'account_owner_confirmed_bank_debit'):
            continue
        if money(row.get('amount_twd'), 'confirmed bank debit') != amount:
            continue
        at = _stamp(row.get('confirmed_at'))
        if at.date().isoformat() == today and bank_stamp >= at:
            return row
    return None


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
        settlements[t] = value
        dates.add(day)
        dates_by_t[t] = parsed_day
    if len(dates_by_t) == 3 and not dates_by_t[0] < dates_by_t[1] < dates_by_t[2]:
        raise SignalError('settlement periods out of order')
    if snapshot.get('mode') == 'production' and set(settlements) != {0, 1, 2}:
        raise SignalError('complete T/T+1/T+2 settlement data required')
    # Broker rows are authoritative for cancellations. Local-only rows cover the
    # interval between acceptance and visibility in the query, without summing a
    # stale local Submitted row on top of a fresh broker Filled/Cancelled row.
    rows = {}
    for row in snapshot['orders']:
        if row.get('action') not in {'Buy', 'Sell'}:
            raise SignalError('unknown cash order action')
        day = str(row.get('trading_day') or row.get('order_datetime') or today)[:10]
        if day != today:
            continue
        key = str(row.get('broker_order_id') or '')
        if not key or key in rows:
            raise SignalError('invalid or duplicate cash order identity')
        rows[key] = dict(row)
    for saved in ledger.get('open_orders', []) + ledger.get('order_history', []):
        if saved.get('trading_day') != today or saved.get('status') == 'NotSubmitted':
            continue
        key = str(saved.get('broker_order_id') or '')
        if not key:
            raise SignalError('saved cash obligation has no order identity')
        if key in rows:
            if rows[key].get('action') != saved.get('action'):
                raise SignalError('cash order identity/action mismatch')
            if filled_shares(rows[key], rows[key].get('order_lot', 'IntradayOdd')) < saved.get('applied_shares', 0):
                raise SignalError('cash order fills went backwards')
            continue
        row = dict(saved.get('broker_snapshot') or {})
        row.update(saved)
        original_lot = row.get('order_lot', 'IntradayOdd')
        row['filled_shares'] = nonnegative_integer(saved.get('applied_shares', 0))
        row['quantity'] = nonnegative_integer(saved['shares'])
        row['cancelled_quantity'] = quantity_to_shares(row.get('cancelled_quantity', 0), original_lot)
        detail = dict(row.get('status_detail') or {})
        detail['deals'] = [dict(d, quantity=quantity_to_shares(d['quantity'], original_lot))
                           for d in detail.get('deals', [])]
        row['status_detail'] = detail
        row['order_lot'] = 'IntradayOdd'  # All local quantities now use shares.
        rows[key] = row
    filled_buys = filled_sales = pending_buys = 0.0
    details = []
    for key, row in rows.items():
        action = row.get('action')
        if action not in {'Buy', 'Sell'}:
            raise SignalError('unknown saved cash order action')
        lot = row.get('order_lot', 'IntradayOdd')
        filled = filled_shares(row, lot)
        # Unfilled sells do not consume cash and cannot fund buys.
        if action == 'Sell' and not filled:
            continue
        total = quantity_to_shares(row['quantity'], lot) if action == 'Buy' else filled
        status = row.get('status')
        if filled > total or (status == 'Filled' and filled == 0):
            raise SignalError('invalid confirmed cash order quantity')
        if status not in TERMINAL | {'Submitted', 'PendingSubmit', 'PreSubmitted', 'Filling', 'PartFilled', 'SubmissionUnknown'}:
            raise SignalError('unknown cash order status')
        cancelled = quantity_to_shares(row.get('cancelled_quantity', 0), lot) if action == 'Buy' else 0
        if filled + cancelled > total:
            raise SignalError('invalid cancelled cash order quantity')
        remaining = 0 if status in TERMINAL else total - filled - cancelled
        price = money(row.get('limit_price', 0) if action == 'Sell' else row.get('limit_price'), 'cash order price') if filled or (action == 'Buy' and remaining) else 0
        deals = row.get('status_detail', {}).get('deals', [])
        deal_shares = sum(quantity_to_shares(d['quantity'], lot) for d in deals)
        if deals and deal_shares != filled:
            raise SignalError('cash deal quantities do not match confirmed fills')
        notional = sum(money(d['price'], 'deal price') * quantity_to_shares(d['quantity'], lot) for d in deals) if deals else filled * price
        if action == 'Buy' and filled and notional <= 0:
            raise SignalError('filled cash order cannot be valued')
        if action == 'Buy':
            if remaining and price <= 0:
                raise SignalError('cash obligation of market order cannot be bounded')
            filled_cost = buy_cost(notional, fee_rate, minimum_fee)
            # One commission budget per order, including partial fills.
            pending_cost = buy_cost(notional + remaining * price, fee_rate, minimum_fee) - filled_cost
            filled_buys += filled_cost
            pending_buys += pending_cost
            details.append({'broker_order_id': key, 'action': action, 'filled_shares': filled,
                            'remaining_shares': remaining, 'filled_cost_twd': filled_cost,
                            'pending_hold_twd': pending_cost})
        else:
            # Only confirmed sales offset confirmed buys on the SAME settlement
            # day. No pending sell is credited. 0.3% is an upper tax buffer for
            # supported cash stocks/ETFs; do not assume day-trade reductions.
            fee = buy_cost(notional, fee_rate, minimum_fee) - notional
            proceeds = max(0.0, math.floor(notional - fee - math.ceil(notional * 0.003)))
            filled_sales += proceeds
            details.append({'broker_order_id': key, 'action': action, 'filled_shares': filled,
                            'confirmed_proceeds_floor_twd': proceeds})
    # Project payment capacity by settlement date, rather than treating sale
    # receivables as bank deposits. T0 postings are ambiguous: reserve any debit
    # unless the owner confirms this exact debit was posted and the bank balance
    # was fetched after that confirmation. Never credit T0 receipts twice.
    t0_reported_payable = max(0.0, -settlements.get(0, 0))
    posted = _posted_t0_debit(ledger, today, t0_reported_payable, stamp)
    t0_hold = 0.0 if posted else t0_reported_payable
    t1_net = settlements.get(1, 0)
    broker_net = settlements.get(2, 0)
    estimated_net = filled_sales - filled_buys
    # Both observations describe today's fills. Choose the lower signed net:
    # larger payable or smaller confirmed receipt, never sum the two sources.
    confirmed_net = min(broker_net, estimated_net)
    broker_payable = max(0.0, -broker_net)
    filled_hold = max(0.0, -confirmed_net)
    today_receivable = max(0.0, confirmed_net)
    earlier_hold = t0_hold + max(0.0, -t1_net)
    today_hold = filled_hold + pending_buys
    bank_only_available = max(0.0, bank - reserve - earlier_hold - today_hold)
    after_t0 = bank - reserve - t0_hold
    after_t1 = after_t0 + t1_net
    earlier_shortfall = max(0.0, -after_t0, -after_t1)
    after_t2 = after_t1 + confirmed_net - pending_buys
    # New buys settle on T+2. A receipt on that date can offset those buys, but
    # cannot cure an uncovered payment on T0 or T+1.
    available = max(0.0, after_t2) if earlier_shortfall == 0 else 0.0
    timeline = [
        {'T': 0, 'date': dates_by_t.get(0, today), 'net_flow_twd': -t0_hold, 'balance_twd': after_t0},
        {'T': 1, 'date': dates_by_t.get(1), 'net_flow_twd': t1_net, 'balance_twd': after_t1},
        {'T': 2, 'date': dates_by_t.get(2), 'net_flow_twd': confirmed_net - pending_buys, 'balance_twd': after_t2},
    ]
    required = sum(buy_cost(o.estimated_notional, fee_rate, minimum_fee) for o in orders if o.action == 'Buy')
    return {'checked_at': now.isoformat(), 'status': 'allowed' if required <= available and earlier_shortfall == 0 else 'insufficient',
            'bank_balance_twd': bank, 'earlier_settlement_hold_twd': earlier_hold,
            'today_commitment_hold_twd': today_hold, 'cash_reserve_twd': reserve,
            'available_twd': available, 'required_twd': required,
            'shortfall_twd': max(earlier_shortfall, required - available, 0.0),
            'bank_only_available_twd': bank_only_available,
            'settlement_buying_power_twd': available,
            'earlier_payment_shortfall_twd': earlier_shortfall,
            'confirmed_receivable_credit_twd': max(0.0, t1_net) + today_receivable,
            'today_confirmed_receivable_credit_twd': today_receivable,
            'settlement_timeline': timeline,
            'settlement_amounts_twd': {str(t): v for t, v in settlements.items()},
            'settlement_receivables_twd': sum(max(0.0, v) for v in settlements.values()),
            'broker_today_payable_twd': broker_payable,
            'estimated_filled_buy_cost_twd': filled_buys,
            'estimated_confirmed_sale_proceeds_twd': filled_sales,
            'filled_net_payable_hold_twd': filled_hold,
            'unfilled_buy_hold_twd': pending_buys,
            'order_commitments': details,
            'today_settlement_status_unverified': t0_hold > 0,
            'today_reported_payable_twd': t0_reported_payable,
            'today_posted_debit_excluded_twd': t0_reported_payable if posted else 0.0,
            'today_posting_confirmation': dict(posted) if posted else None,
            'reason': 'settlement-date buying power includes confirmed receipts once; earlier payments must remain covered; unfilled sells never credited'}


def check_cash(worker, ledger, orders, **options):
    try:
        report = assess_cash(worker.call('cash_snapshot'), ledger, orders, **options)
    except Exception as exc:
        report = {'checked_at': datetime.now(TAIPEI).isoformat(), 'status': 'unavailable',
                  'reason': f'cash check could not verify funds: {type(exc).__name__}: {exc}'}
    ledger['cash_safety'] = report
    ledger.setdefault('cash_safety_history', []).append(dict(report))
    return report
