from dataclasses import replace
import json
import pytest
from order_desk.models import ExecutionPolicy,Quote
from order_desk.planner import PlanningError,build_order_plan
from order_desk.capital import load_account_ledger
from test_plan import _signal,NOW
from test_active_retained_withdrawal import setup_active,freeze
from test_manual_control import send


def make_plan(tmp_path,*,held=1400,target=40000,weight_ticker='2330',max_turnover=2,external=0,**limits):
    signal=_signal(tmp_path/'signal.json',[{'strategy_id':'a','weights':[{'ticker':weight_ticker,'weight':1}]}])
    quotes={t:Quote(t,bid=100,ask=100,last=100,limit_up=110,limit_down=90) for t in ['2330','2317']}
    return build_order_plan(signal,{'a':target},quotes,{'2330':held} if held else {},
        {'2330':held+external},1000000,ExecutionPolicy(lot_mode='odd',max_turnover=max_turnover,**limits),mode='production',now=NOW)


def test_large_withdrawal_uses_before_holdings_not_small_remaining_budget(tmp_path):
    p=make_plan(tmp_path)
    assert p.estimated_sell_twd==90000 and p.estimated_buy_twd==0
    assert p.turnover_basis_twd==140000
    assert p.turnover_ratio==pytest.approx(90000/140000)
    assert p.estimated_sell_twd/p.total_budget_twd>2  # Old erroneous denominator.


def test_small_custom_turnover_limit_still_blocks_withdrawal(tmp_path):
    with pytest.raises(PlanningError,match=r'gross=.*basis=.*managed_before=.*ratio=.*limit=0.5'):
        make_plan(tmp_path,max_turnover=.5)


def test_zero_target_full_liquidation_still_obeys_turnover_limit(tmp_path):
    with pytest.raises(PlanningError,match='max_turnover'):
        make_plan(tmp_path,target=0,max_turnover=.5)
    p=make_plan(tmp_path,target=0,max_turnover=1)
    assert p.turnover_basis_twd==140000 and p.turnover_ratio==.9


def test_external_stock_does_not_inflate_turnover_allowance(tmp_path):
    with pytest.raises(PlanningError,match='max_turnover'):
        make_plan(tmp_path,held=100,target=2000,max_turnover=.5,external=1000000)


def test_deposit_uses_larger_target_budget(tmp_path):
    p=make_plan(tmp_path,held=100,target=100000,max_turnover=1)
    assert p.turnover_basis_twd==100000 and p.turnover_ratio==pytest.approx(.99)


def test_no_capital_still_enforces_limit_on_initial_purchase(tmp_path):
    with pytest.raises(PlanningError,match='max_turnover'):
        make_plan(tmp_path,held=0,target=100000,max_turnover=1)


def test_equal_value_rotation_keeps_existing_turnover_protection(tmp_path):
    with pytest.raises(PlanningError,match='max_turnover'):
        make_plan(tmp_path,target=140000,weight_ticker='2317',max_turnover=1.5)
    assert make_plan(tmp_path,target=140000,weight_ticker='2317',max_turnover=2).turnover_ratio==2


@pytest.mark.parametrize('bad',[float('nan'),float('inf'),-1,True])
def test_invalid_turnover_limit_cannot_disable_protection(tmp_path,bad):
    with pytest.raises(PlanningError,match='finite and nonnegative'):
        make_plan(tmp_path,max_turnover=bad)


@pytest.mark.parametrize('limits,message',[
    ({'max_daily_twd':50000},'max_daily_twd'),
    ({'max_order_twd':50000},'max_order_twd'),
    ({'max_position_twd':10000},'max_position_twd'),
])
def test_absolute_limits_remain_enforced_on_withdrawal(tmp_path,limits,message):
    with pytest.raises(PlanningError,match=message):make_plan(tmp_path,**limits)


def test_large_withdrawal_freezes_retained_stock_and_does_not_repeat(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch,withdraw=1400)
    assert freeze(cli,base)==0
    b.auto_fill=True;assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path);p=l['execution_history'][-1]
    assert p['budgets']['alpha']==600 and p['turnover']['limit']==2
    assert p['turnover']['basis_twd']==1500
    assert sum(o['estimated_notional'] for o in p['orders'])/600>2
    assert p['turnover']['ratio']<=2
    assert b.orders[key]['status']=='Submitted'
    assert all(o['ticker']!='2317' for o in p['orders'])
    count=len(b.orders);assert send(cli,base)==0 and len(b.orders)==count
