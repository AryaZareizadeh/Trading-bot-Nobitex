const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0]
  .replace(/checkSession\(\)\.then\(active => \{ if \(active\) openDashboard\(\); \}\)[\s\S]*$/, '');

function dashboard() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {value: '', textContent: '', innerHTML: '', style: {}, dataset: {}, options: []});
    return elements.get(id);
  };
  const document = {
    getElementById: element,
    querySelectorAll: () => [],
    addEventListener: () => {},
  };
  const context = vm.createContext({
    document,
    window: {fetch: () => {}},
    console,
    setTimeout: callback => { callback(); return 1; },
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    Date,
  });
  vm.runInContext(script, context);
  return {context, element, run: code => vm.runInContext(code, context)};
}

test('bot search and status filter narrow cards while showing a visible count', () => {
  const page = dashboard();
  page.run(`bots = [
    {id:'a', name:'Alpha', symbol:'BTCUSDT', strategy:'AdvRSI', timeframe:'60', mode:'simulation', status:'running', total_pnl:2, credit:100, leverage:2, last_updated:'2026-09-23T00:00:00Z'},
    {id:'b', name:'Beta', symbol:'ETHUSDT', strategy:'MACD', timeframe:'60', mode:'real', status:'error', error:'API <down>', total_pnl:-1, credit:100, leverage:2}
  ]`);
  page.element('botSearch').value = 'eth';
  page.element('botStatusFilter').value = 'error';
  page.run('renderBotsBox()');
  assert.match(page.element('boxBotsHeader').textContent, /1.*2/);
  assert.match(page.element('boxBotsBody').innerHTML, /Beta/);
  assert.doesNotMatch(page.element('boxBotsBody').innerHTML, /Alpha/);
  assert.match(page.element('boxBotsBody').innerHTML, /API &lt;down&gt;/);
  assert.match(page.element('boxBotsBody').innerHTML, /History/);
});

test('optimizer workload previews evaluation budget and held-out split', () => {
  const page = dashboard();
  const workload = page.run('_optimizerWorkload(1000, 30, 2, 4, 70)');
  assert.equal(workload.evaluations, 2480);
  assert.equal(workload.trainCandles, 700);
  assert.equal(workload.testCandles, 300);
  assert.equal(workload.cores, 2);
});

test('optimizer forms show the workload for their selected strategy and worker setting', () => {
  const page = dashboard();
  page.run(`optimizerConfig = {
    default_cores: 2,
    available_strategies: [{value:'AdvRSI', params:[{name:'rsi_length'}, {name:'rsi_ma_length'}]}]
  }`);
  for (const prefix of ['o', 'ot']) {
    page.element(prefix + 'Strategy').value = 'AdvRSI';
    page.element(prefix + 'Candles').value = '1000';
    page.element(prefix + 'Maxiter').value = '30';
    page.element(prefix + 'Cores').value = '0';
  }
  page.element('otTrainPct').value = '70';
  page.run('updateOptimizerNote(); _updateTesterRunNote()');
  assert.match(page.element('oWorkload').innerHTML, /1,240/);
  assert.match(page.element('oWorkload').innerHTML, /2<\/strong> workers/);
  assert.match(page.element('otWorkload').innerHTML, /700<\/strong> train/);
  assert.match(page.element('otWorkload').innerHTML, /300<\/strong> test/);
});

test('live optimizer charts refresh when the best backtest changes', () => {
  const page = dashboard();
  page.run(`window.chartCalls = []; window.rangeRestores = 0;
    _buildOptChart = (outer, data, trades, title, charts) => {
      window.chartCalls.push({revision: data.revision, title, trades: trades.length});
      charts.push({remove() {}, timeScale() { return {
        getVisibleLogicalRange() { return {from: 0, to: 10}; },
        setVisibleLogicalRange() { window.rangeRestores++; }
      }; }});
    };`);
  page.run(`var liveJob = {
    status: 'running', best_trades: [{_start_ts: 1, _end_ts: 2}],
    chart_data: {strategy: 'AdvRSI', revision: 1, candles: [{time: 1}, {time: 2}]}
  };`);
  page.run('renderOptChart(liveJob); renderOptChart(liveJob)');
  assert.equal(page.context.window.chartCalls.length, 1);
  page.run('liveJob.chart_data.revision = 2; renderOptChart(liveJob)');
  assert.equal(page.context.window.chartCalls.length, 2);
  page.run("liveJob.status = 'done'; renderOptChart(liveJob)");
  assert.equal(page.context.window.chartCalls.length, 3);

  page.run("liveJob.status = 'running'; liveJob.test_chart_data = null; renderOptTesterCharts(liveJob)");
  assert.equal(page.context.window.chartCalls.length, 4);
  page.run('liveJob.chart_data.revision = 3; renderOptTesterCharts(liveJob)');
  assert.equal(page.context.window.chartCalls.length, 5);
  page.run("liveJob.status = 'done'; liveJob.test_chart_data = {strategy:'AdvRSI', candles:[{time: 3}]}; renderOptTesterCharts(liveJob)");
  assert.equal(page.context.window.chartCalls.length, 7);
  assert.equal(page.context.window.rangeRestores, 4);
});

test('only a completed held-out run offers applying its params to a bot', () => {
  const page = dashboard();
  page.run(`renderOptimizerTesterResults({
    status:'done', symbol:'btcusdt', strategy:'AdvRSI', timeframe:'60',
    best_params:{rsi_length:14}, best_summary:{total_pnl:12, win_rate:55, trade_count:4},
    baseline:{total_pnl:3, win_rate:45, trade_count:4},
    test_summary:{total_pnl:5, win_rate:50, trade_count:2},
    test_baseline:{total_pnl:1, win_rate:40, trade_count:2},
    params:[], train_candle_count:700, test_candle_count:300
  })`);
  assert.match(page.element('otResults').innerHTML, /Training vs held-out/);
  assert.match(page.element('otResults').innerHTML, /Apply to New Bot/);
  assert.equal(page.context.window._lastOptimizerTesterJob.best_params.rsi_length, 14);
  page.run(`renderOptimizerTesterResults({
    status:'cancelled', symbol:'btcusdt', strategy:'AdvRSI', timeframe:'60',
    best_params:{rsi_length:14}, best_summary:{total_pnl:12, win_rate:55, trade_count:4},
    baseline:{}, params:[]
  })`);
  assert.doesNotMatch(page.element('otResults').innerHTML, /Apply to New Bot/);
});

test('applying optimizer inputs opens a simulation bot draft', () => {
  const page = dashboard();
  page.run(`window._lastOptimizerJob = {
    status:'done', symbol:'btcusdt', strategy:'AdvRSI', timeframe:'60',
    credit:100, leverage:2, best_params:{rsi_length:14}
  };
  populateSymbolSelect = () => {};
  refreshCreateBudget = () => {};`);
  page.element('fSymbol').options = [];
  page.element('fTimeframe').options = [];
  page.element('fModeSim').checked = false;
  page.element('fModeReal').checked = true;
  page.element('createModal').classList = {add: () => {}};
  page.run('applyOptimizerToBot()');
  assert.equal(page.element('fModeSim').checked, true);
  assert.equal(page.element('fModeReal').checked, false);
});

test('portfolio tab renders allocation, history and escaped exchange data', () => {
  const page = dashboard();
  page.run(`renderPortfolio({
    snapshot:{captured_at:'2026-09-24T00:00:00Z',equity_usdt:1220,spot_usdt:1100,
      margin_exposure_usdt:100,valuation_complete:true,unpriced:[],
      holdings:[{currency:'BTC',wallet:'spot',amount:1,value_usdt:100,weight_pct:8.2}],
      positions:[{symbol:'BTCUSDT',side:'buy',exposure_usdt:100,unrealized_pnl_usdt:20}]},
    history:{return_pct:8,net_pnl_usdt:10,max_drawdown_pct:2,
      points:[{time:'2026-09-22',equity_usdt:1100},{time:'2026-09-24',equity_usdt:1220}]},
    cashflows:[{id:1,occurred_at:'2026-09-23T00:00:00Z',amount_usdt:50,note:'<script>bad</script>'}],
    targets:{BTC:.2},rebalance_ideas:[]
  })`);
  assert.match(page.element('portfolioMetrics').innerHTML, /1,220 USDT/);
  assert.match(page.element('portfolioEquityChart').innerHTML, /<svg/);
  assert.match(page.element('portfolioFlows').innerHTML, /&lt;script&gt;bad&lt;\/script&gt;/);
});
