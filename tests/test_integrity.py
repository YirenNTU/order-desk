import copy
import json
import sqlite3
from pathlib import Path
import pytest
from order_desk.broker import BrokerError, BrokerWorker, FakeBroker
from order_desk.capital import account_lock, load_account_ledger, save_account_ledger
from order_desk.execution import refresh_open_orders, submit_sells_then_buys, taipei_today
from order_desk.audit import audit_account, require_clear_audit
from order_desk.models import BrokerPosition
from order_desk.signal import SignalError
from test_capital import _intent, _open_buy, _Rows


def submit(worker, ledger, orders, allocations, persist=None):
    return submit_sells_then_buys(worker, orders, ledger, allocations, cash_reserve_twd=0,
                                 wait_seconds=0, poll_seconds=0, persist=persist)


def test_intent_durable_before_uncertain_broker_submission(tmp_path):
    ledger = {"strategies": {}, "open_orders": []}
    class Worker:
        calls = 0
        def call(self, method, *args):
            if method == 'reconcile_orders': return []
            assert method == 'place_order'
            saved, _ = load_account_ledger(tmp_path)
            assert saved['open_orders'][0]['status'] == 'Submitting'
            self.calls += 1
            raise BrokerError('response lost after broker accepted')
    w = Worker()
    with pytest.raises(BrokerError):
        submit(w, ledger, [_intent('2330','Buy',6,100)],
               {'2330-Buy-6':[{'strategy':'a','shares':6}]},
               lambda: save_account_ledger(tmp_path,ledger,{}))
    saved,_ = load_account_ledger(tmp_path)
    assert saved['open_orders'][0]['status'] == 'SubmissionUnknown'
    with pytest.raises(SignalError, match='open orders'):
        submit(w,saved,[],{})
    assert w.calls == 1
    order = saved['open_orders'][0]
    row = {'broker_order_id':'real-id','custom_field':order['custom_field'],
           'ticker':'2330','action':'Buy','order_lot':'IntradayOdd','filled_shares':6,'status':'Filled'}
    assert refresh_open_orders(_Rows([row]),saved)
    assert saved['strategies'] == {'a':{'2330':6}}
    assert saved['order_history'][0]['broker_order_id'] == 'real-id'


def test_invalid_allocation_prevents_any_order():
    class NoCalls:
        def call(self,*args): raise AssertionError('must validate first')
    with pytest.raises(SignalError, match='allocation'):
        submit(NoCalls(),{'strategies':{},'open_orders':[]},[_intent('2330','Buy',6,100)],{})


def test_persist_failure_prevents_broker_call():
    class NoCalls:
        def call(self,*args): raise AssertionError('must persist first')
    def fail(): raise OSError('disk full')
    with pytest.raises(OSError):
        submit(NoCalls(),{'strategies':{},'open_orders':[]},[_intent('2330','Buy',6,100)],
               {'2330-Buy-6':[{'strategy':'a','shares':6}]},fail)


def test_refresh_is_atomic_when_second_allocation_fails():
    one=_open_buy(taipei_today()); two=copy.deepcopy(one)
    two.update(broker_order_id='def', action='Sell')
    ledger={'strategies':{},'open_orders':[one,two]}; original=copy.deepcopy(ledger)
    rows=[{'broker_order_id':'abc','filled_shares':6,'status':'Filled'},
          {'broker_order_id':'def','filled_shares':7,'status':'Filled'}]
    with pytest.raises(SignalError):refresh_open_orders(_Rows(rows),ledger)
    assert ledger == original


def test_partial_fill_idempotent_and_history_retained(tmp_path):
    ledger={'strategies':{},'open_orders':[_open_buy(taipei_today())]}
    rows=_Rows([{'broker_order_id':'abc','filled_shares':2,'status':'PartFilled'}])
    refresh_open_orders(rows,ledger); refresh_open_orders(rows,ledger)
    assert ledger['strategies']['alpha']['2330']==2
    save_account_ledger(tmp_path,ledger,{})
    loaded,_=load_account_ledger(tmp_path)
    rows.rows[0].update(filled_shares=6,status='Filled')
    refresh_open_orders(rows,loaded);refresh_open_orders(rows,loaded)
    assert loaded['strategies']['alpha']['2330']==6
    assert len(loaded['order_history'])==1
    assert loaded['open_orders']==[]


def test_committed_snapshot_recovers_partial_multi_file_write(tmp_path,monkeypatch):
    import order_desk.capital as capital
    ledger={'strategies':{'a':{'2330':6},'b':{'2317':3}},'open_orders':[]}
    def fail(*args):raise OSError('power failure')
    monkeypatch.setattr(capital,'_write_strategy_ledger',fail)
    with pytest.raises(OSError):save_account_ledger(tmp_path,ledger,{})
    loaded,_=load_account_ledger(tmp_path)
    assert loaded==ledger
    with sqlite3.connect(tmp_path/'logs/ledger.sqlite3') as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_same_account_cannot_be_opened_twice(tmp_path):
    with account_lock(tmp_path):
        with pytest.raises(SignalError,match='another Order Desk'):
            with account_lock(tmp_path):pass


def test_original_buy_survives_restart_after_sells(tmp_path):
    fake=FakeBroker(positions={'2330':10},cash=0,auto_fill=False); w=BrokerWorker(fake)
    ledger={'strategies':{'a':{'2330':10}},'open_orders':[]}
    orders=[_intent('2330','Sell',10,100),_intent('2317','Buy',10,100)]
    allocations={o.client_order_id:[{'strategy':'a','shares':10}] for o in orders}
    paths={}
    try:
        assert submit(w,ledger,orders,allocations,lambda:save_account_ledger(tmp_path,ledger,paths))=='sells_working'
        first=next(iter(fake.orders))
        fake.fill_order(first)
        ledger,paths=load_account_ledger(tmp_path)
        refresh_open_orders(w,ledger)
        fake.auto_fill=True
        assert submit(w,ledger,[],{},lambda:save_account_ledger(tmp_path,ledger,paths))=='completed'
        assert ledger['strategies']=={'a':{'2317':10}}
        assert len(fake.orders)==2
        assert len(ledger['order_history'])==2
        assert 'execution' not in ledger
    finally:w.close()


def test_cash_check_disabled_never_queries_balance():
    fake=FakeBroker(cash=0,auto_fill=True)
    def bad():raise AssertionError('balance not required')
    fake.available_cash=bad
    w=BrokerWorker(fake);ledger={'strategies':{},'open_orders':[]}
    try:
        assert submit(w,ledger,[_intent('2330','Buy',6,100)],{'2330-Buy-6':[{'strategy':'a','shares':6}]})=='completed'
    finally:w.close()


def test_audit_aggregates_broker_lots_and_blocks_other_orders():
    class Worker:
        def call(self,method):
            if method=='positions':return [BrokerPosition('2330',4),BrokerPosition('2330',6)]
            return [{'broker_order_id':'manual-or-missing','ticker':'2330','status':'PartFilled'}]
    ledger={'strategies':{'a':{'2330':10}},'open_orders':[]}
    report=audit_account(Worker(),ledger)
    assert report['positions'][0]['broker_shares']==10
    assert report['orders'][0]['source']=='external'
    with pytest.raises(SignalError):require_clear_audit(report)


def test_prior_day_pending_plan_cannot_send_stale_limits():
    ledger={'strategies':{},'open_orders':[], 'execution':{'trading_day':'2000-01-01'}}
    with pytest.raises(SignalError,match='prior day'):submit(None,ledger,[],{})


def test_explicit_terminal_retry_only_requeues_unfilled_shares():
    from order_desk.execution import retry_terminal_orders
    o=_open_buy(taipei_today());o.update(status='Cancelled',client_order_id='x',applied_shares=2)
    o['allocation'][0]['applied']=2
    ledger={'strategies':{'alpha':{'2330':2}},'open_orders':[o],
            'execution':{'trading_day':taipei_today(),'submitted':['x'],
                         'orders':[{'client_order_id':'x','order_lot':'IntradayOdd','shares':6,'limit_price':100}],
                         'allocations':{'x':[]}}}
    retry_terminal_orders(ledger)
    assert ledger['execution']['orders'][0]['shares']==4
    assert ledger['execution']['allocations']['x']==[{'strategy':'alpha','shares':4}]
    assert ledger['open_orders']==[]
    assert ledger['strategies']['alpha']['2330']==2


def test_unknown_order_cannot_be_forced_into_retry():
    from order_desk.execution import retry_terminal_orders
    o=_open_buy(taipei_today());o['status']='Unconfirmed'
    ledger={'strategies':{},'open_orders':[o],'execution':{'submitted':[]}}
    original=copy.deepcopy(ledger)
    with pytest.raises(SignalError):retry_terminal_orders(ledger)
    assert ledger==original


def test_identity_mismatch_does_not_change_holdings():
    ledger={'strategies':{},'open_orders':[_open_buy(taipei_today())]}
    original=copy.deepcopy(ledger)
    with pytest.raises(SignalError,match='identity mismatch'):
        refresh_open_orders(_Rows([{'broker_order_id':'abc','ticker':'2317','status':'Filled','filled_shares':6}]),ledger)
    assert ledger==original


def test_immediate_fill_retained_when_status_disconnects():
    class Worker:
        def call(self,method,*args):
            if method=='place_order':return type('Result',(),{'broker_order_id':'abc','status':'Filled','filled_shares':6})()
            if method=='reconcile_orders':raise BrokerError('disconnected')
            raise AssertionError(method)
    ledger={'strategies':{},'open_orders':[]}
    assert submit(Worker(),ledger,[_intent('2330','Buy',6,100)],{'2330-Buy-6':[{'strategy':'a','shares':6}]})=='buys_working'
    assert ledger['strategies']=={'a':{'2330':6}}
    assert ledger['open_orders'][0]['applied_shares']==6


def test_submitted_buys_survive_reconciliation_replacing_ledger(tmp_path):
    fake=FakeBroker(positions={'2330':10},cash=0,auto_fill=False);w=BrokerWorker(fake)
    ledger={'strategies':{'a':{'2330':10}},'open_orders':[]}
    orders=[_intent('2330','Sell',10,100),_intent('2317','Buy',10,100)]
    alloc={o.client_order_id:[{'strategy':'a','shares':10}] for o in orders}
    try:
        submit(w,ledger,orders,alloc)
        fake.fill_order(next(iter(fake.orders)))
        refresh_open_orders(w,ledger)
        assert submit(w,ledger,[],{})=='buys_working'
        assert orders[1].client_order_id in ledger['execution']['submitted']
        fake.fill_order(list(fake.orders)[1])
        refresh_open_orders(w,ledger)
        assert submit(w,ledger,[],{})=='completed'
        assert len(fake.orders)==2
        assert ledger['strategies']=={'a':{'2317':10}}
    finally:w.close()


def test_cli_audit_is_read_only_even_with_open_orders(tmp_path,monkeypatch):
    import order_desk.__main__ as cli
    ledger={'strategies':{'alpha':{'2330':6}},'open_orders':[_open_buy(taipei_today())]}
    paths={};save_account_ledger(tmp_path,ledger,paths)
    db=tmp_path/'logs/ledger.sqlite3';before=db.read_bytes()
    (tmp_path/'config.json').write_text(json.dumps({'mode':'simulation','strategies':[{'name':'alpha'}]}))
    class Worker:
        def call(self,method,*args):
            if method=='positions':return [BrokerPosition('2330',6)]
            if method=='reconcile_orders':return [{'broker_order_id':'abc','ticker':'2330','status':'Filled','filled_shares':6}]
            raise AssertionError('audit must not submit or fetch prices')
        def close(self):pass
    monkeypatch.setattr(cli,'_broker',lambda mode:Worker())
    assert cli.main(['--config',str(tmp_path/'config.json'),'--audit'])==0
    assert db.read_bytes()==before
    assert load_account_ledger(tmp_path)[0]==ledger


def test_cli_send_blocks_unattributed_active_order(tmp_path,monkeypatch):
    import order_desk.__main__ as cli
    from test_capital import _write_signal,_quote
    from datetime import datetime,timezone
    _write_signal(tmp_path/'strategies/alpha/signal.json','alpha',[{'ticker':'2330','weight':1}])
    signal_path=tmp_path/'strategies/alpha/signal.json'
    data=json.loads(signal_path.read_text());data['effective_at']='2000-01-01T00:00:00+00:00';data['expires_at']='2099-01-01T00:00:00+00:00';signal_path.write_text(json.dumps(data))
    (tmp_path/'config.json').write_text(json.dumps({'mode':'production','lot_mode':'odd','strategies':[{'name':'alpha','signal_path':'strategies/alpha/signal.json','extra_twd':600}]}))
    class Worker:
        def call(self,method,*args):
            if method=='positions':return []
            if method=='quotes':return {'2330':_quote('2330')}
            if method=='reconcile_orders':return [{'broker_order_id':'external','ticker':'2330','status':'Submitted'}]
            raise AssertionError('no order can be submitted')
        def close(self):pass
    monkeypatch.setattr(cli,'_broker',lambda mode:Worker())
    assert cli.main(['--config',str(tmp_path/'config.json'),'--send','--confirm','APPROVE'])==1
    assert load_account_ledger(tmp_path)[0]['strategies']=={}
    assert json.loads((tmp_path/'logs/account_audit.json').read_text())['blockers']


def test_audit_history_keeps_unattributed_trades_across_days(tmp_path):
    from order_desk.audit import record_audit
    first={'checked_at':'2026-11-09T04:00:00Z','orders':[{'ticker':'1815','source':'unattributed'}]}
    record_audit(tmp_path,first)
    record_audit(tmp_path,{'checked_at':'2026-11-10T04:00:00Z','orders':[]})
    with sqlite3.connect(tmp_path/'logs/account_audit.sqlite3') as db:
        rows=db.execute('SELECT payload FROM audits ORDER BY id').fetchall()
    assert len(rows)==2
    assert json.loads(rows[0][0])==first
