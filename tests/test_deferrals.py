import copy
import json
from datetime import datetime, timedelta, timezone, date
import pytest
from order_desk.books import plan_strategies, load_strategy_entries
from order_desk.deferrals import cycle_id, defer_restricted_orders, frozen_tickers, record_retry_checks, signal_signature
from order_desk.capital import save_account_ledger, load_account_ledger
from order_desk.execution import submit_sells_then_buys
from order_desk.schedule import write_order_stamp, order_stamp_blocks_retry
from order_desk.signal import SignalError
from test_capital import _quote, _write_signal, _policy, NOW


def entries(tmp_path):
    _write_signal(tmp_path/'a/signal.json','a',[{'ticker':'6213','weight':.2},{'ticker':'2330','weight':.3},{'ticker':'2317','weight':.5}])
    _write_signal(tmp_path/'b/signal.json','b',[{'ticker':'3037','weight':1}])
    return load_strategy_entries({'strategies':[{'name':n,'signal_path':f'{n}/signal.json'} for n in ('a','b')]},tmp_path,now=NOW)


def ledger_for(es):
    order={'broker_order_id':'failed','client_order_id':'old','ticker':'6213','action':'Sell','shares':3,
           'applied_shares':0,'status':'Failed','order_lot':'IntradayOdd','trading_day':'2026-09-28',
           'allocation':[{'strategy':'a','shares':3,'applied':0}],
           'broker_snapshot':{'status_detail':{'code':'88','message':'警示股，須預收0元，圈存3股'}}}
    return {'strategies':{'a':{'6213':7,'2330':2},'b':{'3037':10}},'open_orders':[order],
            'execution':{'cycle_id':cycle_id(NOW),'trading_day':'2026-09-28','budgets':{'a':2000,'b':1000},
                         'targets':{'a':{'6213':4,'2330':6,'2317':10},'b':{'3037':10}},
                         'signal_signature':signal_signature(es),'submitted':['old'],'orders':[],'allocations':{}}}


def make_preview(es,ledger,now=NOW):
    return plan_strategies(es,ledger,{t:_quote(t) for t in ('6213','2330','2317','3037')},
                           {'6213':7,'2330':2,'3037':10},10000,_policy(),mode='production',now=now)


def test_frozen_value_deducted_then_weights_normalized_per_strategy(tmp_path):
    es=entries(tmp_path);ledger=ledger_for(es)
    assert defer_restricted_orders(ledger,cycle_id(NOW))
    preview=make_preview(es,ledger)
    # 2000 - all seven frozen shares * 100 = 1300; 3:5 tradable ratio.
    assert preview['targets']['a']=={'6213':7,'2330':4,'2317':8}
    assert preview['targets']['b']=={'3037':10}
    assert not any(o.ticker=='6213' for o in preview['plan'].orders)
    assert sum(q*100 for q in preview['targets']['a'].values())<=2000
    assert preview['budgets']['a']==2000
    assert any('frozen holdings 700.00' in w for w in preview['plan'].warnings)
    assert make_preview(es,ledger)['targets']==preview['targets']
    assert ledger['strategies']['a']['6213']==7


def test_original_budget_preserves_sold_cash_and_does_not_reapply_extra(tmp_path):
    es=entries(tmp_path);es[0]['extra_twd']=99999
    ledger=ledger_for(es);defer_restricted_orders(ledger,cycle_id(NOW))
    assert make_preview(es,ledger)['budgets']['a']==2000


def test_unaffected_targets_do_not_drift_with_new_quotes(tmp_path):
    es=entries(tmp_path);ledger=ledger_for(es);defer_restricted_orders(ledger,cycle_id(NOW))
    quotes={t:_quote(t) for t in ('6213','2330','2317')};quotes['3037']=_quote('3037',120)
    p=plan_strategies(es,ledger,quotes,{'6213':7,'2330':2,'3037':10},10000,_policy(),mode='production',now=NOW)
    assert p['targets']['b']=={'3037':10}


def test_deferral_survives_restart_without_repeating_history(tmp_path):
    es=entries(tmp_path);ledger=ledger_for(es);defer_restricted_orders(ledger,cycle_id(NOW))
    save_account_ledger(tmp_path,ledger,{})
    loaded,_=load_account_ledger(tmp_path)
    assert not defer_restricted_orders(loaded,cycle_id(NOW))
    assert len(loaded['deferred_orders'])==len(loaded['order_history'])==1
    d=loaded['deferred_orders'][0]
    assert (d['unfilled_shares'],d['retry_from'])==(3,'2026-10-05')
    assert d['allocation'][0]['strategy']=='a'
    assert loaded['execution']['reweight_required']


@pytest.mark.parametrize('status,message',[('Unconfirmed','圈存3股'),('PartFilled','圈存3股'),('Failed','餘額不足'),('Cancelled','')])
def test_only_confirmed_restriction_failures_can_be_deferred(tmp_path,status,message):
    es=entries(tmp_path);ledger=ledger_for(es)
    ledger['open_orders'][0]['status']=status
    ledger['open_orders'][0]['broker_snapshot']['status_detail']['message']=message
    original=copy.deepcopy(ledger)
    assert not defer_restricted_orders(ledger,cycle_id(NOW))
    assert ledger==original


def test_unconfirmed_other_order_remains_blocking(tmp_path):
    es=entries(tmp_path);ledger=ledger_for(es)
    other=copy.deepcopy(ledger['open_orders'][0]);other.update(broker_order_id='unknown',status='Unconfirmed')
    ledger['open_orders'].append(other)
    defer_restricted_orders(ledger,cycle_id(NOW))
    assert len(ledger['open_orders'])==1
    with pytest.raises(SignalError,match='open orders'):
        submit_sells_then_buys(None,[],ledger,{},cash_reserve_twd=0,wait_seconds=0,poll_seconds=0)


def test_stale_buys_cannot_resume_after_deferral(tmp_path):
    es=entries(tmp_path);ledger=ledger_for(es);defer_restricted_orders(ledger,cycle_id(NOW))
    with pytest.raises(SignalError,match='reweighted plan'):
        submit_sells_then_buys(None,[],ledger,{},cash_reserve_twd=0,wait_seconds=0,poll_seconds=0)


def test_next_cycle_releases_freeze_and_records_new_sell_attempt(tmp_path):
    es=entries(tmp_path);ledger=ledger_for(es);defer_restricted_orders(ledger,cycle_id(NOW))
    ledger.pop('execution') # previous tradable plan completed
    next_day=NOW+timedelta(days=7)
    for e in es:e['signal']['expires_at']=(next_day+timedelta(days=2)).isoformat()
    p=make_preview(es,ledger,now=next_day)
    assert frozen_tickers(ledger,cycle_id(next_day))==set()
    assert any(o.ticker=='6213' and o.action=='Sell' for o in p['plan'].orders)
    record_retry_checks(ledger,p);record_retry_checks(ledger,p)
    checks=ledger['deferred_orders'][0]['retry_checks']
    assert len(checks)==1 and checks[0]['orders'][0]['action']=='Sell'


def test_next_day_same_cycle_does_not_unfreeze(tmp_path):
    es=entries(tmp_path);ledger=ledger_for(es);defer_restricted_orders(ledger,cycle_id(NOW))
    assert frozen_tickers(ledger,cycle_id(NOW+timedelta(days=1)))=={'6213'}


def test_all_weights_frozen_retains_shares_and_leaves_cash(tmp_path):
    es=entries(tmp_path);es[0]['strategy']['weights']=[{'ticker':'6213','weight':1}]
    ledger=ledger_for(es);defer_restricted_orders(ledger,cycle_id(NOW))
    p=make_preview(es,ledger)
    assert p['targets']['a']=={'6213':7}
    assert any('remains unallocated' in w for w in p['plan'].warnings)


def test_frozen_value_over_budget_fails_closed(tmp_path):
    es=entries(tmp_path);ledger=ledger_for(es);ledger['execution']['budgets']['a']=600
    defer_restricted_orders(ledger,cycle_id(NOW))
    with pytest.raises(SignalError,match='frozen holdings exceed'):make_preview(es,ledger)


def test_partial_fill_freezes_actual_remaining_inventory(tmp_path):
    es=entries(tmp_path);ledger=ledger_for(es)
    ledger['strategies']['a']['6213']=6
    ledger['open_orders'][0]['applied_shares']=1;ledger['open_orders'][0]['allocation'][0]['applied']=1
    defer_restricted_orders(ledger,cycle_id(NOW))
    assert ledger['deferred_orders'][0]['unfilled_shares']==2
    assert make_preview(es,ledger)['targets']['a']['6213']==6


def test_completed_with_deferrals_does_not_repeat_this_week(tmp_path):
    path=tmp_path/'stamp.json'
    write_order_stamp(path,week='2026-09-28',trading_day='2026-09-28',status='completed_with_deferrals')
    assert order_stamp_blocks_retry(path,date(2026,9,29))
    assert not order_stamp_blocks_retry(path,date(2026,10,5))


def test_cli_preview_records_deferral_then_send_uses_new_allocation(tmp_path,monkeypatch):
    import order_desk.__main__ as cli
    from order_desk.execution import taipei_today
    from order_desk.broker import FakeBroker, BrokerWorker
    es=entries(tmp_path)
    now=datetime.now(timezone.utc)
    for e in es:
        e['signal']['effective_at']='2000-01-01T00:00:00+00:00'
        e['signal']['expires_at']='2099-01-01T00:00:00+00:00'
        e['path'].write_text(json.dumps(e['signal']))
    ledger=ledger_for(es)
    ledger['execution']['cycle_id']=cycle_id(now);ledger['execution']['trading_day']=taipei_today()
    ledger['open_orders'][0]['trading_day']=taipei_today()
    save_account_ledger(tmp_path,ledger,{})
    config={'mode':'production','lot_mode':'odd','restricted_order_policy':'freeze_reweight',
            'strategies':[{'name':n,'signal_path':f'{n}/signal.json'} for n in ('a','b')]}
    (tmp_path/'config.json').write_text(json.dumps(config))
    row={'broker_order_id':'failed','ticker':'6213','action':'Sell','order_lot':'IntradayOdd',
         'status':'Failed','filled_shares':0,'status_detail':{'code':'88','message':'須預收0元，圈存3股'}}
    class Broker(FakeBroker):
        def reconcile_orders(self):return [row]+super().reconcile_orders()
    fake=Broker(positions={'6213':7,'2330':2,'3037':10},cash=10000,auto_fill=True)
    fake.set_quotes({t:_quote(t) for t in ('6213','2330','2317','3037')})
    monkeypatch.setattr(cli,'_broker',lambda mode:BrokerWorker(fake))
    args=['--config',str(tmp_path/'config.json')]
    assert cli.main(args)==0
    staged,_=load_account_ledger(tmp_path)
    assert staged['strategies']==ledger['strategies']
    assert staged['execution']['reweight_required']
    assert not fake.orders
    monkeypatch.setattr(cli, '_refuse_weekly_order', lambda *a: 0)
    assert cli.main(args+['--weekly-order'])==0
    final,_=load_account_ledger(tmp_path)
    assert final['strategies']['a']=={'6213':7,'2330':4,'2317':8}
    assert not any(o['ticker']=='6213' for o in fake.orders.values())
    assert final['execution_history'][-1]['outcome']=='completed_with_deferrals'
    assert final['deferred_orders'][0]['unfilled_shares']==3
    assert cli._weekly_order_status(tmp_path,0)=='completed_with_deferrals'


def test_unsubmitted_reweight_can_retry_in_new_cycle_without_losing_sold_cash(tmp_path):
    es=entries(tmp_path);ledger=ledger_for(es);defer_restricted_orders(ledger,cycle_id(NOW))
    next_day=NOW+timedelta(days=7)
    for e in es:
        e['signal']['expires_at']=(next_day+timedelta(days=2)).isoformat()
        e['signal']['revision']=2
    preview=make_preview(es,ledger,now=next_day)
    assert preview['budgets']['a']==2000
    assert preview['targets']['a']['6213']==4
    assert any(o.ticker=='6213' and o.action=='Sell' and o.shares==3 for o in preview['plan'].orders)
    record_retry_checks(ledger,preview)
    assert ledger['deferred_orders'][0]['retry_checks'][0]['cycle_id']=='2026-10-05'


def test_missing_cycle_budget_prevents_unsafe_deferral(tmp_path):
    es=entries(tmp_path);ledger=ledger_for(es);ledger['execution'].pop('budgets')
    original=copy.deepcopy(ledger)
    with pytest.raises(SignalError,match='budgets/targets missing'):
        defer_restricted_orders(ledger,cycle_id(NOW))
    assert ledger==original
