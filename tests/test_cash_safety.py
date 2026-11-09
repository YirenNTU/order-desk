import copy
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import pytest
from order_desk.cash import assess_cash,buy_cost,check_cash
from order_desk.signal import SignalError
from order_desk.broker import FakeBroker,BrokerWorker,BrokerError
from order_desk.execution import submit_sells_then_buys,taipei_today
from test_capital import _intent

NOW=datetime(2026,11,9,10,tzinfo=ZoneInfo('Asia/Taipei'))


def snapshot(balance=1000):
 return {'mode':'production','balance':{'acc_balance':balance,'date':NOW.isoformat(),'errmsg':''},
         'settlements':[{'T':0,'date':'2026-11-09','amount':0},{'T':1,'date':'2026-11-10','amount':0},{'T':2,'date':'2026-11-11','amount':0}],
         'orders':[]}


def ledger():return {'strategies':{},'open_orders':[]}


def buy(shares=6):return _intent('2330','Buy',shares,100)


def test_balance_must_cover_limit_notional_plus_fee_and_reserve():
 assert buy_cost(600)==620
 report=assess_cash(snapshot(700),ledger(),[buy()],reserve=100,now=NOW)
 assert report['status']=='insufficient' and report['shortfall_twd']==20
 assert assess_cash(snapshot(720),ledger(),[buy()],reserve=100,now=NOW)['status']=='allowed'


@pytest.mark.parametrize('value',[-1,float('nan'),float('inf'),True,None])
def test_bad_balance_cannot_pass_safety(value):
 with pytest.raises(SignalError):assess_cash(snapshot(value),ledger(),[buy()],now=NOW)


def test_old_balance_or_broker_error_cannot_pass():
 data=snapshot();data['balance']['date']=(NOW-timedelta(minutes=3)).isoformat()
 with pytest.raises(SignalError,match='stale'):assess_cash(data,ledger(),[buy()],now=NOW)
 data=snapshot();data['balance']['errmsg']='unsupported bank'
 with pytest.raises(SignalError):assess_cash(data,ledger(),[buy()],now=NOW)


@pytest.mark.parametrize('amount',[-500,500])
def test_unsettled_amounts_reserved_without_crediting_future_sales(amount):
 data=snapshot(1000);data['settlements'][1]['amount']=amount
 report=assess_cash(data,ledger(),[buy()],now=NOW)
 assert report['available_twd']==500 and report['status']=='insufficient'


def cash_order(key='manual',status='Submitted',shares=6):
 return {'broker_order_id':key,'ticker':'2330','action':'Buy','order_lot':'IntradayOdd',
         'quantity':shares,'limit_price':100,'filled_shares':shares if status=='Filled' else 0,
         'status':status,'trading_day':'2026-11-09'}


def test_manual_buys_reserve_account_cash():
 data=snapshot(1000);data['orders']=[cash_order()]
 report=assess_cash(data,ledger(),[buy()],now=NOW)
 assert report['today_commitment_hold_twd']==620 and report['status']=='insufficient'


def test_recent_local_order_is_reserved_when_broker_query_lags_and_not_counted_twice():
 l=ledger();l['open_orders']=[dict(cash_order('local'),shares=6,applied_shares=0)]
 data=snapshot(1300)
 first=assess_cash(data,l,[buy()],now=NOW)
 data['orders']=[cash_order('local')]
 second=assess_cash(data,l,[buy()],now=NOW)
 assert first['available_twd']==second['available_twd']==680
 assert first['status']==second['status']=='allowed'


def test_settlement_total_and_same_day_buys_use_larger_reserve_not_sum():
 data=snapshot(2000);data['orders']=[cash_order(status='Filled')];data['settlements'][2]['amount']=-620
 report=assess_cash(data,ledger(),[buy()],now=NOW)
 assert report['today_commitment_hold_twd']==620


def test_terminal_unfilled_manual_order_does_not_consume_cash():
 data=snapshot();data['orders']=[cash_order(status='Cancelled')]
 assert assess_cash(data,ledger(),[buy()],now=NOW)['available_twd']==1000


def test_complete_settlement_periods_required_in_production():
 data=snapshot();data['settlements']=[]
 with pytest.raises(SignalError):assess_cash(data,ledger(),[buy()],now=NOW)


def test_failure_does_not_block_sell_leg_or_send_buy():
 fake=FakeBroker(positions={'2330':10},cash=10000)
 fake.cash_snapshot=lambda:(_ for _ in ()).throw(BrokerError('offline'))
 worker=BrokerWorker(fake);l={'strategies':{'a':{'2330':10}},'open_orders':[]}
 sell=_intent('2330','Sell',10,100);purchase=_intent('2317','Buy',6,100)
 try:
  status=submit_sells_then_buys(worker,[sell,purchase],l,{sell.client_order_id:[{'strategy':'a','shares':10}],purchase.client_order_id:[{'strategy':'a','shares':6}]},cash_reserve_twd=0,wait_seconds=0,poll_seconds=0,cash_check=True)
 finally:worker.close()
 assert status=='buys_waiting_for_cash'
 assert [o['action'] for o in fake.orders.values()]==['Sell']
 assert l['cash_safety']['status']=='unavailable'
 assert 'execution' in l


def test_second_buy_checks_again_and_resume_does_not_resend_first():
 fake=FakeBroker(cash=1500);original=fake.cash_snapshot
 def changing():
  result=original();result['balance']['acc_balance']=700 if fake.orders else 1500;return result
 fake.cash_snapshot=changing
 worker=BrokerWorker(fake);l=ledger();a=buy();b=_intent('2317','Buy',6,100)
 allocation={o.client_order_id:[{'strategy':'a','shares':6}] for o in (a,b)}
 try:
  status=submit_sells_then_buys(worker,[a,b],l,allocation,cash_reserve_twd=0,wait_seconds=0,poll_seconds=0,cash_check=True)
  assert status=='buys_waiting_for_cash' and len(fake.orders)==1
  from order_desk.execution import refresh_open_orders
  refresh_open_orders(worker,l)
  fake.cash_snapshot=lambda:dict(original(),balance={'acc_balance':2000,'date':datetime.now(ZoneInfo('Asia/Taipei')).isoformat(),'errmsg':''})
  status=submit_sells_then_buys(worker,[],l,{},cash_reserve_twd=0,wait_seconds=0,poll_seconds=0,cash_check=True)
  assert status=='completed' and len(fake.orders)==2
  assert l['strategies']['a']=={'2330':6,'2317':6}
 finally:worker.close()


def test_production_ignores_legacy_false_cash_flag():
 from order_desk.__main__ import _policy
 assert _policy({'mode':'production','cash_check':False},True).cash_check is True


def test_preview_shortage_does_not_prevent_sell_plan(tmp_path,monkeypatch):
 from test_inventory_isolation import fixture
 cli,fake,args=fixture(tmp_path,monkeypatch,initial_short=False)
 fake._bank_cash=0
 assert cli.main(args)==1
 assert [o['action'] for o in fake.orders.values()]==['Sell']
 from order_desk.capital import load_account_ledger
 saved,_=load_account_ledger(tmp_path)
 assert saved['cash_safety']['status']=='insufficient'
 assert 'execution' in saved


def test_incomplete_balance_fetch_and_reversed_settlement_dates_fail():
 data=snapshot();data['balance']['fetch_status']='Fetching'
 with pytest.raises(SignalError):assess_cash(data,ledger(),[buy()],now=NOW)
 data=snapshot();data['settlements'][1]['date'],data['settlements'][2]['date']='2026-11-11','2026-11-10'
 with pytest.raises(SignalError):assess_cash(data,ledger(),[buy()],now=NOW)


def test_partial_cancel_reserves_only_confirmed_filled_shares():
 data=snapshot(1000);row=cash_order(status='Cancelled');row['filled_shares']=2;data['orders']=[row]
 report=assess_cash(data,ledger(),[buy()],now=NOW)
 assert report['today_commitment_hold_twd']==220
 assert report['status']=='allowed'
