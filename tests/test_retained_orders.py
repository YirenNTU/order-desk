import copy,json
import pytest
from order_desk.capital import load_account_ledger,save_account_ledger
from order_desk.control import blocking_orders,retention_scope,validate_retained_orders
from order_desk.signal import SignalError
from test_manual_control import setup,send
from test_capital import _write_signal


def setup_retained(tmp_path,monkeypatch,*,buy=True,beta_ticker='2330'):
    cli,b,base=setup(tmp_path,monkeypatch)
    place=b.place_order
    def submit(intent):
        b.auto_fill=bool(buy and intent.action=='Sell')
        return place(intent)
    b.place_order=submit
    assert cli.main(base+['--weekly-order'])==0
    live=[(k,o) for k,o in b.orders.items() if o['status']=='Submitted']
    assert len(live)==1
    key,order=live[0]
    b.place_order=place
    _write_signal(tmp_path/'strategies/beta/signal.json','beta',[{'ticker':beta_ticker,'weight':1.0}])
    p=tmp_path/'strategies/beta/signal.json';s=json.loads(p.read_text());s.update(effective_at='2000-01-01T00:00:00+00:00',expires_at='2099-01-01T00:00:00+00:00');p.write_text(json.dumps(s))
    p=tmp_path/'config.json';c=json.loads(p.read_text());c['strategies'][0]['adjust_positions']=False
    c['strategies'].append({'name':'beta','signal_path':'strategies/beta/signal.json','extra_twd':1000});p.write_text(json.dumps(c))
    return cli,b,base,key


def keep(cli,base):return cli.main(base+['--new-manual-plan','--keep-working-orders','--confirm','APPROVE'])


def test_other_strategy_buy_remains_live_while_manual_plan_completes(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch)
    count=len(b.orders);assert keep(cli,base)==0 and len(b.orders)==count
    l,_=load_account_ledger(tmp_path)
    assert len(l['open_orders'])==1 and not blocking_orders(l)
    assert l['open_orders'][0]['retained_for_manual']==l['manual_session']['id']
    assert l['execution_history'][-1]['outcome']=='superseded_by_manual'
    # The original order is untouched; only the new beta buy is sent.
    held=copy.deepcopy(l['strategies']['alpha']);b.auto_fill=True
    assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['strategies']['alpha']==held and l['strategies']['beta']['2330']==10
    assert b.orders[key]['status']=='Submitted'
    assert l['manual_session']['state']=='completed' and 'execution' not in l
    assert len(l['open_orders'])==1
    count=len(b.orders);assert send(cli,base)==0 and len(b.orders)==count


def test_retained_buy_keeps_cash_reservation_and_can_block_new_buy(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch)
    b._bank_cash=700
    assert keep(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    report=l.get('cash_safety',{})
    # Preparation is only a preview; send still applies account-wide cash checks.
    count=len(b.orders);b.auto_fill=True
    assert send(cli,base)==1
    l,_=load_account_ledger(tmp_path)
    assert len(b.orders)==count and l['cash_safety']['status']!='allowed'
    assert l['cash_safety']['unfilled_buy_hold_twd']>0
    b._bank_cash=10000
    assert send(cli,base)==0 and len(b.orders)==count+1


def test_retained_sell_does_not_block_unrelated_manual_buy_or_fund_it(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch,buy=False,beta_ticker='2317')
    assert keep(cli,base)==0
    before=b._cash;b.auto_fill=True;assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert b.orders[key]['action']=='Sell' and b.orders[key]['status']=='Submitted'
    assert l['strategies']['alpha']['2330']==10
    assert l['strategies']['beta']['2317']==10 and b._cash<before


def test_overlap_or_active_owner_cannot_be_retained(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch,beta_ticker='2317')
    count=len(b.orders);assert keep(cli,base)==1
    l,_=load_account_ledger(tmp_path)
    assert 'execution' in l and not l['open_orders'][0].get('retained_for_manual')
    assert len(b.orders)==count


def test_working_order_of_active_strategy_still_blocks(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch)
    p=tmp_path/'config.json';c=json.loads(p.read_text());c['strategies'][0]['adjust_positions']=True;p.write_text(json.dumps(c))
    assert keep(cli,base)==1


def test_retained_partial_fills_applied_once_to_original_strategy(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch)
    assert keep(cli,base)==0
    b.fill_order(key,shares=2)
    b.auto_fill=True;assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['strategies']['alpha']['2317']==2
    count=len(b.orders)
    assert cli.main(base+['--weekly-order'])==1  # reconciliation only; still working
    assert load_account_ledger(tmp_path)[0]['strategies']['alpha']['2317']==2
    b.fill_order(key)
    assert cli.main(base+['--weekly-order'])==0
    l,_=load_account_ledger(tmp_path)
    assert l['strategies']['alpha']['2317']==5 and not l['open_orders']
    assert len(b.orders)==count
    assert len([o for o in l['order_history'] if o['broker_order_id']==b.orders[key]['broker_order_id']])==1


@pytest.mark.parametrize('status',['Cancelled','Failed'])
def test_retained_terminal_order_archived_without_resubmission(tmp_path,monkeypatch,status):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch);assert keep(cli,base)==0
    b.auto_fill=True;assert send(cli,base)==0
    b.orders[key]['status']=status;count=len(b.orders)
    assert cli.main(base+['--weekly-order'])==0
    l,_=load_account_ledger(tmp_path)
    assert not l['open_orders'] and l['order_history'][-1]['status']==status
    assert len(b.orders)==count


def test_unknown_retained_order_blocks_new_orders(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch);assert keep(cli,base)==0
    b.reconcile_orders=lambda:[]
    count=len(b.orders);assert send(cli,base)==0  # no send, unresolved order report
    l,_=load_account_ledger(tmp_path)
    assert len(b.orders)==count and l['open_orders'][0]['status']=='Unconfirmed'


def test_retained_stock_cannot_be_added_to_new_signal_after_preparation(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch);assert keep(cli,base)==0
    p=tmp_path/'strategies/beta/signal.json';s=json.loads(p.read_text());s['strategies'][0]['weights']=[{'ticker':'2317','weight':1.0}];p.write_text(json.dumps(s))
    count=len(b.orders);assert send(cli,base)==1 and len(b.orders)==count


def test_retained_orders_cannot_be_reused_under_other_manual_session(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch);assert keep(cli,base)==0
    l,_=load_account_ledger(tmp_path);l['manual_session']['id']='other'
    with pytest.raises(SignalError,match='different manual session'):blocking_orders(l)


def test_keep_requires_explicit_manual_preparation(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch)
    with pytest.raises(SystemExit):cli.main(base+['--keep-working-orders','--send','--confirm','APPROVE'])
    assert not b.orders


def test_retained_price_and_allocation_must_be_verifiable(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch);assert keep(cli,base)==0
    l,_=load_account_ledger(tmp_path);l['open_orders'][0]['broker_snapshot']['limit_price']=0
    with pytest.raises(SignalError,match='price bound'):blocking_orders(l)
    l,_=load_account_ledger(tmp_path);l['open_orders'][0]['allocation'][0]['shares']+=1
    with pytest.raises(SignalError,match='allocation'):blocking_orders(l)


def test_retained_fill_during_audit_retries_without_double_applying_inventory(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch);assert keep(cli,base)==0
    # The retained buy fills between order refresh and inventory read.
    original=b.positions;calls=[]
    def positions():
        if not calls:
            calls.append(1);b.fill_order(key,shares=2)
        return original()
    b.positions=positions;b.auto_fill=True
    assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['strategies']['alpha']['2317']==2
    assert not any(h['ticker']=='2317' for h in l.get('inventory_adjustment_history',[]))


def test_retained_partial_fill_then_cancel_records_only_filled_shares(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch);assert keep(cli,base)==0
    b.fill_order(key,shares=2);b.orders[key]['status']='Cancelled'
    b.auto_fill=True;assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['strategies']['alpha']['2317']==2 and not l['open_orders']
    record=[o for o in l['order_history'] if o['broker_order_id']==b.orders[key]['broker_order_id']]
    assert len(record)==1 and record[0]['applied_shares']==2


def test_multiple_retained_sells_cannot_double_reserve_same_shares(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch,buy=False,beta_ticker='2317');assert keep(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    row=copy.deepcopy(l['open_orders'][0]);row['shares']=8;row['allocation'][0]['shares']=8
    row['broker_snapshot']['shares']=8
    l['open_orders'].append(row)
    with pytest.raises(SignalError,match='combined retained sells'):blocking_orders(l)


def test_retained_order_does_not_authorize_new_order_for_same_stock(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch);assert keep(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    with pytest.raises(SignalError,match='overlaps retained'):validate_retained_orders(l,candidate_tickers=['2317'])
    with pytest.raises(SignalError,match='overlaps retained'):validate_retained_orders(l,candidate_strategies=['alpha'])


def test_prior_day_working_order_needs_final_result_before_retention(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch);assert keep(cli,base)==0
    l,_=load_account_ledger(tmp_path);l['open_orders'][0]['trading_day']='2000-01-01'
    with pytest.raises(SignalError,match='prior-day'):blocking_orders(l)


def test_schedule_cannot_restart_sending_until_retained_orders_finish(tmp_path,monkeypatch):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch);assert keep(cli,base)==0
    b.auto_fill=True;assert send(cli,base)==0
    called=[];monkeypatch.setattr(cli,'_install_schedule',lambda *a:called.append(1) or 0)
    assert cli.main(base+['--resume-schedule','--confirm','APPROVE'])==1
    assert not called
