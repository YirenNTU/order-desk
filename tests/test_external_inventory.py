import copy
from datetime import datetime,timezone
from pathlib import Path
import sqlite3,json
import pytest
from order_desk.audit import audit_account,audit_with_history,record_audit,require_clear_audit,load_audit_context
from order_desk.models import BrokerPosition
from order_desk.signal import SignalError

DAY=datetime.now(timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Taipei')).date().isoformat()

class Worker:
    def __init__(self,shares,orders=()):self.shares=shares;self.orders=orders
    def call(self,method):
        if method=='positions':return [BrokerPosition('2330',self.shares)]
        if method=='reconcile_orders':return self.orders
        raise AssertionError(method)


def book():return {'strategies':{'alpha':{'2330':20}},'open_orders':[]}


def manual(action,filled,**kwargs):
    return {'broker_order_id':'manual','ticker':'2330','action':action,'order_lot':'IntradayOdd',
            'trading_day':DAY,'status':'Filled','filled_shares':filled,'custom_field':'',**kwargs}


def anchor(ledger=None):return audit_account(Worker(30),ledger or book())['inventory_baseline']


@pytest.mark.parametrize('action,filled,total,external',[('Buy',5,35,15),('Sell',8,22,2)])
def test_manual_trades_only_adjust_external_inventory(action,filled,total,external):
    ledger=book();before=copy.deepcopy(ledger)
    report=audit_account(Worker(total,[manual(action,filled)]),ledger,baseline=anchor())
    assert not report['blockers']
    row=report['positions'][0]
    assert row['external_shares']==external and row['strategy_shares_after_external']==20
    assert ledger==before


def test_manual_sell_beyond_external_inventory_cannot_rewrite_strategy():
    ledger=book();before=copy.deepcopy(ledger)
    r=audit_account(Worker(18,[manual('Sell',12)]),ledger,baseline=anchor())
    assert any('manual sells exceed external holdings' in b for b in r['blockers'])
    with pytest.raises(SignalError):require_clear_audit(r)
    assert ledger==before


def test_program_fills_are_not_counted_as_manual_changes():
    ledger=book();baseline=anchor(ledger)
    row=manual('Buy',5,broker_order_id='program')
    ledger['strategies']['alpha']['2330']=25
    ledger['order_history']=[{**row,'trading_day':DAY}]
    r=audit_account(Worker(35,[row]),ledger,baseline=baseline)
    assert not r['blockers'] and r['positions'][0]['external_shares']==10
    assert r['orders'][0]['source']=='program'


def test_partial_fills_and_restart_do_not_double_count(tmp_path):
    ledger=book();initial=audit_account(Worker(30),ledger);record_audit(tmp_path,initial)
    for filled in (2,2,5,5):
        r=audit_with_history(Worker(30+filled,[manual('Buy',filled,status='PartFilled')]),ledger,tmp_path)
        assert not r['blockers']
        assert r['positions'][0]['external_shares']==10+filled
        record_audit(tmp_path,r)
    # Broker no longer lists the order. Durable observation must keep the five shares.
    r=audit_with_history(Worker(35),ledger,tmp_path)
    assert not r['blockers'] and r['positions'][0]['external_shares']==15
    assert len(r['orders'])==1


def test_cancelled_manual_order_keeps_its_partial_fill():
    r=audit_account(Worker(33,[manual('Buy',3,status='Cancelled')]),book(),baseline=anchor())
    assert not r['blockers'] and r['positions'][0]['external_shares']==13


def test_common_lot_manual_fill_converts_lots_to_shares():
    row=manual('Buy',0,order_lot='Common',filled_quantity=1);row.pop('filled_shares')
    r=audit_account(Worker(1030,[row]),book(),baseline=anchor())
    assert not r['blockers'] and r['positions'][0]['external_shares']==1010


def test_initial_observed_manual_fill_is_already_in_baseline():
    row=manual('Buy',10)
    first=audit_account(Worker(30,[row]),book())
    second=audit_account(Worker(30,[row]),book(),baseline=first['inventory_baseline'])
    assert not second['blockers'] and second['positions'][0]['external_shares']==10


def test_unexplained_inventory_change_is_not_absorbed_into_external():
    r=audit_account(Worker(35),book(),baseline=anchor())
    assert r['positions'][0]['external_shares']==10
    assert r['positions'][0]['difference_shares']==5
    with pytest.raises(SignalError):require_clear_audit(r)


@pytest.mark.parametrize('reserved,blocked',[(8,False),(12,True)])
def test_pending_manual_sell_only_reserves_external_inventory(reserved,blocked):
    row=manual('Sell',0,status='Submitted',quantity=reserved)
    r=audit_account(Worker(30,[row]),book(),baseline=anchor())
    assert bool(r['blockers'])==blocked


def test_multiple_pending_manual_sells_cannot_share_the_same_external_shares():
    rows=[manual('Sell',0,status='Submitted',quantity=6,broker_order_id=str(i)) for i in (1,2)]
    r=audit_account(Worker(30,rows),book(),baseline=anchor())
    assert any('pending manual sells exceed' in b for b in r['blockers'])


def test_unrecognized_program_tag_is_not_assumed_manual():
    r=audit_account(Worker(35,[manual('Buy',5,custom_field='abc123')]),book(),baseline=anchor())
    assert any('unrecognized program-tagged' in b for b in r['blockers'])


def test_unrelated_manual_order_does_not_block_strategy():
    row=manual('Sell',5,ticker='9999')
    r=audit_account(Worker(30,[row]),book(),baseline=anchor())
    assert not r['blockers']


def test_old_audit_database_migration_does_not_require_manual_review(tmp_path):
    path=tmp_path/'logs/account_audit.sqlite3';path.parent.mkdir()
    initial=audit_account(Worker(30,[manual('Buy',10)]),book())
    initial.pop('inventory_baseline')
    with sqlite3.connect(path) as db:
        db.execute('create table audits (id integer primary key, checked_at text, payload text)')
        db.execute('insert into audits(checked_at,payload) values(?,?)',(initial['checked_at'],json.dumps(initial)))
    r=audit_with_history(Worker(30,[manual('Buy',10)]),book(),tmp_path)
    assert not r['blockers'] and r['positions'][0]['external_shares']==10
    record_audit(tmp_path,r)
    baseline,observed=load_audit_context(tmp_path)
    assert baseline['external_shares']['2330']==10 and len(observed)==1


def test_backward_fill_report_cannot_erase_observed_manual_trade(tmp_path):
    first=audit_account(Worker(30),book());record_audit(tmp_path,first)
    filled=audit_with_history(Worker(35,[manual('Buy',5)]),book(),tmp_path);record_audit(tmp_path,filled)
    r=audit_with_history(Worker(35,[manual('Buy',2)]),book(),tmp_path)
    assert any('moved backward' in x for x in r['blockers'])
    record_audit(tmp_path,r)
    _,observed=load_audit_context(tmp_path)
    assert observed[0]['filled_shares']==5


def test_missing_same_day_manual_sell_does_not_release_reserved_external_shares(tmp_path):
    first=audit_account(Worker(30),book());record_audit(tmp_path,first)
    r=audit_with_history(Worker(30,[manual('Sell',0,status='Submitted',quantity=12)]),book(),tmp_path)
    record_audit(tmp_path,r)
    later=audit_with_history(Worker(30),book(),tmp_path)
    assert any('pending manual sells exceed' in x for x in later['blockers'])


def test_cli_preserves_personal_shares_and_sends_only_strategy_delta(tmp_path,monkeypatch):
    import order_desk.__main__ as cli
    from order_desk.broker import FakeBroker,BrokerWorker
    from order_desk.capital import load_account_ledger
    from test_capital import _write_signal,_quote
    _write_signal(tmp_path/'strategies/alpha/signal.json','alpha',[{'ticker':'2330','weight':1}])
    p=tmp_path/'strategies/alpha/signal.json';signal=json.loads(p.read_text())
    signal.update(effective_at='2000-01-01T00:00:00+00:00',expires_at='2099-01-01T00:00:00+00:00');p.write_text(json.dumps(signal))
    config={'mode':'production','lot_mode':'odd','strategies':[{'name':'alpha','signal_path':'strategies/alpha/signal.json','extra_twd':600}]}
    (tmp_path/'config.json').write_text(json.dumps(config))
    class Broker(FakeBroker):
        def reconcile_orders(self):return [dict(manual('Buy',10), limit_price=100)]+super().reconcile_orders()
    fake=Broker(positions={'2330':10},cash=10000,auto_fill=True);fake.set_quotes({'2330':_quote('2330')})
    monkeypatch.setattr(cli,'_broker',lambda mode:BrokerWorker(fake))
    assert cli.main(['--config',str(tmp_path/'config.json'),'--send','--confirm','APPROVE'])==0
    ledger,_=load_account_ledger(tmp_path)
    assert ledger['strategies']=={'alpha':{'2330':6}}
    assert sum(o['shares'] for o in fake.orders.values())==6
    r=audit_with_history(Worker(16,fake.reconcile_orders()),ledger,tmp_path)
    assert not r['blockers'] and r['positions'][0]['external_shares']==10
