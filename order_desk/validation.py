"""Validate Taiwan cash-stock quantities before a broker call or ledger mutation."""
from __future__ import annotations
import math
from order_desk.signal import SignalError


def nonnegative_integer(value, label='quantity'):
    try:
        integer = int(value)
        valid = not isinstance(value, bool) and integer >= 0 and float(value) == integer
    except (ValueError, TypeError, OverflowError):
        valid = False
    if not valid:
        raise SignalError(f'{label}: expected a nonnegative integer')
    return integer


def validate_intent(order):
    if order.action not in {'Buy', 'Sell'} or order.order_lot not in {'Common', 'IntradayOdd'}:
        raise SignalError('invalid stock order action or lot')
    shares = nonnegative_integer(order.shares, 'shares')
    quantity = nonnegative_integer(order.broker_quantity, 'broker quantity')
    if not shares or not quantity:
        raise SignalError('stock order quantity must be positive')
    if order.order_lot == 'Common':
        if shares != quantity * 1000 or quantity > 499:
            raise SignalError('Common orders require 1000 shares per unit, at most 499 units')
    elif shares != quantity or shares > 999:
        raise SignalError('IntradayOdd orders require 1 to 999 shares')
    if not math.isfinite(order.limit_price) or order.limit_price <= 0:
        raise SignalError('stock order price must be finite and positive')
    if (not math.isfinite(order.estimated_notional)
            or abs(order.estimated_notional - shares * order.limit_price) > 0.011):
        raise SignalError('stock order notional does not match price and quantity')
    if not order.client_order_id or not str(order.ticker):
        raise SignalError('stock order identity is missing')


def validate_allocations(orders, allocations, submitted=()):
    ids = [order.client_order_id for order in orders]
    if len(ids) != len(set(ids)) or len(submitted) != len(set(submitted)) or set(submitted) - set(ids):
        raise SignalError('duplicate or unknown saved order identity')
    for order in orders:
        validate_intent(order)
        legs = allocations.get(order.client_order_id, [])
        quantities = [nonnegative_integer(leg.get('shares'), 'allocation shares') for leg in legs]
        if (not legs or any(q <= 0 for q in quantities) or sum(quantities) != order.shares
                or any(not isinstance(leg.get('strategy'), str) or not leg['strategy'] for leg in legs)):
            raise SignalError(f'{order.ticker}: invalid strategy allocation')
