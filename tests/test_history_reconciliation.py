import copy
from datetime import datetime,timedelta
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo
import pytest
from order_desk.history import accounting_sources,resolve_history
from order_desk.execution import refresh_open_orders
from order_desk.broker import BrokerWorker,FakeBroker,BrokerError
from order_desk.signal import SignalError

NOW=datetime.now(ZoneInfo('Asia/Taipei'))
TODAY=NOW.date().isoformat()
DAY=(NOW.date()-timedelta(days=1)).isoformat()
OLD=(NOW.date()-timedelta(days=5)).isoformat()
BIND={'mode':'production','account':'test-hash'}


def lot(code='2330',day=DAY,seq='number',qty=0,unit='CommonTruncated'):
 return {'ticker':code,'date':day,'dseq':seq,'quantity':qty,'unit':unit}


def source(code='2330',day=DAY,seq='number',qty=2):
 return {'ticker':code,'date':day,'dseq':seq,'shares':qty}


def sale(code='2330',day=TODAY,seq='sale',qty=2,buys=None):
 return {'ticker':code,'date':day,'dseq':seq,'shares':qty,'buy_sources':buys if buys is not None else [source(qty=qty)]}


def order(seq='number',applied=0,shares=6):
 return {'broker_order_id':'oid','ticker':'2330','action':'Buy','shares':shares,
  'order_lot':'IntradayOdd','trading_day':DAY,'status':'Unconfirmed','applied_shares':applied,
  'client_order_id':'intent','custom_field':'abc123','limit_price':100,
  'allocation':[{'strategy':'a','shares':shares,'applied':applied}],
  'broker_snapshot':{'broker_order_id':'oid','ticker':'2330','action':'Buy','order_lot':'IntradayOdd',
   'trading_day':DAY,'status':'Submitted','broker_order_number':seq,'order_type':'ROD'}}


def setup(seq='number',applied=0):
 o=order(seq,applied)
 l={'account_binding':BIND.copy(),'strategies':{'a':{'2330':applied}},'open_orders':[o],
    'order_history':[{'broker_order_id':'control','ticker':'control','action':'Buy','trading_day':DAY,'applied_shares':1}]}
 s={'account_binding':BIND.copy(),'begin_date':DAY,'end_date':TODAY,'fetched_at':datetime.now(ZoneInfo('Asia/Taipei')).isoformat(),
    'complete':True,'positions_stable':True,'positions':[{'ticker':'control','shares':1,'lots':[lot('control',seq='control')]}],'sales':[]}
 return s,l,o


def resolve(s,l,o):return resolve_history(s,l,[o],today=TODAY)


def test_zero_fill_requires_complete_fresh_accounting_and_records_expiry():
 s,l,o=setup();s['positions'].append({'ticker':'2330','shares':20,'lots':[lot(day=OLD,seq='old')]})
 rows,alerts,key=resolve(s,l,o)
 assert not alerts and len(key)==64
 assert rows[0]['filled_shares']==0 and rows[0]['remaining_shares']==6 and rows[0]['status']=='Expired'


def test_legacy_zero_fill_does_not_require_missing_broker_number():
 s,l,o=setup(seq='');del o['broker_snapshot']['order_type']
 assert resolve(s,l,o)[0][0]['filled_shares']==0


def test_zero_whole_lot_value_does_not_mean_zero_shares():
 s,l,o=setup();s['positions'].append({'ticker':'2330','shares':2,'lots':[lot()]})
 row=resolve(s,l,o)[0][0]
 assert row['filled_shares']==2 and row['remaining_shares']==4


def test_ambiguous_odd_lot_distribution_is_not_guessed():
 s,l,o=setup();s['positions'].append({'ticker':'2330','shares':20,'lots':[lot(),lot(day=OLD,seq='old')]})
 rows,alerts,_=resolve(s,l,o)
 assert rows==[] and 'ambiguous odd-lot' in alerts[0]['reason']


def test_buy_then_sell_is_counted_using_realized_source_not_just_current_holdings():
 s,l,o=setup();s['sales']=[sale(qty=3)]
 assert resolve(s,l,o)[0][0]['filled_shares']==3


def test_remaining_lot_and_realized_source_sum_without_double_counting():
 s,l,o=setup();s['sales']=[sale(qty=2)]
 s['positions'].append({'ticker':'2330','shares':3,'lots':[lot()]})
 assert resolve(s,l,o)[0][0]['filled_shares']==5


def test_full_fill_is_not_retried():
 s,l,o=setup();s['sales']=[sale(qty=6)]
 assert resolve(s,l,o)[0][0]['status']=='Filled'


def test_manual_buy_is_not_attributed_to_program_order():
 s,l,o=setup();s['positions'].append({'ticker':'2330','shares':2,'lots':[lot(seq='manual')]})
 assert resolve(s,l,o)[0][0]['filled_shares']==0


def test_legacy_identity_cannot_claim_same_day_manual_or_program_fills():
 s,l,o=setup(seq='');s['positions'].append({'ticker':'2330','shares':2,'lots':[lot(seq='manual')]})
 assert not resolve(s,l,o)[0]


@pytest.mark.parametrize('change',['account','complete','end','unstable','stale','missing_lots','missing_sell_sources','duplicate_lot','invalid_unit'])
def test_bad_history_does_not_release_order(change):
 s,l,o=setup()
 if change=='account':s['account_binding']['account']='other'
 elif change=='complete':s['complete']=False
 elif change=='end':s['end_date']=DAY
 elif change=='unstable':s['positions_stable']=False
 elif change=='stale':s['fetched_at']=(NOW-timedelta(minutes=5)).isoformat()
 elif change=='missing_lots':s['positions'][0]['lots']=[]
 elif change=='missing_sell_sources':s['sales']=[sale(buys=[])]
 elif change=='duplicate_lot':s['positions'][0]['lots']*=2
 elif change=='invalid_unit':s['positions'][0]['lots'][0]['unit']='guess'
 before=copy.deepcopy(l)
 with pytest.raises((SignalError,ValueError)):resolve(s,l,o)
 assert l==before


@pytest.mark.parametrize('change',['control','accepted','date','type','excess','backwards','duplicate_identity'])
def test_unproven_identity_and_conflicting_fills_remain_unconfirmed(change):
 s,l,o=setup()
 if change=='control':s['positions']=[]
 elif change=='accepted':o['broker_snapshot']['status']='PendingSubmit'
 elif change=='date':o['broker_snapshot']['trading_day']=OLD
 elif change=='type':o['broker_snapshot']['order_type']='GTC'
 elif change=='excess':s['sales']=[sale(qty=7)]
 elif change=='backwards':o['applied_shares']=1
 elif change=='duplicate_identity':l['open_orders'].append(copy.deepcopy(o))
 assert resolve(s,l,o)[0]==[]


def test_empty_unverified_history_is_not_zero_fill_proof():
 s,l,o=setup();l['order_history']=[];s['positions']=[]
 assert not resolve(s,l,o)[0]


def test_exact_sell_match_uses_share_units():
 s,l,o=setup();o['action']=o['broker_snapshot']['action']='Sell'
 s['sales']=[sale(day=DAY,seq='number',qty=3,buys=[source(day=OLD,seq='old',qty=3)])]
 assert resolve(s,l,o)[0][0]['filled_shares']==3


class HistoryBroker(FakeBroker):
 def __init__(self,s):super().__init__();self.snapshot=s;self.history_calls=0
 def historical_accounting(self,*args):self.history_calls+=1;return copy.deepcopy(self.snapshot)


def test_partial_recovery_applies_only_unrecorded_delta_once_and_keeps_evidence():
 s,l,o=setup(applied=1);s['positions'].append({'ticker':'2330','shares':3,'lots':[lot()]})
 b=HistoryBroker(s);w=BrokerWorker(b)
 try:
  assert refresh_open_orders(w,l,today=TODAY)
  assert l['strategies']['a']['2330']==3 and l['open_orders']==[]
  assert l['order_history'][-1]['historical_reconciliation']['remaining_shares']==3
  assert l['order_history'][-1]['original_broker_snapshot']['status']=='Submitted'
  assert len(l['historical_accounting_evidence'])==1
  assert refresh_open_orders(w,l,today=TODAY)
  assert l['strategies']['a']['2330']==3 and b.history_calls==1
  assert len(l['order_history'])==2
 finally:w.close()


def test_historical_query_failure_retains_order_and_specific_reason():
 s,l,o=setup();b=HistoryBroker(s)
 b.historical_accounting=lambda *args:(_ for _ in ()).throw(BrokerError('history offline'))
 w=BrokerWorker(b)
 try:assert not refresh_open_orders(w,l,today=TODAY)
 finally:w.close()
 assert l['open_orders'][0]['status']=='Unconfirmed'
 assert 'offline' in l['historical_reconciliation_alerts'][0]['reason']
 assert l['strategies']['a']['2330']==0


def test_reused_today_trade_id_cannot_resolve_yesterdays_order():
 s,l,o=setup();b=HistoryBroker(s)
 b.reconcile_orders=lambda:[{'broker_order_id':'oid','trading_day':TODAY,'filled_shares':6,'status':'Filled'}]
 w=BrokerWorker(b)
 try:assert refresh_open_orders(w,l,today=TODAY)
 finally:w.close()
 assert l['order_history'][-1]['applied_shares']==0
 assert l['order_history'][-1]['status']=='Expired'


def test_cli_historical_partial_recovery_replans_remainder_and_does_not_repeat(tmp_path,monkeypatch):
 import json
 from order_desk.capital import load_account_ledger,save_account_ledger
 from order_desk.books import load_strategy_entries,signal_signature
 from order_desk.deferrals import cycle_id
 from test_inventory_isolation import fixture
 cli,fake,args=fixture(tmp_path,monkeypatch,initial_short=False)
 monkeypatch.setattr(cli, '_refuse_weekly_order', lambda *a: 0)
 args = ['--config', str(tmp_path/'config.json'), '--weekly-order']
 config=json.loads((tmp_path/'config.json').read_text());config['inventory_mismatch_policy']='reconcile_actual'
 (tmp_path/'config.json').write_text(json.dumps(config))
 l,paths=load_account_ledger(tmp_path)
 # Let CLI establish its existing account binding, without a real broker.
 import hashlib,os
 binding={'mode':'production','account':hashlib.sha256(os.getenv('SJ_ACCOUNT_ID','').encode()).hexdigest()}
 l['account_binding']=binding
 o=order(seq='number',applied=1,shares=5);o['ticker']=o['broker_snapshot']['ticker']='2317';o['allocation']=[{'strategy':'alpha','shares':5,'applied':1}]
 l.update(strategies={'alpha':{'6213':10,'2330':5,'2317':1}},open_orders=[o],
  order_history=[{'broker_order_id':'control','ticker':'6213','action':'Buy','trading_day':DAY,'applied_shares':1}],
  execution={'trading_day':DAY,'cycle_id':cycle_id(),'budgets':{'alpha':2000},
   'targets':{'alpha':{'6213':10,'2330':5,'2317':5}},'signal_signature':signal_signature(load_strategy_entries(config,tmp_path)),
   'orders':[],'allocations':{},'submitted':[]})
 s,_,_=setup();s['account_binding']=binding
 s['positions']=[{'ticker':'6213','shares':10,'lots':[lot('6213',seq='control')]},
                 {'ticker':'2317','shares':2,'lots':[lot('2317')]}]
 fake._positions={'6213':10,'2330':5,'2317':2}
 fake.historical_accounting=lambda *a:copy.deepcopy(s)
 save_account_ledger(tmp_path,l,paths)
 assert cli.main(args)==0
 result,_=load_account_ledger(tmp_path)
 assert result['strategies']['alpha']=={'6213':10,'2330':5,'2317':5}
 assert [(r['ticker'],r['action'],r['shares']) for r in fake.orders.values()]==[('2317','Buy',3)]
 monkeypatch.setattr(cli,'_refuse_weekly_order',lambda *a:0)
 assert cli.main(['--config',str(tmp_path/'config.json'),'--weekly-order'])==0
 assert len(fake.orders)==1
 assert len([r for r in result['order_history'] if r['broker_order_id']=='oid'])==1


def test_history_adapter_uses_share_units_and_reads_every_sale_source(monkeypatch):
 from test_taiwan_review import adapter
 import sys
 b=adapter(monkeypatch);sys.modules['shioaji'].Unit=NS(Share='Share');calls=[]
 p=NS(code='2330',quantity=2,id=1,cond='Cash',direction='Buy')
 sl=NS(code='2330',quantity=1,id=2,cond='Cash',date=DAY,dseq='sale')
 def ps(**kw):calls.append(('positions',kw['unit']));return [p]
 def pl(**kw):calls.append(('sales',kw['unit'],kw['begin_date'],kw['end_date']));return [sl]
 def pd(**kw):calls.append(('source',kw['unit']));return [NS(code='2330',date=OLD,dseq='old',quantity=1,cond='Cash')]
 b.api=NS(list_positions=ps,list_profit_loss=pl,list_profit_loss_detail=pd,
  list_position_detail=lambda **kw:[NS(code='2330',date=DAY,dseq='number',quantity=0,cond='Cash',direction='Buy')])
 result=b.historical_accounting(DAY,TODAY,['2330'])
 assert result['complete'] and result['positions_stable']
 assert result['positions'][0]['lots'][0]['quantity']==0
 assert result['positions'][0]['lots'][0]['unit']=='CommonTruncated'
 assert result['sales'][0]['shares']==1
 assert calls==[('positions','Share'),('sales','Share',DAY,TODAY),('source','Share'),('positions','Share')]


def test_read_only_resolution_does_not_mutate_supplied_book():
 s,l,o=setup();before=copy.deepcopy(l)
 resolve(s,l,o)
 assert l==before


def test_sources_from_multiple_later_sales_are_added_for_original_buy():
 s,l,o=setup();s['sales']=[sale(seq='sale1',qty=2),sale(seq='sale2',qty=1)]
 assert resolve(s,l,o)[0][0]['filled_shares']==3


@pytest.mark.parametrize('quantity',[True,-1,1.5,float('nan')])
def test_invalid_historical_quantities_never_reach_recovery(quantity):
 s,l,o=setup();s['sales']=[sale(qty=quantity)]
 with pytest.raises(SignalError):resolve(s,l,o)


def test_duplicate_sale_record_is_not_double_counted():
 s,l,o=setup();s['sales']=[sale(),sale()]
 with pytest.raises(SignalError,match='duplicate'):resolve(s,l,o)


def test_newly_changed_account_holdings_cannot_release_prior_order(monkeypatch):
 from test_taiwan_review import adapter
 b=adapter(monkeypatch);calls=[]
 def ps(**kw):
  calls.append(1);return [NS(code='2330',quantity=len(calls),id=1,cond='Cash',direction='Buy')]
 b.api=NS(list_positions=ps,list_profit_loss=lambda **kw:[],
          list_position_detail=lambda **kw:[NS(code='2330',date=OLD,dseq='old',quantity=0,cond='Cash',direction='Buy')])
 assert b.historical_accounting(DAY,TODAY,['2330'])['positions_stable'] is False


def test_pending_duplicate_identity_is_rejected_before_inference():
 s,l,o=setup()
 with pytest.raises(SignalError,match='duplicate pending'):
  resolve_history(s,l,[o,copy.deepcopy(o)],today=TODAY)


def test_netting_realized_trade_is_not_omitted_from_cash_buy_history(monkeypatch):
 from test_taiwan_review import adapter
 b=adapter(monkeypatch)
 b.api=NS(list_positions=lambda **kw:[],
  list_profit_loss=lambda **kw:[NS(code='2330',quantity=2,id=1,cond='Netting',date=DAY,dseq='sale')],
  list_profit_loss_detail=lambda **kw:[NS(code='2330',quantity=2,cond='Netting',date=DAY,dseq='number')])
 s=b.historical_accounting(DAY,TODAY,['2330'])
 assert s['sales'][0]['buy_sources'][0]['shares']==2


def test_overlapping_margin_position_does_not_disappear_from_zero_fill_proof(monkeypatch):
 from test_taiwan_review import adapter
 b=adapter(monkeypatch)
 b.api=NS(list_positions=lambda **kw:[NS(code='2330',quantity=1,cond='MarginTrading',direction='Buy')])
 with pytest.raises(BrokerError,match='non-cash'):b.historical_accounting(DAY,TODAY,['2330'])


def test_open_partial_fill_control_is_included_in_history_query():
 s,l,o=setup()
 control=order(seq='control',applied=1)
 control.update(broker_order_id='control',ticker='control')
 control['broker_snapshot'].update(broker_order_id='control',ticker='control')
 l['open_orders'].append(control);l['order_history']=[]
 b=HistoryBroker(s)
 def query(begin,end,tickers):
  assert 'control' in tickers
  return copy.deepcopy(s)
 b.historical_accounting=query
 w=BrokerWorker(b)
 try:assert refresh_open_orders(w,l,today=TODAY)
 finally:w.close()
 assert not l['open_orders']


def test_same_broker_id_on_different_historical_days_resolves_separately():
 s,l,o=setup()
 older=copy.deepcopy(o)
 older['trading_day']=older['broker_snapshot']['trading_day']=OLD
 l['open_orders'].append(older)
 control=copy.deepcopy(l['order_history'][0]);control['trading_day']=OLD
 l['order_history'].append(control)
 s['begin_date']=OLD
 s['positions'][0]['shares']=2
 s['positions'][0]['lots'].append(lot('control',day=OLD,seq='older-control'))
 b=HistoryBroker(s);w=BrokerWorker(b)
 try:assert refresh_open_orders(w,l,today=TODAY)
 finally:w.close()
 assert not l['open_orders']
 resolved=[x for x in l['order_history'] if x['broker_order_id']=='oid']
 assert len(resolved)==2 and {x['trading_day'] for x in resolved}=={DAY,OLD}
