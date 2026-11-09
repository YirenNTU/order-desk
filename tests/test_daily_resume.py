import copy
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import pytest
from order_desk.capital import load_account_ledger, save_account_ledger
from order_desk.execution import prepare_next_day_execution, refresh_open_orders, taipei_today
from order_desk.signal import SignalError
from test_integrity import _open_buy, _Rows
from test_inventory_isolation import fixture


def test_terminal_snapshot_survives_broker_day_rollover():
    old = (datetime.fromisoformat(taipei_today()) - timedelta(days=1)).date().isoformat()
    order = _open_buy(old)
    ledger = {'strategies': {}, 'open_orders': [order]}
    row = {'broker_order_id': order['broker_order_id'], 'filled_shares': 2, 'status': 'Cancelled', 'trading_day': old}
    refresh_open_orders(_Rows([row]), ledger, today=old)
    assert ledger['strategies']['alpha']['2330'] == 2
    assert refresh_open_orders(_Rows([]), ledger)
    assert ledger['open_orders'] == []
    assert ledger['order_history'][0]['applied_shares'] == 2
    assert ledger['strategies']['alpha']['2330'] == 2


def test_missing_nonterminal_order_is_not_assumed_expired():
    ledger = {'strategies': {}, 'open_orders': [_open_buy('2000-01-01')]}
    assert not refresh_open_orders(_Rows([]), ledger)
    assert ledger['open_orders'][0]['status'] == 'Unconfirmed'


def test_new_day_reprices_pending_targets_without_reapplying_deposit(tmp_path, monkeypatch):
    import order_desk.__main__ as cli
    from order_desk.books import load_strategy_entries, signal_signature
    from order_desk.deferrals import cycle_id
    cli, fake, args = fixture(tmp_path, monkeypatch, initial_short=False)
    config = json.loads((tmp_path/'config.json').read_text())
    config.pop('inventory_mismatch_policy')  # Existing users get new defaults after pull.
    config['strategies'][0]['extra_twd'] = 9999  # Already included in saved capital, must not be added twice.
    (tmp_path/'config.json').write_text(json.dumps(config))
    ledger, paths = load_account_ledger(tmp_path)
    old = (datetime.fromisoformat(taipei_today()) - timedelta(days=1)).date().isoformat()
    ledger['execution'] = {'trading_day': old, 'cycle_id': cycle_id(), 'budgets': {'alpha': 2000},
        'targets': {'alpha': {'6213': 10, '2330': 5, '2317': 5}},
        'signal_signature': signal_signature(load_strategy_entries(config, tmp_path)),
        'orders': [], 'allocations': {}, 'submitted': []}
    save_account_ledger(tmp_path, ledger, paths)
    assert cli.main(args) == 0
    after, _ = load_account_ledger(tmp_path)
    assert after['strategies']['alpha'] == {'6213': 10, '2330': 5, '2317': 5}
    assert [(o['ticker'], o['action'], o['shares']) for o in fake.orders.values()] == [('2330', 'Sell', 5), ('2317', 'Buy', 5)]
    assert after['daily_retry_history'][0]['budgets'] == {'alpha': 2000}
    assert after['execution_history'][-1]['budgets'] == {'alpha': 2000}
    assert 'execution' not in after


def test_prior_day_plan_without_original_funding_is_not_mutated():
    ledger = {'execution': {'trading_day': '2000-01-01'}, 'open_orders': []}
    before = copy.deepcopy(ledger)
    with pytest.raises(SignalError):
        prepare_next_day_execution(ledger)
    assert ledger == before


def test_reconcile_only_records_final_fills_without_sending(tmp_path, monkeypatch):
    cli, fake, args = fixture(tmp_path, monkeypatch, initial_short=False)
    fake.auto_fill = False
    p = tmp_path/'config.json';config = json.loads(p.read_text());config['sell_wait_seconds'] = 0;p.write_text(json.dumps(config))
    assert cli.main(args) == 0
    assert len(fake.orders) == 1
    key = next(iter(fake.orders))
    fake.fill_order(key, shares=2)
    fake.orders[key]['status'] = 'Cancelled'
    assert cli.main(['--config', str(p), '--reconcile-only']) == 0
    ledger, _ = load_account_ledger(tmp_path)
    assert len(fake.orders) == 1
    assert ledger['strategies']['alpha']['2330'] == 8
    assert ledger['open_orders'][0]['applied_shares'] == 2
    assert cli.main(['--config', str(p), '--reconcile-only']) == 0
    assert load_account_ledger(tmp_path)[0]['strategies']['alpha']['2330'] == 8


def test_weekly_tick_launches_fresh_code_after_pull(tmp_path, monkeypatch):
    import order_desk.__main__ as cli
    from order_desk.capital import atomic_json
    now = datetime(2026, 11, 9, 10, tzinfo=ZoneInfo('Asia/Taipei'))
    monkeypatch.setattr(cli, 'taipei_now', lambda: now)
    monkeypatch.setattr(cli.TwseCalendar, 'is_trading_day', lambda self, day: True)
    config = {'schedule': {'enabled': True}}
    path = tmp_path/'config.json';atomic_json(path, config)
    def pull(root):
        atomic_json(root/'logs/last_pull.json', {'ok': True, 'trading_day': '2026-11-09'})
        return 0
    monkeypatch.setattr(cli, '_weekly_pull', pull)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs));return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cli.subprocess, 'run', run)
    assert cli._weekly_tick(path, config) == 0
    assert calls[0][0][-1] == '--weekly-order'
    assert calls[0][0][:3] == [cli.sys.executable, '-m', 'order_desk']
    assert calls[0][1]['cwd'] == tmp_path


def test_closing_tick_only_reconciles_and_records_completion(tmp_path, monkeypatch):
    import order_desk.__main__ as cli
    config = {'schedule': {'enabled': True}}
    path = tmp_path/'config.json';path.write_text(json.dumps(config))
    save_account_ledger(tmp_path, {'strategies': {}, 'open_orders': [], 'execution': {'trading_day': '2026-11-09'}}, {})
    monkeypatch.setattr(cli, 'taipei_now', lambda: datetime(2026,11,9,14,tzinfo=ZoneInfo('Asia/Taipei')))
    monkeypatch.setattr(cli.TwseCalendar, 'is_trading_day', lambda self, day: True)
    calls=[]
    monkeypatch.setattr(cli, '_fresh_process', lambda path, flag: calls.append(flag) or 0)
    assert cli._weekly_tick(path, config) == 0
    assert cli._weekly_tick(path, config) == 0
    assert calls == ['--reconcile-only']


def test_resume_requires_user_confirmation_and_failed_install_stays_paused(tmp_path, monkeypatch):
    import order_desk.__main__ as cli
    path=tmp_path/'config.json';config={'schedule': {'enabled': False}};path.write_text(json.dumps(config))
    calls=[]
    monkeypatch.setattr(cli, '_install_schedule', lambda path, config: calls.append('install') or 1)
    monkeypatch.setattr(cli, '_weekly_tick', lambda *args: calls.append('tick') or 0)
    assert cli.main(['--config', str(path), '--resume-schedule']) == 1
    assert calls == []
    assert cli.main(['--config', str(path), '--resume-schedule', '--confirm', 'APPROVE']) == 1
    assert json.loads(path.read_text())['schedule']['enabled'] is False
    assert calls == ['install']


def test_resume_command_installs_then_enables_and_checks_once(tmp_path, monkeypatch):
    import order_desk.__main__ as cli
    path=tmp_path/'config.json';path.write_text(json.dumps({'schedule': {'enabled': False}}))
    def install(path, config):
        assert json.loads(path.read_text())['schedule']['enabled'] is False
        return 0
    calls=[]
    monkeypatch.setattr(cli, '_install_schedule', install)
    monkeypatch.setattr(cli, '_weekly_tick', lambda p,c: calls.append(c['schedule']['enabled']) or 0)
    assert cli.main(['--config',str(path),'--resume-schedule','--confirm','APPROVE']) == 0
    assert calls == [True]
    assert json.loads(path.read_text())['schedule']['enabled'] is True


@pytest.mark.parametrize('inventory_short',[False,True])
def test_partial_prior_day_buy_retries_only_remaining_target_once(tmp_path,monkeypatch,inventory_short):
    from order_desk.books import load_strategy_entries,signal_signature
    from order_desk.deferrals import cycle_id
    cli,fake,args=fixture(tmp_path,monkeypatch,initial_short=False)
    config=json.loads((tmp_path/'config.json').read_text());config['inventory_mismatch_policy']='reconcile_actual'
    (tmp_path/'config.json').write_text(json.dumps(config))
    saved,paths=load_account_ledger(tmp_path)
    old=(datetime.fromisoformat(taipei_today())-timedelta(days=1)).date().isoformat()
    saved['strategies']={'alpha':{'6213':10,'2330':5}}
    order=_open_buy(old);order.update(ticker='2317',shares=5,limit_price=100,
        allocation=[{'strategy':'alpha','shares':5,'applied':0}])
    order['broker_snapshot']={'broker_order_id':'abc','ticker':'2317','action':'Buy',
        'order_lot':'IntradayOdd','status':'Cancelled','filled_shares':2,'trading_day':old}
    saved['open_orders']=[order]
    saved['execution']={'trading_day':old,'cycle_id':cycle_id(),'budgets':{'alpha':2000},
        'targets':{'alpha':{'6213':10,'2330':5,'2317':5}},
        'signal_signature':signal_signature(load_strategy_entries(config,tmp_path)),
        'orders':[],'allocations':{},'submitted':[]}
    fake._positions={'6213':5 if inventory_short else 10,'2330':5,'2317':2}
    save_account_ledger(tmp_path,saved,paths)
    assert cli.main(args)==0
    after,_=load_account_ledger(tmp_path)
    expected={'6213':7,'2330':3,'2317':3} if inventory_short else {'6213':10,'2330':5,'2317':5}
    assert after['strategies']['alpha']==expected
    if inventory_short:
        assert after['inventory_adjustment_history'][-1]['strategy_after']=={'alpha':5}
        assert sorted((o['ticker'],o['action'],o['shares']) for o in fake.orders.values())==[('2317','Buy',1),('2330','Sell',2),('6213','Buy',2)]
    else:
        assert [(o['ticker'],o['action'],o['shares']) for o in fake.orders.values()]==[('2317','Buy',3)]
    count=len(fake.orders)
    # Simulate a scheduler retry after its external completion stamp was lost.
    monkeypatch.setattr(cli,'_refuse_weekly_order',lambda *a:0)
    assert cli.main(['--config',str(tmp_path/'config.json'),'--weekly-order'])==0
    assert len(fake.orders)==count
    assert len([o for o in load_account_ledger(tmp_path)[0]['order_history'] if o['broker_order_id']=='abc'])==1


def test_tuesday_schedule_retries_working_monday_without_retrying_complete(tmp_path):
    from order_desk.schedule import due_actions,schedule_slots,write_order_stamp,order_stamp_blocks_retry
    now=datetime(2026,11,10,9,20,tzinfo=ZoneInfo('Asia/Taipei'))
    stamp=tmp_path/'last_order.json'
    write_order_stamp(stamp,week='2026-11-09',trading_day='2026-11-09',status='working')
    done=order_stamp_blocks_retry(stamp,now.date())
    assert due_actions(now,schedule_slots({}),pull_done=True,order_done=done,is_trading_day=lambda d:True)==['order']
    write_order_stamp(stamp,week='2026-11-09',trading_day='2026-11-10',status='completed')
    assert due_actions(now,schedule_slots({}),pull_done=True,order_done=order_stamp_blocks_retry(stamp,now.date()),is_trading_day=lambda d:True)==[]


def test_completion_guard_does_not_hide_active_work_or_next_cycle():
    from order_desk.execution import cycle_already_completed
    l={'execution_history':[{'cycle_id':'2026-11-09','outcome':'completed'}]}
    assert cycle_already_completed(l,'2026-11-09')
    assert not cycle_already_completed(l,'2026-11-16')
    l['execution']={'trading_day':'2026-11-10'}
    assert not cycle_already_completed(l,'2026-11-09')
    del l['execution'];l['open_orders']=[{'status':'Unconfirmed'}]
    assert not cycle_already_completed(l,'2026-11-09')
