import copy
import json
import pytest
from order_desk.capital import load_account_ledger, save_account_ledger
from order_desk.control import begin_manual, retire_for_manual, manual_hold, release_manual
from order_desk.signal import SignalError
from test_inventory_isolation import fixture


def setup(tmp_path, monkeypatch):
    cli,b,args=fixture(tmp_path,monkeypatch,initial_short=False)
    p=tmp_path/'config.json';c=json.loads(p.read_text());c['schedule']={'enabled':True};c['sell_wait_seconds']=0;p.write_text(json.dumps(c))
    monkeypatch.setattr(cli,'_refuse_weekly_order',lambda *a:0)
    base=['--config',str(p)]
    return cli,b,base


def prepare(cli,base):return cli.main(base+['--new-manual-plan','--confirm','APPROVE'])

def send(cli,base):return cli.main(base+['--send','--confirm','APPROVE'])


def test_manual_preparation_never_sends_and_blocks_scheduler_even_without_pending(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch)
    assert prepare(cli,base)==0 and not b.orders
    l,_=load_account_ledger(tmp_path);key=l['manual_session']['id']
    assert manual_hold(l) and l['manual_session']['state']=='ready'
    assert cli.main(base+['--weekly-order'])==0 and not b.orders
    assert prepare(cli,base)==0
    assert load_account_ledger(tmp_path)[0]['manual_session']['id']==key


def test_manual_completion_and_repeated_send_do_not_start_another_rebalance(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch)
    assert prepare(cli,base)==0
    assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['execution_history'][-1]['origin']=='manual'
    assert l['manual_session']['state']=='completed'
    count=len(b.orders)
    assert send(cli,base)==0 and cli.main(base+['--weekly-order'])==0
    assert len(b.orders)==count


def test_interactive_send_never_adopts_schedule_plan(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch);b.auto_fill=False
    assert cli.main(base+['--weekly-order'])==0
    l,_=load_account_ledger(tmp_path);assert l['execution']['origin']=='schedule'
    before=copy.deepcopy(l);count=len(b.orders)
    assert send(cli,base)==1
    assert len(b.orders)==count and load_account_ledger(tmp_path)[0]==before


def test_schedule_never_resumes_manual_plan_after_restart(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch);b.auto_fill=False
    assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path);assert l['execution']['origin']=='manual'
    before=copy.deepcopy(l);count=len(b.orders)
    assert cli.main(base+['--weekly-order'])==0
    assert len(b.orders)==count and load_account_ledger(tmp_path)[0]==before


def test_working_old_order_blocks_handoff_but_retains_schedule_hold(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch);b.auto_fill=False
    assert cli.main(base+['--weekly-order'])==0
    count=len(b.orders)
    assert prepare(cli,base)==1
    l,_=load_account_ledger(tmp_path)
    assert l['execution']['origin']=='schedule' and manual_hold(l)
    assert l['manual_session']['state']=='waiting_for_reconciliation'
    assert cli.main(base+['--weekly-order'])==0 and send(cli,base)==1
    assert len(b.orders)==count


def test_partial_fill_cancel_retire_preserves_fills_and_original_capital(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch);b.auto_fill=False
    assert cli.main(base+['--weekly-order'])==0
    key=next(iter(b.orders));b.fill_order(key,shares=2);b.orders[key]['status']='Cancelled'
    assert prepare(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['strategies']['alpha']['2330']==8
    assert not l['open_orders'] and 'execution' not in l
    assert l['execution_history'][-1]['outcome']=='superseded_by_manual'
    assert l['manual_session']['carry_budgets']['alpha']==2000
    assert l['order_history'][-1]['applied_shares']==2
    assert prepare(cli,base)==0 and load_account_ledger(tmp_path)[0]['strategies']['alpha']['2330']==8
    b.auto_fill=True
    assert send(cli,base)==0
    assert [(r['action'],r['ticker'],r['shares']) for k,r in b.orders.items() if k!=key]==[('Sell','2330',3),('Buy','2317',5)]
    count=len(b.orders);assert send(cli,base)==0 and len(b.orders)==count


def test_config_change_cannot_silently_resume_old_manual_plan(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch);b.auto_fill=False
    assert send(cli,base)==0
    p=tmp_path/'config.json';c=json.loads(p.read_text());c['strategies'][0]['adjust_positions']=False;p.write_text(json.dumps(c))
    count=len(b.orders);assert send(cli,base)==1 and len(b.orders)==count


def test_paused_strategy_has_no_new_allocations_after_retirement(tmp_path,monkeypatch):
    from test_capital import _write_signal
    cli,b,base=setup(tmp_path,monkeypatch);b.auto_fill=False
    assert cli.main(base+['--weekly-order'])==0
    key=next(iter(b.orders));b.orders[key]['status']='Cancelled'
    p=tmp_path/'config.json';c=json.loads(p.read_text())
    # Freeze alpha, and activate beta with its own stock budget.
    c['strategies'][0]['adjust_positions']=False
    _write_signal(tmp_path/'strategies/beta/signal.json','beta',[{'ticker':'2317','weight':1.0}])
    sp=tmp_path/'strategies/beta/signal.json';s=json.loads(sp.read_text());s.update(effective_at='2000-01-01T00:00:00+00:00',expires_at='2099-01-01T00:00:00+00:00');sp.write_text(json.dumps(s))
    c['strategies'].append({'name':'beta','signal_path':'strategies/beta/signal.json','extra_twd':1000});p.write_text(json.dumps(c))
    assert prepare(cli,base)==0
    before=load_account_ledger(tmp_path)[0]
    held=copy.deepcopy(before['strategies']['alpha']);old_count=len(before['order_history'])
    b.auto_fill=True;assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path);assert l['strategies']['alpha']==held
    new=l['order_history'][old_count:]
    assert new and all(a['strategy']=='beta' for o in new for a in o['allocation'])


def test_unknown_submitted_identity_cannot_be_discarded():
    l={'execution':{'submitted':['lost'],'budgets':{'a':100}},'open_orders':[]}
    begin_manual(l);before=copy.deepcopy(l)
    with pytest.raises(SignalError,match='unique final'):retire_for_manual(l)
    assert l==before


def test_return_to_schedule_refuses_unfinished_manual_work(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch);assert prepare(cli,base)==0
    called=[];monkeypatch.setattr(cli,'_install_schedule',lambda *a:called.append(1) or 0)
    monkeypatch.setattr(cli,'_weekly_tick',lambda *a:called.append(2) or 0)
    assert cli.main(base+['--resume-schedule','--confirm','APPROVE'])==1 and not called
    assert send(cli,base)==0
    assert cli.main(base+['--resume-schedule','--confirm','APPROVE'])==0
    assert called==[1,2] and not manual_hold(load_account_ledger(tmp_path)[0])


def test_prepare_requires_confirmation_and_cannot_send(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch)
    assert cli.main(base+['--new-manual-plan'])==1
    with pytest.raises(SystemExit):cli.main(base+['--new-manual-plan','--send','--confirm','APPROVE'])
    assert not b.orders


def test_missing_final_order_blocks_retirement_without_clearing_history(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch);b.auto_fill=False
    assert cli.main(base+['--weekly-order'])==0
    b.reconcile_orders=lambda:[]
    assert prepare(cli,base)==1
    l,_=load_account_ledger(tmp_path)
    assert l['open_orders'][0]['status']=='Unconfirmed' and 'execution' in l
    assert manual_hold(l)


def test_retained_capital_new_withdrawal_applies_once_in_manual_session(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch);b.auto_fill=False
    assert cli.main(base+['--weekly-order'])==0
    key=next(iter(b.orders));b.fill_order(key,shares=2);b.orders[key]['status']='Cancelled'
    p=tmp_path/'config.json';c=json.loads(p.read_text());c['strategies'][0]['cash_out_twd']=500;p.write_text(json.dumps(c))
    assert prepare(cli,base)==0 and prepare(cli,base)==0
    b.auto_fill=True;assert send(cli,base)==0
    l,_=load_account_ledger(tmp_path)
    assert l['execution_history'][-1]['budgets']=={'alpha':1500}
    count=len(b.orders);assert send(cli,base)==0 and len(b.orders)==count


def test_hold_remains_when_preparation_signal_is_invalid(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch)
    (tmp_path/'strategies/alpha/signal.json').write_text('{}')
    assert prepare(cli,base)==1
    assert manual_hold(load_account_ledger(tmp_path)[0])
    assert cli.main(base+['--weekly-order'])==0 and not b.orders


def test_paused_all_strategies_can_complete_no_order_manual_session(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch)
    p=tmp_path/'config.json';c=json.loads(p.read_text());c['strategies'][0]['adjust_positions']=False;p.write_text(json.dumps(c))
    assert prepare(cli,base)==0 and send(cli,base)==0 and not b.orders
    assert load_account_ledger(tmp_path)[0]['manual_session']['state']=='completed'


def test_changed_signal_after_manual_preview_requires_new_review(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch);assert prepare(cli,base)==0
    p=tmp_path/'strategies/alpha/signal.json';s=json.loads(p.read_text());s['revision']=99;p.write_text(json.dumps(s))
    assert send(cli,base)==1 and not b.orders


def test_lost_manual_marker_does_not_allow_schedule_to_send(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch);b.auto_fill=False;assert send(cli,base)==0
    l,p=load_account_ledger(tmp_path);l.pop('manual_session');save_account_ledger(tmp_path,l,p)
    count=len(b.orders);assert cli.main(base+['--weekly-order'])==0 and len(b.orders)==count


def test_manual_request_while_manual_work_pending_has_a_new_session_identity():
    l={};s=begin_manual(l);s['state']='ready';old=s['id']
    l['execution']={'origin':'manual','manual_session_id':old}
    new=begin_manual(l,new_request=True)
    assert new['id']!=old and new['state']=='waiting_for_reconciliation'
    assert begin_manual(l,new_request=True)['id']==new['id']


def test_missing_manual_identity_blocks_interactive_resume(tmp_path,monkeypatch):
    cli,b,base=setup(tmp_path,monkeypatch);b.auto_fill=False;assert send(cli,base)==0
    l,p=load_account_ledger(tmp_path);l.pop('manual_session');save_account_ledger(tmp_path,l,p)
    count=len(b.orders);assert send(cli,base)==1 and len(b.orders)==count


def test_account_lock_prevents_simultaneous_manual_handoff(tmp_path,monkeypatch):
    from order_desk.capital import account_lock
    cli,b,base=setup(tmp_path,monkeypatch)
    before=load_account_ledger(tmp_path)[0]
    with account_lock(tmp_path):assert prepare(cli,base)==1
    assert load_account_ledger(tmp_path)[0]==before and not b.orders


def test_inventory_loss_reduces_carried_manual_capital(tmp_path,monkeypatch):
    from order_desk.inventory import reconcile_actual_inventory
    from test_capital import _quote
    l={'strategies':{'alpha':{'2330':10}},'open_orders':[],
       'manual_session':{'id':'manual','state':'ready','carry_budgets':{'alpha':2000}}}
    report={'checked_at':'2026-01-01T00:00:00Z','inventory_issues':{'2330':['short']},
            'positions':[{'ticker':'2330','broker_shares':8,'external_shares':0}]}
    assert reconcile_actual_inventory(l,report,{'2330':_quote('2330')})
    assert l['manual_session']['carry_budgets']['alpha']==1800
