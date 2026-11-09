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


@pytest.mark.parametrize('amount,available',[(-500,500),(500,1500)])
def test_unsettled_amounts_respect_receivable_and_payable_sign(amount,available):
 data=snapshot(1000);data['settlements'][1]['amount']=amount
 report=assess_cash(data,ledger(),[buy()],now=NOW)
 assert report['available_twd']==available


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


def test_receivable_and_pending_buys_are_not_combined_as_two_payables():
 data=snapshot(180000)
 data['settlements'][1]['amount']=-130000
 data['settlements'][2]['amount']=100000
 data['orders']=[cash_order('pending',shares=300)]
 report=assess_cash(data,ledger(),[],now=NOW)
 assert report['available_twd']==19957
 assert report['filled_net_payable_hold_twd']==0
 assert report['unfilled_buy_hold_twd']==30043


def test_filled_buy_and_pending_buy_are_distinct_from_same_day_payable():
 data=snapshot(2000)
 data['settlements'][2]['amount']=-620
 data['orders']=[cash_order('filled','Filled'),cash_order('pending')]
 report=assess_cash(data,ledger(),[],now=NOW)
 assert report['today_commitment_hold_twd']==1240
 assert report['available_twd']==760


def test_confirmed_sale_can_fund_new_same_settlement_buys_but_is_not_bank_cash():
 data=snapshot(2000)
 sell=dict(cash_order('sale','Filled',10),action='Sell')
 data['orders']=[cash_order('filled','Filled'),cash_order('pending'),sell]
 data['settlements'][2]['amount']=357
 report=assess_cash(data,ledger(),[],now=NOW)
 assert report['filled_net_payable_hold_twd']==0
 assert report['bank_only_available_twd']==1380
 assert report['available_twd']==1737
 assert report['bank_balance_twd']==2000
 assert report['today_confirmed_receivable_credit_twd']==357


def test_pending_sell_never_funds_a_buy():
 data=snapshot(1000)
 data['orders']=[dict(cash_order('sale',shares=100),action='Sell'),cash_order('buy','Filled')]
 assert assess_cash(data,ledger(),[],now=NOW)['available_twd']==380


def test_partial_fill_reserves_filled_and_remaining_once_with_one_fee():
 data=snapshot(2000);row=cash_order(shares=10);row['filled_shares']=2
 data['orders']=[row];data['settlements'][2]['amount']=-220
 report=assess_cash(data,ledger(),[],now=NOW)
 assert report['filled_net_payable_hold_twd']==220
 assert report['unfilled_buy_hold_twd']==800
 assert report['today_commitment_hold_twd']==1020


@pytest.mark.parametrize('remote_status,expected',[('Cancelled',1000),('Filled',380)])
def test_authoritative_remote_terminal_releases_stale_local_open_hold(remote_status,expected):
 data=snapshot(1000);data['orders']=[cash_order('same',remote_status)]
 l=ledger();l['open_orders']=[dict(cash_order('same'),shares=6,applied_shares=0)]
 assert assess_cash(data,l,[],now=NOW)['available_twd']==expected


def test_lagging_settlement_still_reserves_confirmed_buy():
 data=snapshot(1000);data['settlements'][2]['amount']=300
 data['orders']=[cash_order('buy','Filled')]
 assert assess_cash(data,ledger(),[],now=NOW)['available_twd']==380


def test_market_filled_order_uses_confirmed_deal_price():
 data=snapshot(1000);row=cash_order('buy','Filled');row['limit_price']=0
 row['status_detail']={'deals':[{'price':90,'quantity':6}]};data['orders']=[row]
 assert assess_cash(data,ledger(),[],now=NOW)['available_twd']==440


def test_missing_fill_detail_cannot_value_market_order():
 data=snapshot();row=cash_order('buy','Filled');row['limit_price']=0;data['orders']=[row]
 with pytest.raises(SignalError):assess_cash(data,ledger(),[],now=NOW)


def test_actual_fill_price_prevents_using_lower_modified_limit_for_past_fills():
 data=snapshot(1000);row=cash_order('buy','Filled');row['limit_price']=50
 row['status_detail']={'deals':[{'price':100,'quantity':6}]};data['orders']=[row]
 assert assess_cash(data,ledger(),[],now=NOW)['available_twd']==380


def test_same_order_local_fill_regression_is_not_silently_ignored():
 data=snapshot();data['orders']=[cash_order()];l=ledger()
 l['open_orders']=[dict(cash_order(),shares=6,applied_shares=2)]
 with pytest.raises(SignalError,match='backwards'):assess_cash(data,l,[],now=NOW)


def test_later_receipt_cannot_pay_earlier_obligation():
 data=snapshot(1000);data['settlements'][1]['amount']=-700
 data['settlements'][2]['amount']=10000
 assert assess_cash(data,ledger(),[],now=NOW)['available_twd']==300


def test_today_receivable_is_neither_reserved_nor_added_to_balance():
 data=snapshot(1000);data['settlements'][0]['amount']=10000
 assert assess_cash(data,ledger(),[],now=NOW)['available_twd']==1000


def test_today_payable_with_unknown_bank_posting_is_explicit():
 data=snapshot();data['settlements'][0]['amount']=-200
 r=assess_cash(data,ledger(),[],now=NOW)
 assert r['available_twd']==800 and r['today_settlement_status_unverified']


def test_partial_quantity_reduction_releases_only_cancelled_unfilled_shares():
 data=snapshot(2000);row=cash_order(shares=10)
 row.update(filled_shares=2,cancelled_quantity=3);data['orders']=[row]
 r=assess_cash(data,ledger(),[],now=NOW)
 assert r['filled_net_payable_hold_twd']==220
 assert r['unfilled_buy_hold_twd']==500
 assert r['available_twd']==1280


def test_missing_sale_valuation_is_not_credited_as_cash():
 data=snapshot();sell=dict(cash_order('sale','Filled'),action='Sell')
 del sell['limit_price'];data['orders']=[sell]
 r=assess_cash(data,ledger(),[],now=NOW)
 assert r['estimated_confirmed_sale_proceeds_twd']==0
 assert r['available_twd']==1000


def test_common_local_fill_and_cancel_units_are_normalized_once():
 l=ledger();l['open_orders']=[dict(cash_order('local',shares=10),order_lot='Common',
  shares=10000,applied_shares=2000,broker_snapshot={'cancelled_quantity':3,
   'status_detail':{'deals':[{'price':90,'quantity':2}]}})]
 r=assess_cash(snapshot(1000000),l,[],now=NOW)
 assert r['order_commitments'][0]['remaining_shares']==5000
 assert r['order_commitments'][0]['filled_cost_twd']==buy_cost(180000)
 assert r['today_commitment_hold_twd']==buy_cost(680000)


def test_broker_snapshot_uses_latest_modified_price_and_cancelled_quantity(monkeypatch):
 from test_taiwan_review import adapter,trade,NS
 b=adapter(monkeypatch);t=trade(b.account)
 t.status.modified_price=105;t.status.cancel_quantity=2
 b.api=NS(update_status=lambda a:None,list_trades=lambda:[t])
 row=b.reconcile_orders()[0]
 assert row['limit_price']==105 and row['cancelled_quantity']==2


def test_confirmed_sale_can_fund_new_buy_when_bank_cash_is_zero():
 data=snapshot(0);data['orders']=[dict(cash_order('sale','Filled',10),action='Sell')]
 data['settlements'][2]['amount']=980
 r=assess_cash(data,ledger(),[buy()],now=NOW)
 assert r['status']=='allowed'
 assert r['available_twd']==977  # 1000 minus commission buffer 20 and tax 3.
 assert r['bank_only_available_twd']==0 and r['bank_balance_twd']==0


def test_receipt_is_not_credited_twice_after_new_buy_fills():
 data=snapshot(0);data['orders']=[dict(cash_order('sale','Filled',10),action='Sell'),cash_order('buy','Filled')]
 data['settlements'][2]['amount']=380
 r=assess_cash(data,ledger(),[buy()],now=NOW)
 assert r['available_twd']==357 and r['status']=='insufficient'


def test_receipt_reconciliation_uses_smaller_of_broker_and_confirmed_fills():
 data=snapshot(0);data['orders']=[dict(cash_order('sale','Filled',10),action='Sell')]
 data['settlements'][2]['amount']=500
 assert assess_cash(data,ledger(),[],now=NOW)['available_twd']==500
 data['settlements'][2]['amount']=2000
 assert assess_cash(data,ledger(),[],now=NOW)['available_twd']==977


def test_future_sale_receipt_does_not_cover_an_earlier_payment_deficit():
 data=snapshot(100);data['settlements'][1]['amount']=-200
 data['settlements'][2]['amount']=1000
 data['orders']=[dict(cash_order('sale','Filled',20),action='Sell')]
 r=assess_cash(data,ledger(),[buy()],now=NOW)
 assert r['status']=='insufficient' and r['available_twd']==0
 assert r['earlier_payment_shortfall_twd']==100


def test_previous_days_confirmed_receipt_can_fund_later_new_buy():
 data=snapshot(100);data['settlements'][1]['amount']=700
 r=assess_cash(data,ledger(),[buy()],now=NOW)
 assert r['status']=='allowed' and r['available_twd']==800
 assert r['bank_balance_twd']==100


def test_next_day_old_fills_are_not_added_again_to_rolled_settlement():
 data=snapshot(0);data['settlements'][1]['amount']=700
 old=dict(cash_order('sale','Filled',100),action='Sell',trading_day='2026-11-06')
 data['orders']=[old];l=ledger();l['order_history']=[dict(old,shares=100,applied_shares=100)]
 r=assess_cash(data,l,[buy()],now=NOW)
 assert r['available_twd']==700 and r['confirmed_receivable_credit_twd']==700


def test_unfilled_buy_against_receipt_reserved_before_new_buy():
 data=snapshot(0);data['settlements'][2]['amount']=980
 data['orders']=[dict(cash_order('sale','Filled',10),action='Sell'),cash_order('pending')]
 r=assess_cash(data,ledger(),[buy()],now=NOW)
 assert r['available_twd']==357 and r['status']=='insufficient'
