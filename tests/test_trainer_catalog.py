from copy import deepcopy
import asyncio
import sys
from types import SimpleNamespace

import pytest

from model_trainer import ModelTrainerManager, validate_config
from storage import Store


def test_transformer_defaults_are_sequence_forecast():
    config = validate_config({'architecture': 'transformer-ohlcv-v1'})
    assert (config['lookback'], config['horizon'], config['candles']) == (100, 10, 10000)
    with pytest.raises(ValueError, match='100'):
        validate_config({'architecture': 'transformer-ohlcv-v1', 'lookback': 96})


@pytest.mark.parametrize('raw', [
    {'architecture': 'unknown'},
    {'architecture': 'price-ai-hgbr-v1', 'timeframes': ['15']},
    {'architecture': 'agentic-ai-mtf-hgb-v1', 'specialist_timeframes': ['240', 'D']},
    {'architecture': 'agentic-ai-mtf-hgb-v1', 'fee_bps': 0},
    {'architecture': 'spot-hgb-v1', 'timeframes': ['240']},
    {'architecture': 'spot-hgb-v1', 'symbols': ['unknownusdt']},
    {'architecture': 'price-ai-hgbr-v1', 'candles': 500},
    {'architecture': 'agentic-ai-mtf-hgb-v1', 'candles': 1000},
    {'architecture': 'spot-hgb-v1', 'candles': 500},
])
def test_family_specific_requirements(raw):
    with pytest.raises(ValueError):
        validate_config(raw)


def test_four_hour_agent_has_causal_specialist_defaults():
    config = validate_config({'architecture': 'agentic-ai-mtf-hgb-v1', 'timeframes': ['240']})
    assert config['specialist_timeframes'] == ['240', 'D']
    assert config['device'] == 'cpu'


def test_catalog_exposes_all_pipelines_without_torch(tmp_path):
    manager = ModelTrainerManager(Store(tmp_path / 'store.sqlite3'), runtime=lambda: {'torch_available': False})
    config = manager.get_config()
    families = {item['id']: item for item in config['models']}
    assert len(families) == 6
    assert families['price-ai-hgbr-v1']['available']
    assert not families['transformer-ohlcv-v1']['available']
    families['transformer-ohlcv-v1']['defaults']['symbols'].append('ethusdt')
    assert manager.get_config()['models'][1]['defaults']['symbols'] == ['btcusdt']


def test_tree_api_uses_family_defaults_instead_of_neural_defaults(tmp_path):
    from fastapi.testclient import TestClient
    from bot_manager import BotManager
    from server import create_app
    app = create_app(manager=BotManager(store=Store(tmp_path / 'store.sqlite3'), legacy_dir=tmp_path),
                     offline=True, security=False)
    captured = []
    app.state.model_trainer.start_job = lambda raw: captured.append(deepcopy(raw)) or 'test'
    with TestClient(app) as client:
        response = client.post('/api/model-trainer/run', json={
            'architecture': 'transformer-ohlcv-v1', 'symbols': ['btcusdt'], 'timeframes': ['60']})
        assert response.status_code == 202
    config = validate_config(captured[0])
    assert config['lookback'] == 100
    assert config['horizon'] == 10
    assert config['batch_size'] == 16


def test_cpu_tree_training_dispatches_without_torch_and_roundtrips_registry(tmp_path, monkeypatch):
    packages = {}
    def train(df, config, **kwargs):
        kwargs['progress']({'epoch': 1, 'history': []})
        return {'metadata': {'validation_passed': True}, 'config': dict(config)}
    def save(package, path):
        packages[str(path)] = deepcopy(package)
        path.write_bytes(b'private model')
    adapters = SimpleNamespace(train_tabular=train, save_checkpoint=save,
        load_checkpoint=lambda path: packages[str(path)],
        predict_tabular=lambda package, **kw: {'architecture': package['config']['architecture'], 'predicted_price': 101.})
    monkeypatch.setitem(sys.modules, 'trainer_adapters', adapters)
    manager = ModelTrainerManager(Store(tmp_path / 'store.sqlite3'),
        fetcher=lambda s, t, n: list(range(n)), runtime=lambda: {'torch_available': False})
    async def run():
        job_id = manager.start_job({'architecture': 'price-ai-hgbr-v1'})
        await manager._tasks[job_id]
        job = manager.get_job(job_id)
        assert job['status'] == 'done', job
        model = manager.list_models()[0]
        assert model['architecture'] == 'price-ai-hgbr-v1'
        assert model['bot_compatible'] is False
        assert manager.model_path(model['id']).suffix == '.joblib'
        assert manager.predict(model['id'])['predicted_price'] == 101.
        packages[str(manager.model_path(model['id']))]['metadata']['architecture'] = 'spot-hgb-v1'
        with pytest.raises(ValueError, match='registry'):
            manager.predict(model['id'])
    asyncio.run(run())


def test_transformer_preview_includes_observed_history_and_future_candles(tmp_path, monkeypatch):
    import pandas as pd
    frame = pd.DataFrame({'Open': 100., 'High': 102., 'Low': 98., 'Close': 101., 'Volume': 5.},
                         index=pd.date_range('2026-01-01', periods=102, freq='h'))
    model_id = 'b' * 32
    record = dict(id=model_id, status='ready', created_at='2026-01-01', source='nobitex',
        symbol='btcusdt', timeframe='60', architecture='transformer-ohlcv-v1', lookback=100,
        horizon=10, horizon_candles=10)
    forecast = [dict(time=(frame.index[-1].tz_localize('UTC') + pd.Timedelta(hours=i)).isoformat(),
                     open=101., high=104., low=100., close=103., volume=6.) for i in range(1, 11)]
    module = SimpleNamespace(load_checkpoint=lambda path: SimpleNamespace(metadata=record),
                             predict_transformer=lambda model, df: forecast)
    monkeypatch.setitem(sys.modules, 'transformer_model', module)
    manager = ModelTrainerManager(Store(tmp_path / 'store.sqlite3'), fetcher=lambda *args: frame)
    manager.store.put('trained_models', model_id, record)
    manager.model_dir.mkdir()
    (manager.model_dir / f'{model_id}.pt').write_bytes(b'weights')
    preview = manager.predict(model_id)
    assert len(preview['observed_candles']) == 100
    assert preview['forecast_candles'] == forecast
    assert preview['target_time'] == (pd.Timestamp(forecast[-1]['time']) + pd.Timedelta(hours=1)).isoformat()


def test_transformer_job_actual_checkpoint_and_preview(tmp_path):
    import numpy as np
    import pandas as pd
    torch = pytest.importorskip('torch')
    old_threads = torch.get_num_threads()
    torch.set_num_threads(1)
    t = np.arange(500)
    close = 100 * np.exp(.02 * np.sin(t / 13))
    frame = pd.DataFrame({'Open': close * .999, 'High': close * 1.004,
                         'Low': close * .995, 'Close': close, 'Volume': 100 + 10 * np.sin(t / 7)},
                        index=pd.date_range('2025-01-01', periods=len(t), freq='h'))
    manager = ModelTrainerManager(Store(tmp_path / 'store.sqlite3'),
        fetcher=lambda s, tf, count: frame.tail(count))
    async def run():
        job_id = manager.start_job({'architecture': 'transformer-ohlcv-v1', 'device': 'cpu',
                                   'candles': 500, 'epochs': 1})
        await manager._tasks[job_id]
        job = manager.get_job(job_id)
        assert job['status'] == 'done', job
        preview = manager.predict(job['model_ids'][0])
        assert len(preview['observed_candles']) == 100
        assert len(preview['forecast_candles']) == 10
        assert pd.Timestamp(preview['forecast_candles'][0]['time']) == pd.Timestamp(preview['observed_close_time'])
        assert all(c['low'] <= min(c['open'], c['close']) <= max(c['open'], c['close']) <= c['high']
                   and c['volume'] >= 0 for c in preview['forecast_candles'])
    try:
        asyncio.run(run())
    finally:
        torch.set_num_threads(old_threads)
