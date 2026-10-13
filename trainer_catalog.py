"""Model Trainer capabilities and defaults, without importing optional runtimes."""
from copy import deepcopy
import importlib.util


ALEXNET = 'alexnet1d-v1'
TRANSFORMER = 'transformer-ohlcv-v1'
PRICE_AI = 'price-ai-hgbr-v1'
AGENTIC_AI = 'agentic-ai-mtf-hgb-v1'
SPOT_HGB = 'spot-hgb-v1'
SPOT_LIGHTGBM = 'spot-lightgbm-v1'
NEURAL = {ALEXNET, TRANSFORMER}
TABULAR = {PRICE_AI, AGENTIC_AI, SPOT_HGB, SPOT_LIGHTGBM}
DEFAULTS = dict(symbols=['btcusdt'], timeframes=['60'], candles=2000, lookback=96,
                horizon=4, epochs=10, batch_size=32, learning_rate=.001, device='cuda')
TIMEFRAMES = [{'value': '15', 'label': '15m'}, {'value': '60', 'label': '1h'},
              {'value': '240', 'label': '4h'}, {'value': 'D', 'label': '1d'}]

_DEFINITIONS = [
    dict(id=ALEXNET, label='AlexNet', description='Convolutional forecast of one future closing price.',
         requires_torch=True, bot_compatible=True, supported_timeframes=['15', '60', '240', 'D'],
         defaults={**DEFAULTS}),
    dict(id=TRANSFORMER, label='Transformer',
         description='100 completed OHLCV candles → 10 complete candles, generated one by one.',
         requires_torch=True, bot_compatible=False, supported_timeframes=['15', '60', '240', 'D'],
         defaults={**DEFAULTS, 'candles': 10000, 'lookback': 100, 'horizon': 10,
                   'epochs': 30, 'batch_size': 16, 'learning_rate': .0003}),
    dict(id=PRICE_AI, label='PriceAI', description='CPU gradient boosting forecast of four-candle returns.',
         requires_torch=False, bot_compatible=False, supported_timeframes=['60', '240'],
         defaults={**DEFAULTS, 'candles': 3000, 'horizon': 4, 'epochs': 1, 'device': 'cpu'}),
    dict(id=AGENTIC_AI, label='Agentic AI',
         description='Multiple timeframe specialists with calibrated probabilities for cost-adjusted moves.',
         requires_torch=False, bot_compatible=False, supported_timeframes=['60', '240'],
         defaults={**DEFAULTS, 'candles': 10000, 'horizon': 4, 'epochs': 1, 'device': 'cpu',
                   'specialist_timeframes': ['60', '240'], 'fee_bps': 10.,
                   'exit_fee_bps': 10., 'slippage_bps': 5.}),
    dict(id=SPOT_HGB, label='Spot HGB',
         description='Hourly return and downside forecasts at 4, 24 and 72 hours with BTC context.',
         requires_torch=False, bot_compatible=False, supported_timeframes=['60'],
         defaults={**DEFAULTS, 'candles': 10000, 'horizon': 72, 'epochs': 1, 'device': 'cpu'}),
    dict(id=SPOT_LIGHTGBM, label='Spot LightGBM',
         description='Optional LightGBM return and downside forecasts at 4, 24 and 72 hours.',
         requires_torch=False, bot_compatible=False, supported_timeframes=['60'],
         defaults={**DEFAULTS, 'candles': 10000, 'horizon': 72, 'epochs': 1, 'device': 'cpu'}),
]


def definition(architecture):
    for item in _DEFINITIONS:
        if item['id'] == architecture:
            return deepcopy(item)
    raise ValueError('Unsupported model architecture')


def _installed(package):
    try:
        return importlib.util.find_spec(package) is not None
    except (ImportError, ValueError):
        return False


def model_catalog(runtime):
    models = deepcopy(_DEFINITIONS)
    for item in models:
        item['defaults']['architecture'] = item['id']
        item['min_candles'] = {PRICE_AI: 650, AGENTIC_AI: 1500,
                               SPOT_HGB: 800, SPOT_LIGHTGBM: 800}.get(item['id'], 500)
        error = None
        if item['requires_torch'] and not runtime.get('torch_available'):
            error = runtime.get('error') or 'Install PyTorch using requirements-training.txt, then restart the server.'
        elif not item['requires_torch'] and not _installed('sklearn'):
            error = 'Install scikit-learn in the server Python environment, then restart the server.'
        elif item['id'] == SPOT_LIGHTGBM and not _installed('lightgbm'):
            error = 'Install lightgbm in the server Python environment, then restart the server.'
        item.update(available=error is None, error=error)
    return models


def artifact_suffix(architecture):
    definition(architecture)
    return '.pt' if architecture in NEURAL else '.joblib'
