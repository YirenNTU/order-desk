"""Reconcile evidenced inventory differences and quarantine unsettled reservations."""
from __future__ import annotations
import copy
from order_desk.signal import SignalError


class ReplanRequired(SignalError):
    pass


def sync_quarantine(ledger, report):
    issues = report.get('inventory_issues', {})
    old = ledger.get('inventory_quarantine', {})
    current = {}
    for ticker, reasons in issues.items():
        position = next(r for r in report['positions'] if r['ticker'] == ticker)
        current[ticker] = {'ticker': ticker, 'reasons': reasons,
                           'broker_shares': position['broker_shares'],
                           'strategy_shares': position['program_shares'],
                           'external_shares': position['external_shares'],
                           'difference_shares': position['difference_shares'],
                           'first_seen': old.get(ticker, {}).get('first_seen', report['checked_at'])}
    changed = set(old) != set(current)
    if changed and ledger.get('execution'):
        pending = ledger['execution']
        if not pending.get('budgets') or not pending.get('targets') or not pending.get('signal_signature'):
            raise SignalError('inventory changed but saved cycle budgets are unavailable; recover original plan first')
    if current != old:
        for ticker in sorted(set(old) | set(current)):
            if old.get(ticker) != current.get(ticker):
                ledger.setdefault('inventory_quarantine_history', []).append({
                    'checked_at': report['checked_at'], 'ticker': ticker,
                    'event': 'released' if ticker not in current else ('quarantined' if ticker not in old else 'updated'),
                    'before': copy.deepcopy(old.get(ticker)), 'after': copy.deepcopy(current.get(ticker))})
        ledger['inventory_quarantine'] = current
    if changed and ledger.get('execution'):
        ledger['execution']['reweight_required'] = True
    return changed


def reconcile_actual_inventory(ledger, report, quotes):
    """Book explicit reconciliation adjustments, never invented broker executions."""
    from order_desk.planner import _price_for_size, _validate_quote
    staged = copy.deepcopy(ledger)
    changed = False
    for position in report['positions']:
        ticker = position['ticker']
        if ticker not in report.get('inventory_issues', {}):
            continue
        owners = {name: int(held.get(ticker, 0)) for name, held in staged['strategies'].items() if held.get(ticker, 0)}
        total = sum(owners.values())
        broker = int(position['broker_shares'])
        if broker < 0:
            raise SignalError('negative broker inventory cannot be reconciled as cash stock')
        external = max(0, int(position['external_shares']))
        available = min(total, max(0, broker - external))
        allocated = {name: available * count // total for name, count in owners.items()} if total else {}
        spare = available - sum(allocated.values())
        ranking = sorted(owners, key=lambda name: (-(available * owners[name] % total), name))
        for name in ranking[:spare]:
            allocated[name] += 1
        changes = {name: allocated[name] - held for name, held in owners.items() if allocated[name] != held}
        required_external = broker - available
        external_delta = required_external - int(position['external_shares'])
        if not changes and external_delta == 0:
            continue  # A pending manual sell is isolated, not treated as an actual fill.
        price = None
        budget_changes = {}
        if changes:
            if ticker not in quotes:
                raise SignalError(f'{ticker}: price required to adjust actual strategy capital')
            _validate_quote(quotes[ticker])
            price = _price_for_size(quotes[ticker])
            execution = staged.get('execution')
            if execution and (not execution.get('budgets') or not execution.get('targets') or not execution.get('signal_signature')):
                raise SignalError('original cycle capital missing; cannot safely retain uninvested sale proceeds')
            for name, delta in changes.items():
                if allocated[name]:
                    staged['strategies'][name][ticker] = allocated[name]
                else:
                    staged['strategies'][name].pop(ticker, None)
                if execution:
                    if name not in execution['budgets']:
                        raise SignalError(f'{name}: original strategy capital missing')
                    before = float(execution['budgets'][name])
                    after = max(0.0, before + delta * price)
                    execution['budgets'][name] = after
                    budget_changes[name] = {'before': before, 'after': after}
                elif name in staged.get('manual_session', {}).get('carry_budgets', {}):
                    carried = staged['manual_session']['carry_budgets']
                    before = float(carried[name])
                    carried[name] = max(0.0, before + delta * price)
                    budget_changes[name] = {'before': before, 'after': carried[name]}
            if execution:
                execution['reweight_required'] = True
                execution['reallocate_strategies'] = sorted(set(execution.get('reallocate_strategies', [])) | set(changes))
        if report.get('inventory_baseline') is not None and external_delta:
            adjustments = staged.setdefault('external_inventory_adjustments', {})
            adjustments[ticker] = adjustments.get(ticker, 0) + external_delta
        staged.setdefault('inventory_adjustment_history', []).append({
            'checked_at': report['checked_at'], 'ticker': ticker,
            'type': 'reconciliation_adjustment_not_broker_fill',
            'broker_shares': broker, 'strategy_before': owners, 'strategy_after': allocated,
            'external_before': position['external_shares'], 'external_after': required_external,
            'valuation_price': price, 'budget_adjustments': budget_changes,
            'allocation_rule': 'proportional shares; largest remainder; strategy-name tie break',
            'funding_rule': 'retain saved cycle funding cap and subtract lost shares at current mark; do not credit unassigned manual proceeds',
            'evidence': copy.deepcopy(report['inventory_issues'][ticker]),
        })
        changed = True
    if changed:
        staged['strategies'] = {name: held for name, held in staged['strategies'].items() if held}
        ledger.clear()
        ledger.update(staged)
    return changed
