import copy,json
from datetime import datetime,timezone
import pytest
from order_desk.audit import audit_account,record_audit,require_clear_audit,audit_with_history
from order_desk.inventory import sync_quarantine,ReplanRequired
from order_desk.capital import save_account_ledger,load_account_ledger
from order_desk.models import BrokerPosition
from order_desk.broker import BrokerWorker,FakeBroker
from order_desk.signal import SignalError
from test_capital import _write_signal,_quote


def fixture(tmp_path,monkeypatch,*,initial_short=True,short_after_sell=False):
    import order_desk.__main__ as cli
    _write_signal(tmp_path/'strategies/alpha/signal.json','alpha',[
        {'ticker':'6213','weight':.5},{'ticker':'2330','weight':.25},{'ticker':'2317','weight':.25}])
    p=tmp_path/'strategies/alpha/signal.json';s=json.loads(p.read_text());s.update(effective_at='2000-01-01T00:00:00+00:00',expires_at='2099-01-01T00:00:00+00:00');p.write_text(json.dumps(s))
    config={'mode':'production','lot_mode':'odd','inventory_mismatch_policy':'quarantine',
            'strategies':[{'name':'alpha','signal_path':'strategies/alpha/signal.json'}]}
    (tmp_path/'config.json').write_text(json.dumps(config))
    ledger={'strategies':{'alpha':{'6213':10,'2330':10}},'open_orders':[]}
    save_account_ledger(tmp_path,ledger,{})
    class Broker(FakeBroker):
        manual=False
        def reconcile_orders(self):
            rows=super().reconcile_orders()
            if self.manual:
                rows.append({'broker_order_id':'manual-sell','ticker':'6213','action':'Sell','order_lot':'IntradayOdd','filled_shares':5,'status':'Filled','custom_field':''})
            return rows
        def place_order(self,intent):
            result=super().place_order(intent)
            if short_after_sell and intent.action=='Sell' and intent.ticker=='2330' and not self.manual:
                self._positions['6213']-=5
                self.manual=True
            return result
    fake=Broker(positions={'6213':10,'2330':10},cash=10000,auto_fill=True)
    fake.set_quotes({t:_quote(t) for t in ('6213','2330','2317')})
    w=BrokerWorker(fake)
    try:record_audit(tmp_path,audit_account(w,ledger))
    finally:w.close()
    if initial_short:
        fake._positions['6213']=5;fake.manual=True
    monkeypatch.setattr(cli,'_broker',lambda mode:BrokerWorker(fake))
    return cli,fake,['--config',str(tmp_path/'config.json'),'--send','--confirm','APPROVE']


def test_one_short_stock_does_not_block_other_stock_orders(tmp_path,monkeypatch):
    cli,fake,args=fixture(tmp_path,monkeypatch)
    assert cli.main(args)==0
    ledger,_=load_account_ledger(tmp_path)
    assert ledger['strategies']['alpha']=={'6213':10,'2330':5,'2317':5}
    assert fake._positions['6213']==5 # discrepancy retained, not disguised as a fill
    assert set(ledger['inventory_quarantine'])=={'6213'}
    assert [(o['ticker'],o['action'],o['shares']) for o in fake.orders.values()]==[('2330','Sell',5),('2317','Buy',5)]
    assert ledger['execution_history'][-1]['outcome']=='completed_with_deferrals'
    assert ledger['inventory_quarantine_history'][0]['event']=='quarantined'


def test_new_difference_between_sells_and_buys_replans_without_resending_sells(tmp_path,monkeypatch):
    cli,fake,args=fixture(tmp_path,monkeypatch,initial_short=False,short_after_sell=True)
    assert cli.main(args)==0
    ledger,_=load_account_ledger(tmp_path)
    assert set(ledger['inventory_quarantine'])=={'6213'}
    assert len(fake.orders)==2
    assert [(o['action'],o['ticker']) for o in fake.orders.values()]==[('Sell','2330'),('Buy','2317')]
    assert ledger['strategies']['alpha']=={'6213':10,'2330':5,'2317':5}
    assert len(ledger['order_history'])==2


def test_recovered_inventory_releases_quarantine_with_history(tmp_path,monkeypatch):
    cli,fake,args=fixture(tmp_path,monkeypatch)
    assert cli.main(args)==0
    ledger,_=load_account_ledger(tmp_path)
    w=BrokerWorker(fake)
    try:r=audit_with_history(w,ledger,tmp_path,tickers=['6213'])
    finally:w.close()
    assert not sync_quarantine(ledger,r)
    # A reported manual buy restores the five shares; no strategy edit is needed.
    old=fake.reconcile_orders
    fake.reconcile_orders=lambda:old()+[{'broker_order_id':'manual-buy','ticker':'6213','action':'Buy','order_lot':'IntradayOdd','filled_shares':5,'status':'Filled'}]
    fake._positions['6213']=10
    w=BrokerWorker(fake)
    try:r=audit_with_history(w,ledger,tmp_path,tickers=['6213'])
    finally:w.close()
    assert sync_quarantine(ledger,r)
    assert ledger['inventory_quarantine']=={}
    assert ledger['inventory_quarantine_history'][-1]['event']=='released'
    assert ledger['strategies']['alpha']['6213']==10


def test_quarantine_cannot_bypass_unknown_program_order(tmp_path,monkeypatch):
    cli,fake,args=fixture(tmp_path,monkeypatch)
    original=fake.reconcile_orders
    fake.reconcile_orders=lambda:original()+[{'broker_order_id':'unknown-program','ticker':'6213','action':'Buy','order_lot':'IntradayOdd','filled_shares':0,'status':'Submitted','custom_field':'a1b2c3'}]
    assert cli.main(args)==1
    assert not fake.orders


def test_stock_shortage_never_produces_an_order_for_quarantined_stock(tmp_path,monkeypatch):
    cli,fake,args=fixture(tmp_path,monkeypatch)
    assert cli.main(args[:2])==0
    ledger,_=load_account_ledger(tmp_path)
    assert ledger['strategies']['alpha']=={'6213':10,'2330':10}
    assert ledger['inventory_quarantine']['6213']['broker_shares']==5
    assert not fake.orders


def test_automatic_replanning_is_bounded(monkeypatch,tmp_path):
    import order_desk.__main__ as cli
    calls=[]
    monkeypatch.setattr(cli,'_run_account_once',lambda *args:calls.append(1) or 3)
    assert cli._run_account(None,{},tmp_path)==1
    assert len(calls)==3


def test_strict_audit_remains_strict_unless_exact_stock_is_excluded():
    report={'blockers':['A mismatch','B mismatch','unknown order'],
            'inventory_issues':{'A':['A mismatch'],'B':['B mismatch']}}
    with pytest.raises(SignalError,match='B mismatch'):
        require_clear_audit(report,excluded_tickers={'A'})
    with pytest.raises(SignalError,match='unknown order'):
        require_clear_audit(report,excluded_tickers={'A','B'})


def actual_policy(tmp_path):
    path=tmp_path/'config.json';config=json.loads(path.read_text());config['inventory_mismatch_policy']='reconcile_actual';path.write_text(json.dumps(config))


def test_actual_inventory_mode_corrects_shares_and_continues_all_targets(tmp_path,monkeypatch):
    cli,fake,args=fixture(tmp_path,monkeypatch)
    actual_policy(tmp_path)
    assert cli.main(args)==0
    ledger,_=load_account_ledger(tmp_path)
    assert ledger['inventory_quarantine']=={} if 'inventory_quarantine' in ledger else True
    assert ledger['inventory_adjustment_history'][0]['strategy_before']=={'alpha':10}
    assert ledger['inventory_adjustment_history'][0]['strategy_after']=={'alpha':5}
    assert ledger['inventory_adjustment_history'][0]['type']=='reconciliation_adjustment_not_broker_fill'
    assert ledger['execution_history'][-1]['budgets']['alpha']==1500
    assert ledger['strategies']['alpha']=={t:q for t,q in fake._positions.items() if q}
    assert ledger['strategies']['alpha']=={'6213':7,'2330':3,'2317':3}
    assert len(ledger['order_history'])==len(fake.orders)==3


def test_mid_execution_shortage_reduces_cap_but_preserves_unsent_sale_funding(tmp_path,monkeypatch):
    cli,fake,args=fixture(tmp_path,monkeypatch,initial_short=False,short_after_sell=True)
    actual_policy(tmp_path)
    assert cli.main(args)==0
    ledger,_=load_account_ledger(tmp_path)
    adjustment=ledger['inventory_adjustment_history'][0]
    assert adjustment['budget_adjustments']=={'alpha':{'before':2000,'after':1500}}
    # Current stock after first sell is worth 1000, but the cycle still has 500 of approved funding.
    assert ledger['execution_history'][-1]['budgets']['alpha']==1500
    assert ledger['strategies']['alpha']=={'6213':7,'2330':3,'2317':3}
    sells=[o for o in fake.orders.values() if o['ticker']=='2330' and o['action']=='Sell']
    assert [o['shares'] for o in sells]==[5,2] # only the new delta is sent, not the original five again
    assert all(o['ticker']!='6213' or o['action']=='Buy' for o in fake.orders.values())


def test_shared_stock_shortage_uses_deterministic_proportional_allocation():
    from order_desk.inventory import reconcile_actual_inventory
    ledger={'strategies':{'a':{'2330':7},'b':{'2330':3}},'open_orders':[]}
    report={'checked_at':'2026-11-09T05:00:00Z','inventory_baseline':{'checked_at':'2026-11-09T04:00:00Z'},
            'inventory_issues':{'2330':['mismatch']},
            'positions':[{'ticker':'2330','broker_shares':7,'program_shares':10,'external_shares':1,'difference_shares':-4}]}
    assert reconcile_actual_inventory(ledger,report,{'2330':_quote('2330')})
    assert ledger['strategies']=={'a':{'2330':4},'b':{'2330':2}}
    assert ledger.get('external_inventory_adjustments',{})=={}


def test_unexplained_surplus_stays_external_and_does_not_inflate_strategy_capital():
    from order_desk.inventory import reconcile_actual_inventory
    ledger={'strategies':{'a':{'2330':10}},'open_orders':[]}
    report={'checked_at':'2026-11-09T05:00:00Z','inventory_baseline':{'checked_at':'2026-11-09T04:00:00Z'},
            'inventory_issues':{'2330':['mismatch']},
            'positions':[{'ticker':'2330','broker_shares':15,'program_shares':10,'external_shares':0,'difference_shares':5}]}
    assert reconcile_actual_inventory(ledger,report,{})
    assert ledger['strategies']['a']['2330']==10
    assert ledger['external_inventory_adjustments']['2330']==5


def test_reconciliation_is_atomic_when_original_budget_is_missing():
    from order_desk.inventory import reconcile_actual_inventory
    ledger={'strategies':{'a':{'2330':10}},'open_orders':[], 'execution':{'orders':[]}}
    report={'checked_at':'2026-11-09T05:00:00Z','inventory_baseline':{},'inventory_issues':{'2330':['mismatch']},
            'positions':[{'ticker':'2330','broker_shares':5,'program_shares':10,'external_shares':0,'difference_shares':-5}]}
    original=copy.deepcopy(ledger)
    with pytest.raises(SignalError,match='original cycle capital'):
        reconcile_actual_inventory(ledger,report,{'2330':_quote('2330')})
    assert ledger==original


def test_actual_reconciliation_is_idempotent_across_next_audit(tmp_path,monkeypatch):
    cli,fake,args=fixture(tmp_path,monkeypatch)
    actual_policy(tmp_path)
    assert cli.main(args[:2])==0
    first,_=load_account_ledger(tmp_path)
    assert first['strategies']['alpha']['6213']==5
    assert cli.main(args[:2])==0
    second,_=load_account_ledger(tmp_path)
    assert first['inventory_adjustment_history']==second['inventory_adjustment_history']
    assert second['strategies']==first['strategies']
    assert not fake.orders


def test_direct_submission_cannot_trade_quarantined_inventory():
    from order_desk.execution import submit_sells_then_buys
    from test_capital import _intent
    ledger={'strategies':{'alpha':{'2330':10}},'open_orders':[], 'inventory_quarantine':{'2330':{}}}
    with pytest.raises(ReplanRequired):
        submit_sells_then_buys(None,[_intent('2330','Sell',5,100)],ledger,
                              {'2330-Sell-5':[{'strategy':'alpha','shares':5}]},
                              cash_reserve_twd=0,wait_seconds=0,poll_seconds=0)
    assert ledger['execution']['reweight_required']
