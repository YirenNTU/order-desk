import copy,json
import pytest
from order_desk.capital import load_account_ledger,save_account_ledger
from order_desk.control import retained_buy_reservations,validate_retained_orders
from order_desk.signal import SignalError
from test_retained_orders import setup_retained
from test_manual_control import send


def setup_active(tmp_path,monkeypatch,*,withdraw=500,buy=True):
    cli,b,base,key=setup_retained(tmp_path,monkeypatch,buy=buy,beta_ticker='2317' if not buy else '2330')
    p=tmp_path/'config.json';c=json.loads(p.read_text())
    c['strategies'][0].update(adjust_positions=True,cash_out_twd=withdraw)
    c['strategies'][1].update(adjust_positions=False,extra_twd=0)
    p.write_text(json.dumps(c))
    return cli,b,base,key


def freeze(cli,base):return cli.main(base+['--new-manual-plan','--freeze-working-stocks','--confirm','APPROVE'])


def test_active_strategy_withdrawal_retains_buy_and_only_sends_other_stock_orders(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch)
    assert freeze(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['manual_session']['frozen_working_stocks']==['2317']
    reserve=retained_buy_reservations(l)['alpha']
    assert 570<=reserve<=571
    count=len(b.orders);b.auto_fill=True
    assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path);execution=l['execution_history'][-1]
    assert execution['budgets']['alpha']==1500
    assert execution['retained_buy_reservations']['alpha']==reserve
    assert execution['frozen_working_stocks']==['2317']
    assert all(r['ticker']!='2317' for k,r in b.orders.items() if k!=key)
    assert len(b.orders)>count and b.orders[key]['status']=='Submitted'
    assert sum(q*100 for q in l['strategies']['alpha'].values())+reserve<=1500
    count=len(b.orders);assert send(cli,base)==0 and len(b.orders)==count
    assert l['manual_session']['state']=='completed'


def test_withdrawal_cannot_consume_retained_buy_reservation(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch,withdraw=1500)
    count=len(b.orders);assert freeze(cli,base)==1
    l,_=load_account_ledger(tmp_path)
    assert l['manual_session']['state']=='waiting_for_reconciliation'
    assert len(b.orders)==count and b.orders[key]['status']=='Submitted'


def test_active_retained_partial_fill_reserves_only_remaining_incremental_cost(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch)
    b.fill_order(key,shares=2)
    assert freeze(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['strategies']['alpha']['2317']==2
    assert 330<=retained_buy_reservations(l)['alpha']<=331
    b.auto_fill=True;assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['strategies']['alpha']['2317']==2
    assert all(r['ticker']!='2317' for k,r in b.orders.items() if k!=key)
    reserve=l['execution_history'][-1]['retained_buy_reservations']['alpha']
    assert sum(q*100 for q in l['strategies']['alpha'].values())+reserve<=1500


def test_frozen_current_holdings_also_limit_withdrawal(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch,withdraw=1600)
    b.fill_order(key,shares=2)
    # Post-withdrawal budget 400 exceeds pending 330 but not pending+held 530.
    count=len(b.orders);assert freeze(cli,base)==1 and len(b.orders)==count


def test_filled_retained_stock_stays_frozen_for_entire_manual_session(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch);assert freeze(cli,base)==0
    b.fill_order(key)
    b.auto_fill=True;assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['strategies']['alpha']['2317']==5
    assert all(r['ticker']!='2317' for k,r in b.orders.items() if k!=key)
    assert l['execution_history'][-1]['targets']['alpha']['2317']==5
    with pytest.raises(SignalError,match='frozen'):validate_retained_orders(l,candidate_tickers=['2317'])


def test_retained_cancel_does_not_reintroduce_frozen_stock_into_new_targets(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch);assert freeze(cli,base)==0
    b.orders[key]['status']='Cancelled';b.auto_fill=True
    assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['strategies']['alpha'].get('2317',0)==0
    assert all(r['ticker']!='2317' for k,r in b.orders.items() if k!=key)


def test_pending_sell_stays_frozen_and_its_proceeds_are_not_assumed(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch,buy=False)
    assert freeze(cli,base)==0
    l,_=load_account_ledger(tmp_path);assert retained_buy_reservations(l)=={}
    held=l['strategies']['alpha']['2330'];b.auto_fill=True
    assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['strategies']['alpha']['2330']==held
    assert b.orders[key]['status']=='Submitted'
    assert all(r['ticker']!='2330' for k,r in b.orders.items() if k!=key)
    assert sum(q*100 for q in l['strategies']['alpha'].values())<=1500


def test_repeated_freeze_preparation_does_not_repeat_withdrawal(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch)
    assert freeze(cli,base)==0 and freeze(cli,base)==0
    b.auto_fill=True;assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path);assert l['execution_history'][-1]['budgets']['alpha']==1500


def test_freeze_option_requires_prepare_and_never_sends(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch)
    count=len(b.orders)
    with pytest.raises(SystemExit):cli.main(base+['--freeze-working-stocks','--send','--confirm','APPROVE'])
    assert len(b.orders)==count


def test_old_strict_keep_option_still_refuses_active_strategy_overlap(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch)
    assert cli.main(base+['--new-manual-plan','--keep-working-orders','--confirm','APPROVE'])==1


def test_partial_fill_between_quote_and_audit_uses_preloaded_frozen_quote(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch)
    # A retained stock may no longer appear in new signal weights.
    p=tmp_path/'strategies/alpha/signal.json';s=json.loads(p.read_text());s['strategies'][0]['weights']=[{'ticker':'6213','weight':.5},{'ticker':'2330','weight':.5}];p.write_text(json.dumps(s))
    assert freeze(cli,base)==0
    original=b.quotes;called=[]
    def quotes(tickers):
        assert '2317' in tickers
        result=original(tickers)
        if not called:called.append(1);b.fill_order(key,shares=2)
        return result
    b.quotes=quotes;b.auto_fill=True
    assert send(cli,base)==0
    assert load_account_ledger(tmp_path)[0]['strategies']['alpha']['2317']==2


def test_retained_buy_price_increase_requires_new_capital_review(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch);assert freeze(cli,base)==0
    b.orders[key]['limit_price']=120
    count=len(b.orders);assert send(cli,base)==1 and len(b.orders)==count
    assert freeze(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert retained_buy_reservations(l)['alpha']==620


def test_unconfirmed_active_retained_order_is_never_ignored(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch)
    b.reconcile_orders=lambda:[]
    count=len(b.orders);assert freeze(cli,base)==1 and len(b.orders)==count
    assert load_account_ledger(tmp_path)[0]['open_orders'][0]['status']=='Unconfirmed'


def test_other_paused_strategy_holdings_remain_intact_during_active_withdrawal(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch)
    l,p=load_account_ledger(tmp_path);l['strategies']['beta']={'2330':3};b._positions['2330']+=3
    save_account_ledger(tmp_path,l,p)
    # Reset the mock's audit baseline to match an existing independent sleeve.
    from order_desk.audit import audit_account,record_audit
    from order_desk.broker import BrokerWorker
    w=BrokerWorker(b)
    try:record_audit(tmp_path,audit_account(w,l))
    finally:w.close()
    assert freeze(cli,base)==0
    b.auto_fill=True;assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['strategies']['beta']=={'2330':3}
    last=l['execution_history'][-1]
    assert all(a['strategy']=='alpha' for legs in last['allocations'].values() for a in legs)


def test_new_session_does_not_subtract_old_pending_buy_cash_twice(tmp_path,monkeypatch):
    cli,b,base,key=setup_active(tmp_path,monkeypatch);assert freeze(cli,base)==0
    b.auto_fill=True;assert send(cli,base)==0
    before=load_account_ledger(tmp_path)[0];count=len(b.orders)
    p=tmp_path/'config.json';c=json.loads(p.read_text());c['strategies'][0]['cash_out_twd']=0;p.write_text(json.dumps(c))
    assert freeze(cli,base)==0 and send(cli,base)==0
    after=load_account_ledger(tmp_path)[0]
    assert after['strategies']==before['strategies'] and len(b.orders)==count
    reserve=retained_buy_reservations(after)['alpha']
    assert after['execution_history'][-1]['budgets']['alpha']==sum(q*100 for q in after['strategies']['alpha'].values())+reserve


def test_previously_retained_order_filled_before_freeze_request_stays_fixed(tmp_path,monkeypatch):
    from test_retained_orders import keep
    cli,b,base,key=setup_retained(tmp_path,monkeypatch)
    assert keep(cli,base)==0
    b.fill_order(key);assert cli.main(base+['--reconcile-only'])==0
    p=tmp_path/'config.json';c=json.loads(p.read_text())
    c['strategies'][0].update(adjust_positions=True,cash_out_twd=500)
    c['strategies'][1].update(adjust_positions=False,extra_twd=0);p.write_text(json.dumps(c))
    assert freeze(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert not l['open_orders'] and l['manual_session']['frozen_working_stocks']==['2317']
    b.auto_fill=True;assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['strategies']['alpha']['2317']==5
    assert all(r['ticker']!='2317' for k,r in b.orders.items() if k!=key)
