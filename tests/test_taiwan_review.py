import copy
import json
import sys
from datetime import date
from types import SimpleNamespace as NS
from dataclasses import replace
import pytest

from order_desk.broker import ShioajiBroker, BrokerError
from order_desk.calendar import TwseCalendar
from order_desk.capital import filled_shares
from order_desk.execution import submit_sells_then_buys, taipei_today
from order_desk.signal import SignalError
from test_capital import _intent


def adapter(monkeypatch):
    monkeypatch.setitem(sys.modules, 'shioaji', NS(Unit=NS(Share='Share')))
    b=ShioajiBroker('simulation')
    b.account=NS(account_id='123', broker_id='B', account_type='S')
    return b


def test_calendar_open_markers_are_trading_days(tmp_path, monkeypatch):
    rows=[{'Date':'1150101','Name':'中華民國開國紀念日','Description':'放假'},
          {'Date':'1150102','Name':'國曆新年開始交易日','Description':'國曆新年開始交易。'},
          {'Date':'1150211','Name':'農曆春節前最後交易日','Description':'最後交易'},
          {'Date':'1150212','Name':'市場無交易，僅辦理結算交割作業','Description':''},
          {'Date':'1150223','Name':'農曆春節後開始交易日','Description':''}]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return json.dumps(rows).encode()
    monkeypatch.setattr('urllib.request.urlopen',lambda *a,**k:Response())
    cal=TwseCalendar(tmp_path/'calendar.json');cal.refresh(2026)
    assert cal.is_trading_day(date(2026,1,2))
    assert cal.is_trading_day(date(2026,2,11))
    assert cal.is_trading_day(date(2026,2,23))
    assert not cal.is_trading_day(date(2026,2,12))
    assert not cal.is_trading_day(date(2026,1,1))


def test_cash_position_excludes_margin_and_short(monkeypatch):
    b=adapter(monkeypatch)
    b.api=NS(list_positions=lambda **kw:[NS(code='2330',quantity=5,direction='Buy',cond='Cash'),
        NS(code='2330',quantity=1000,direction='Buy',cond='MarginTrading'),
        NS(code='2330',quantity=1000,direction='Sell',cond='ShortSelling')])
    assert [(p.ticker,p.shares) for p in b.positions()]==[('2330',5)]


def trade(account,*,condition='Cash',security='STK'):
    return NS(order=NS(id='oid',account=account,order_cond=condition,action='Buy',order_lot='IntradayOdd',quantity=5,price=100),
        contract=NS(code='2330',security_type=security),status=NS(status='Filled',deal_quantity=5))


def test_trade_query_isolates_account_and_cash_orders(monkeypatch):
    b=adapter(monkeypatch)
    rows=[trade(b.account),trade(NS(account_id='other',broker_id='B',account_type='S')),
          trade(b.account,condition='MarginTrading'),trade(b.account,security='FUT')]
    b.api=NS(update_status=lambda a:None,list_trades=lambda:rows)
    assert len(b.reconcile_orders())==1


def test_snapshot_order_cannot_swap_security_prices(monkeypatch):
    b=adapter(monkeypatch)
    contracts={t:NS(code=t,limit_up=p*1.1,limit_down=p*.9,security_type='STK',exchange='TSE',unit=1000) for t,p in [('2330',100),('2317',50)]}
    b.api=NS(Contracts=NS(Stocks=contracts),snapshots=lambda cs:[NS(code='2317',buy_price=50,sell_price=50,close=50),NS(code='2330',buy_price=100,sell_price=100,close=100)])
    quotes=b.quotes(['2330','2317'])
    assert quotes['2330'].last==100 and quotes['2317'].last==50


@pytest.mark.parametrize('bad',[1.5,True,-1,float('nan')])
def test_fractional_or_invalid_fills_are_rejected(bad):
    with pytest.raises(SignalError): filled_shares({'filled_quantity':bad},'Common')


def test_resumed_corrupt_allocation_rejected_before_broker():
    order=_intent('2330','Buy',6,100)
    from dataclasses import asdict
    ledger={'strategies':{},'open_orders':[], 'execution':{'trading_day':taipei_today(),'orders':[asdict(order)],'allocations':{order.client_order_id:[{'strategy':'a','shares':2}]},'submitted':[]}}
    class Worker:
        def call(self,method,*args):
            if method=='reconcile_orders': return []
            pytest.fail('must not submit an invalid saved allocation')
    with pytest.raises(SignalError):
        submit_sells_then_buys(Worker(),[],ledger,{},cash_reserve_twd=0,wait_seconds=0,poll_seconds=0)


@pytest.mark.parametrize('shares',[1,999,1000,1001,499000,499001,500000,999999])
@pytest.mark.parametrize('side',[1,-1])
def test_taiwan_lot_splits_preserve_total_and_unique_ids(shares,side):
    from order_desk.planner import _orders_for_delta
    from order_desk.models import ExecutionPolicy
    from order_desk.validation import validate_intent
    from test_capital import _quote
    orders=_orders_for_delta('bundle',1,'2330',side*shares,_quote('2330'),ExecutionPolicy(lot_mode='odd'),odd_lots_enabled=True)
    assert sum(o.shares for o in orders)==shares
    assert len({o.client_order_id for o in orders})==len(orders)
    for order in orders:
        validate_intent(order)
        assert order.action==('Buy' if side>0 else 'Sell')
        if order.order_lot=='Common': assert order.broker_quantity<=499
        else: assert order.shares<1000


@pytest.mark.parametrize('price,tick',[(9.99,'.01'),(10,'.05'),(49.95,'.05'),(50,'.1'),(100,'.5'),(500,'1'),(1000,'5')])
def test_stock_price_tick_boundaries(price,tick):
    from decimal import Decimal
    from order_desk.planner import stock_tick
    assert stock_tick(price)==Decimal(tick)


def test_old_holiday_cache_requires_reinterpretation(tmp_path,monkeypatch):
    from order_desk.calendar import TWSE_HOLIDAY_URL
    p=tmp_path/'calendar.json';p.write_text(json.dumps({'schema_version':1,'year':2026,'source':TWSE_HOLIDAY_URL,'holidays':['2026-01-02']}))
    cal=TwseCalendar(p);calls=[]
    monkeypatch.setattr(cal,'refresh',lambda year:calls.append(year) or {'holidays':[],'trading_days':['2026-01-02']})
    assert cal.is_trading_day(date(2026,1,2))
    assert calls==[2026]


def test_missing_trade_account_cannot_be_treated_as_selected_account(monkeypatch):
    b=adapter(monkeypatch);b.api=NS(update_status=lambda a:None,list_trades=lambda:[trade(None)])
    with pytest.raises(BrokerError,match='account'): b.reconcile_orders()


def mock_order_api(monkeypatch):
    b=adapter(monkeypatch);calls=[]
    module=sys.modules['shioaji']
    module.Action=NS(Buy='Buy',Sell='Sell');module.StockPriceType=NS(LMT='LMT');module.OrderType=NS(ROD='ROD')
    module.StockOrderLot=NS(Common='Common',IntradayOdd='IntradayOdd');module.StockOrderCond=NS(Cash='Cash')
    module.StockOrder=lambda **kw:NS(**kw)
    contract=NS(code='2330',limit_up=110,limit_down=90,security_type=NS(value='STK'),exchange=NS(value='TSE'),unit=1000)
    def place(contract,order):
        calls.append(order)
        return NS(order=NS(id='123'),status=NS(status='Filled',deal_quantity=order.quantity))
    b.api=NS(Contracts=NS(Stocks={'2330':contract}),place_order=place)
    return b,calls


@pytest.mark.parametrize('quantity,lot,shares',[(2,'Common',2000),(6,'IntradayOdd',6)])
def test_sdk_cash_limit_rod_unit_conversion(monkeypatch,quantity,lot,shares):
    b,calls=mock_order_api(monkeypatch)
    intent=replace(_intent('2330','Buy',shares,100),order_lot=lot,broker_quantity=quantity)
    result=b.place_order(intent)
    assert result.filled_shares==shares
    assert (calls[0].quantity,calls[0].order_lot,calls[0].order_type,calls[0].price_type,calls[0].order_cond)==(quantity,lot,'ROD','LMT','Cash')


@pytest.mark.parametrize('hour,minute,is_open',[(13,26,True),(8,59,True),(10,0,False)])
def test_each_production_order_checks_session_before_api(monkeypatch,hour,minute,is_open):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from order_desk.broker import OrderNotSubmitted
    b,calls=mock_order_api(monkeypatch);b.mode='production'
    monkeypatch.setenv('TRADING_ALLOW_PRODUCTION',b.PRODUCTION_CONFIRMATION)
    monkeypatch.setattr('order_desk.broker.taipei_now',lambda:datetime(2026,11,9,hour,minute,tzinfo=ZoneInfo('Asia/Taipei')))
    monkeypatch.setattr('order_desk.broker.TwseCalendar.is_trading_day',lambda self,day:is_open)
    with pytest.raises(OrderNotSubmitted): b.place_order(_intent('2330','Buy',6,100))
    assert calls==[]


def test_pre_api_rejection_is_recorded_without_unknown_order_or_fake_fill():
    from order_desk.broker import OrderNotSubmitted
    from test_integrity import submit
    order=_intent('2330','Buy',6,100)
    ledger={'strategies':{},'open_orders':[]}
    class Worker:
        def call(self,method,*args):
            if method=='reconcile_orders': return []
            raise OrderNotSubmitted('session ended before submission')
    with pytest.raises(OrderNotSubmitted):
        submit(Worker(),ledger,[order],{order.client_order_id:[{'strategy':'a','shares':6}]})
    assert ledger['strategies']=={} and ledger['open_orders']==[]
    assert ledger['execution']['submitted']==[]
    assert ledger['execution']['reweight_required']
    assert ledger['local_rejection_history'][0]['status']=='NotSubmitted'
    assert not ledger.get('order_history')


def test_nonstandard_trading_unit_is_not_assumed_to_be_1000(monkeypatch):
    from order_desk.broker import OrderNotSubmitted
    b,calls=mock_order_api(monkeypatch);b.api.Contracts.Stocks['2330'].unit=200
    with pytest.raises(OrderNotSubmitted,match='unsupported'):b.place_order(_intent('2330','Buy',6,100))
    assert calls==[]


def test_invalid_saved_quantity_cannot_reach_broker(monkeypatch):
    from order_desk.broker import OrderNotSubmitted
    b,calls=mock_order_api(monkeypatch)
    with pytest.raises(OrderNotSubmitted):b.place_order(replace(_intent('2330','Buy',6,100),broker_quantity=6000))
    assert calls==[]


@pytest.mark.parametrize('lot,multiplier',[('Common',1000),('Fixing',1000),('Odd',1),('IntradayOdd',1)])
def test_manual_trade_lot_units_include_after_hours_fixed_price(lot,multiplier):
    assert filled_shares({'filled_quantity':2},lot)==2*multiplier


def test_pending_after_hours_manual_sell_reserves_thousands_of_shares():
    from order_desk.audit import audit_account
    from order_desk.models import BrokerPosition
    class Worker:
        def call(self,method,*args):
            if method=='positions':return [BrokerPosition('2330',2500)]
            return [{'broker_order_id':'manual','ticker':'2330','action':'Sell','order_lot':'Fixing',
                     'quantity':2,'filled_quantity':0,'status':'Submitted'}]
    ledger={'strategies':{'a':{'2330':1500}},'open_orders':[]}
    report=audit_account(Worker(),ledger)
    assert '2330' in report['inventory_issues']
    assert any('pending manual sells' in msg for msg in report['inventory_issues']['2330'])
