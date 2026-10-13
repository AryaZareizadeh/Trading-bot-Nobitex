import asyncio
from dataclasses import replace

from bot import BotConfig, BotStatus
from bot_manager import BotManager
from storage import Store


def manager_at(tmp_path):
    return BotManager(Store(tmp_path / 'history.db'), tmp_path)


def trade(pnl='4'):
    return {'Trade #': 1, 'Position': 'Long', 'Start Time': '2026-10-01 00:00:00',
            'End Time': '2026-10-01 01:00:00', 'Start Price': '100',
            'End Price': '104', 'P&L ($)': pnl}


def test_creation_and_full_activity_survive_restart_and_are_private(tmp_path):
    manager = manager_at(tmp_path)
    bot = manager.create_bot(BotConfig('btcusdt', 'AdvRSI', '60', 100))
    other = manager.create_bot(BotConfig('ethusdt', 'AdvRSI', '60', 100))
    for i in range(205):
        bot._log_event('info', str(i))
    bot._persist()
    manager.load()
    detail = manager.get_bot(bot.id).to_detail_dict()
    assert len(detail['history_events']) == 206
    assert detail['history_events'][-1]['event'] == 'bot_created'
    assert detail['history_events'][-1]['time'] == detail['created_at']
    assert all(e['bot_id'] == bot.id for e in detail['history_events'])
    assert len(manager.get_bot(other.id).to_detail_dict()['history_events']) == 1


def test_running_snapshot_records_interruption_once_on_load(tmp_path):
    manager = manager_at(tmp_path)
    bot = manager.create_bot(BotConfig('btcusdt', 'AdvRSI', '60', 100))
    bot.status = BotStatus.RUNNING
    bot._log_event('bot_started')
    bot._persist()
    manager.load()
    manager.load()
    detail = manager.get_bot(bot.id).to_detail_dict()
    assert detail['status'] == 'idle'
    assert sum(e['event'] == 'runtime_interrupted' for e in detail['history_events']) == 1


def test_start_is_durable_before_first_candle_and_stop_is_idempotent(tmp_path, monkeypatch):
    manager = manager_at(tmp_path)
    bot = manager.create_bot(BotConfig('btcusdt', 'AdvRSI', '60', 100))
    async def waiting():
        await asyncio.Event().wait()
    monkeypatch.setattr(bot, '_run_loop', waiting)
    async def run():
        await bot.start()
        assert manager.store.get('runtime', bot.id)['desired_status'] == 'running'
        await bot.stop()
        await bot.stop()
    asyncio.run(run())
    events = bot.to_detail_dict()['history_events']
    assert sum(e['event'] == 'bot_stopped' for e in events) == 1


def test_trades_update_without_duplicates_and_survive_reconfiguration(tmp_path):
    manager = manager_at(tmp_path)
    bot = manager.create_bot(BotConfig('btcusdt', 'AdvRSI', '60', 100))
    bot.completed_trades = [trade('0')]
    bot._persist()
    bot.completed_trades = [trade()]
    bot.closed_trade_count = 1
    bot._persist()
    bot._persist()
    assert len(bot.to_detail_dict()['trade_history']) == 1
    bot = manager.update_bot(bot, replace(bot.cfg, symbol='ethusdt'))
    bot.completed_trades = [trade('-2')]
    bot.closed_trade_count = 1
    bot._persist()
    manager.load()
    history = manager.get_bot(bot.id).to_detail_dict()['trade_history']
    assert len(history) == 2
    assert {t['symbol'] for t in history} == {'BTCUSDT', 'ETHUSDT'}
    assert {t['trade']['P&L ($)'] for t in history} == {'4', '-2'}
    assert all(t['status'] == 'closed' for t in history)


def test_dual_history_survives_changing_bot_type(tmp_path):
    manager = manager_at(tmp_path)
    bot = manager.create_bot(BotConfig('btcusdt', 'AdvRSIIndependent', '60', 200,
                                      long_credit=100, short_credit=100))
    bot.sides['long'].completed_trades = [trade()]
    bot.sides['long'].closed_trade_count = 1
    bot.sides['long']._persist()
    child_id = bot.sides['long'].id
    bot.sides['long']._log_event('position_opened', 'long')
    bot = manager.update_bot(bot, replace(bot.cfg, strategy_name='AdvRSI',
                                          long_credit=None, short_credit=None))
    detail = bot.to_detail_dict()
    assert len(detail['trade_history']) == 1
    assert detail['trade_history'][0]['side'] == 'long'
    assert any(e['bot_id'] == child_id for e in detail['history_events'])


def test_real_history_uses_exchange_evidence_and_keeps_other_bots_private(tmp_path):
    manager = manager_at(tmp_path)
    bot = manager.create_bot(BotConfig('btcusdt', 'AdvRSI', '60', 100, mode='real'))
    manager.store.put('orders', 'own', {'bot_id': bot.id, 'state': 'uncertain', 'created_at': '2026-10-10T00:00:00Z'})
    manager.store.put('orders', 'other', {'bot_id': 'other', 'state': 'rejected'})
    manager.store.put('closed_positions', 'position', {'bot_id': bot.id, 'position': {'id': 42, 'PNL': '-3.12'}})
    bot.completed_trades = [trade('999')]
    detail = bot.to_detail_dict()
    assert detail['trade_history'] == []
    assert [o['state'] for o in detail['order_ledger']] == ['uncertain']
    assert detail['closed_exchange_positions'] == [{'id': 42, 'PNL': '-3.12'}]


def test_legacy_import_does_not_invent_creation_time(tmp_path):
    import json
    from dataclasses import asdict
    (tmp_path / 'bots.json').write_text(json.dumps([asdict(BotConfig('btcusdt', 'AdvRSI', '60', 100, id='legacy'))]))
    manager = manager_at(tmp_path)
    manager.load()
    detail = manager.get_bot('legacy').to_detail_dict()
    assert detail['created_at'] is None
    assert not any(e['event'] == 'bot_created' for e in detail['history_events'])


def test_runtime_failure_records_down_event(tmp_path, monkeypatch):
    manager = manager_at(tmp_path)
    bot = manager.create_bot(BotConfig('btcusdt', 'AdvRSI', '60', 100))
    async def fail():
        raise ValueError('invalid candles')
    async def no_delay(_):
        return None
    monkeypatch.setattr(bot, '_refresh_data', fail)
    monkeypatch.setattr('bot.asyncio.sleep', no_delay)
    asyncio.run(bot._run_loop())
    assert bot.status == BotStatus.ERROR
    assert any(e['event'] == 'bot_down' for e in bot.to_detail_dict()['history_events'])


def test_open_simulation_trade_is_reset_by_edit_without_booked_profit(tmp_path):
    manager = manager_at(tmp_path)
    bot = manager.create_bot(BotConfig('btcusdt', 'AdvRSI', '60', 100))
    bot.completed_trades = [trade('8')]
    bot._persist()
    manager.update_bot(bot, replace(bot.cfg, credit=200))
    manager.load()
    history = manager.get_bot(bot.id).to_detail_dict()['trade_history']
    assert len(history) == 1
    assert history[0]['status'] == 'reset'


def test_failed_edit_does_not_mark_a_trade_as_reset(tmp_path, monkeypatch):
    import pytest
    manager = manager_at(tmp_path)
    bot = manager.create_bot(BotConfig('btcusdt', 'AdvRSI', '60', 100))
    bot.completed_trades = [trade('8')]
    bot._persist()
    original_write = manager.store.write
    def fail_config(conn, namespace, key, value):
        if namespace == 'bots':
            raise RuntimeError('disk failure')
        return original_write(conn, namespace, key, value)
    monkeypatch.setattr(manager.store, 'write', fail_config)
    with pytest.raises(RuntimeError, match='disk failure'):
        manager.update_bot(bot, replace(bot.cfg, credit=200))
    assert bot.cfg.credit == 100
    saved = list(manager.store.list('bot_trades').values())
    assert saved[0]['status'] == 'open'
    assert not any(e['event'] == 'reconfigured' for e in bot.to_detail_dict()['history_events'])
