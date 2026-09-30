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

test('bots wallet ribbon displays margin balances without a spot section', async () => {
  const page = dashboard();
  page.element('walletBar').classList = {remove: () => {}};
  page.run(`apiFetch = async () => ({json: async () => ({
    ok: true,
    margin: {USDT: {balance: '100', blocked: '25'}, RLS: {balance: '400'}},
    spot: {BTC: {balance: '2'}},
    usdt_available: 75, usdt_locked_in: 25
  })})`);
  await page.run('fetchWallet()');
  const ribbon = page.element('walletBar').innerHTML;
  assert.match(ribbon, /75\.0000/);
  assert.match(ribbon, /RLS/);
  assert.doesNotMatch(ribbon, /Spot|BTC/);
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
  assert.match(page.element('oRunNote').textContent, /Next run:/);
  assert.match(page.element('otWorkload').innerHTML, /700<\/strong> train/);
  assert.match(page.element('otWorkload').innerHTML, /300<\/strong> test/);
});

test('optimizer Run card shows the saved job settings and actual progress separately from the editable estimate', () => {
  const page = dashboard();
  page.element('oWorkload').innerHTML = '<span>14 workers in the editable estimate</span>';
  page.run(`renderOptimizerProgress({
    id:'run-42', status:'cancelled', created_at:1750000000,
    symbol:'BTCUSDT', strategy:'AdvRSI', strategy_label:'Advanced RSI Combined', timeframe:'60',
    candles:2200, candle_count:2150, credit:100, leverage:5,
    objective:'balanced', min_win_rate:55, min_closed_trades:30,
    requested_cores:14, workers:12, maxiter:5000, iteration:26, evaluations:5940,
    current_delta:0.125, best_pnl:18.25, best_summary:{win_rate:60, trade_count:35},
    custom_bounds:{rsi_length:[7,21]}, search_bounds:{rsi_length:[8,21]}, random_seed:42,
    execution:{entry_fee_rate:0.001, exit_fee_rate:0.002, slippage_bps:5,
      holding_rate_per_day:0.0005, holding_policy:'entry_notional_daily',
      fill_timing:'next_open', position_mode:'net', margin_fraction:0.8,
      liquidation_ratio:1.1, max_hold_days:10}
  })`);
  const run = page.element('oCurrentRun').innerHTML;
  assert.match(run, /run-42/);
  assert.match(run, /BTCUSDT.*Advanced RSI Combined.*1h/);
  assert.match(run, /2,200.*2,150/);
  assert.match(run, /55.*30/);
  assert.match(run, /14 requested.*12 actual/);
  assert.match(run, /5,000.*26.*5,940/);
  assert.match(run, /rsi_length.*8.*21/);
  assert.match(run, /Entry fee.*0\.100.*Exit fee.*0\.200/);
  assert.match(run, /Seed.*42/);
  assert.match(page.element('oWorkload').innerHTML, /14 workers in the editable estimate/);
  assert.match(page.element('oProgressCurrent').textContent, /Cancelled/);
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
  page.run("liveJob.status = 'pausing'; renderOptChart(liveJob); liveJob.status = 'paused'; renderOptChart(liveJob)");
  assert.equal(page.context.window.chartCalls.length, 1);
  page.run('liveJob.chart_data.revision = 2; renderOptChart(liveJob)');
  assert.equal(page.context.window.chartCalls.length, 2);
  page.run("liveJob.status = 'done'; renderOptChart(liveJob)");
  assert.equal(page.context.window.chartCalls.length, 3);

  page.run("liveJob.status = 'running'; liveJob.test_chart_data = null; renderOptTesterCharts(liveJob)");
  assert.equal(page.context.window.chartCalls.length, 4);
  page.run("liveJob.status = 'paused'; renderOptTesterCharts(liveJob)");
  assert.equal(page.context.window.chartCalls.length, 4);
  page.run('liveJob.chart_data.revision = 3; renderOptTesterCharts(liveJob)');
  assert.equal(page.context.window.chartCalls.length, 5);
  page.run("liveJob.status = 'done'; liveJob.test_chart_data = {strategy:'AdvRSI', candles:[{time: 3}]}; renderOptTesterCharts(liveJob)");
  assert.equal(page.context.window.chartCalls.length, 7);
  assert.equal(page.context.window.rangeRestores, 4);
});

test('optimizer progress shows four generation plots and missing eligible metrics honestly', () => {
  const page = dashboard();
  page.run(`renderDeltaCard('oDeltaSection', 'oDeltaChart', {
    status:'running', iteration:3, maxiter:50, current_delta:2, delta_tol:0.01,
    history:[
      {iteration:1, delta:0.2, pnl:null, win_rate:null, trade_count:null},
      {iteration:2, delta:0.8, pnl:12, win_rate:55, trade_count:30},
      {iteration:3, delta:2, pnl:18, win_rate:60, trade_count:32}
    ]
  })`);
  const chart = page.element('oDeltaChart').innerHTML;
  for (const title of ['Convergence', 'Best net P&L', 'Win rate', 'Closed trades']) {
    assert.match(chart, new RegExp(title));
  }
  assert.equal((chart.match(/<svg /g) || []).length, 4);
  assert.match(chart, /gen 1/);
  assert.match(chart, /\$18\.00/);
  page.run(`renderDeltaCard('otDeltaSection', 'otDeltaChart', {
    status:'running', iteration:1, maxiter:50, current_delta:0.2,
    history:[{iteration:1, delta:0.2, pnl:null, win_rate:null, trade_count:null}]
  })`);
  assert.match(page.element('otDeltaChart').innerHTML, /No eligible candidate yet/);
});

test('paused optimizer views keep chart and table DOM until their displayed data changes', () => {
  const page = dashboard();
  const writes = {oDeltaChart: 0, otDeltaChart: 0, oResults: 0, otResults: 0};
  for (const id of Object.keys(writes)) {
    const el = page.element(id);
    let value = '';
    Object.defineProperty(el, 'innerHTML', {
      get: () => value,
      set: next => { value = next; writes[id]++; },
    });
  }
  page.run(`var pausedJob = {
    id:'job-1', status:'running', iteration:2, maxiter:50, evaluations:100,
    current_delta:0.2, delta_tol:0.01, symbol:'btcusdt', strategy:'AdvRSI', timeframe:'60',
    candle_count:500, train_candle_count:350, objective:'profit', best_pnl:12,
    best_summary:{total_pnl:12, win_rate:60, trade_count:1},
    baseline:{total_pnl:2, win_rate:50, trade_count:1},
    params:[{name:'rsi_length', value:14, default:20, low:5, high:40}],
    best_trades:[{'Trade #':0, Position:'Long', 'P&L ($)':12}],
    history:[{iteration:1,delta:0.3,pnl:10,win_rate:50,trade_count:1},
             {iteration:2,delta:0.2,pnl:12,win_rate:60,trade_count:1}]
  };
  renderDeltaCard('oDeltaSection','oDeltaChart',pausedJob);
  renderDeltaCard('otDeltaSection','otDeltaChart',pausedJob);
  renderOptimizerResults(pausedJob);
  renderOptimizerTesterResults(pausedJob);`);
  assert.deepEqual(writes, {oDeltaChart: 1, otDeltaChart: 1, oResults: 1, otResults: 1});

  page.run(`pausedJob = JSON.parse(JSON.stringify(pausedJob));
    pausedJob.status = 'paused';
    renderDeltaCard('oDeltaSection','oDeltaChart',pausedJob);
    renderDeltaCard('otDeltaSection','otDeltaChart',pausedJob);
    renderOptimizerResults(pausedJob);
    renderOptimizerTesterResults(pausedJob);`);
  assert.deepEqual(writes, {oDeltaChart: 1, otDeltaChart: 1, oResults: 1, otResults: 1});

  page.run(`pausedJob.evaluations = 101;
    renderOptimizerResults(pausedJob);
    renderOptimizerTesterResults(pausedJob);`);
  assert.equal(writes.oResults, 1);
  assert.equal(writes.otResults, 1);
  assert.equal(page.element('oResultBacktests').textContent, '101');
  assert.equal(page.element('otResultBacktests').textContent, '101');

  page.run(`pausedJob.history.push({iteration:3,delta:0.1,pnl:15,win_rate:70,trade_count:2});
    pausedJob.iteration = 3; pausedJob.current_delta = 0.1;
    pausedJob.best_summary.total_pnl = 15;
    pausedJob.best_trades.push({'Trade #':1, Position:'Long', 'P&L ($)':3});
    renderDeltaCard('oDeltaSection','oDeltaChart',pausedJob);
    renderDeltaCard('otDeltaSection','otDeltaChart',pausedJob);
    renderOptimizerResults(pausedJob);
    renderOptimizerTesterResults(pausedJob);`);
  assert.deepEqual(writes, {oDeltaChart: 2, otDeltaChart: 2, oResults: 2, otResults: 2});
});

test('optimizer trade history deducts every fee and accumulates fees and net P&L', () => {
  const page = dashboard();
  const table = page.run(`_tradeTableHtml([
    {'Trade #':0, Position:'Long', 'Start Time':'2025-01-01 00:00:00', 'End Time':'2025-01-02 12:00:00',
     'Days Open':1.5, 'Gross P&L ($)':5.6, 'Entry Fee ($)':0.1, 'Exit Fee ($)':0.2, 'Holding Fee ($)':0.3, 'P&L ($)':5},
    {'Trade #':1, Position:'Short', 'P&L ($)':-2}
  ])`);
  assert.match(table, /Entry Fee/);
  assert.match(table, /Exit Fee/);
  assert.match(table, /Holding Fee/);
  assert.match(table, /1\.500/);
  assert.match(table, /Cumulative Fees/);
  assert.match(table, /\$0\.6000/);
  assert.match(table, /Cumulative P&amp;L/);
  assert.match(table, /\+\$3\.00/);
  assert.doesNotMatch(table, /Balance/);
});

test('optimizer trade history calculates net P&L from gross and fee breakdown', () => {
  const page = dashboard();
  const table = page.run(`_tradeTableHtml([
    {'Trade #':0, Position:'Long', 'Gross P&L ($)':10, 'Entry Fee ($)':0.1, 'Exit Fee ($)':0.2, 'Holding Fee ($)':0.3},
    {'Trade #':1, Position:'Short', 'Gross P&L ($)':-2, 'Entry Fee ($)':0.1, 'Exit Fee ($)':0.2, 'Holding Fee ($)':0.1}
  ])`);
  assert.match(table, /\$0\.6000<\/td>[\s\S]*\+\$9\.40<\/td>[\s\S]*\+\$9\.40<\/td>/);
  assert.match(table, /\$1\.0000<\/td>[\s\S]*-\$2\.40<\/td>[\s\S]*\+\$7\.00<\/td>/);
});

test('pause buttons switch to resume while either optimizer is paused', () => {
  const page = dashboard();
  page.run("_setOptimizerRunning(true, 'paused', true); _setOptimizerTesterRunning(true, 'pausing', true)");
  assert.equal(page.element('oPauseBtn').textContent, '▶ Resume');
  assert.equal(page.element('otPauseBtn').textContent, '▶ Resume');
  page.run("_setOptimizerRunning(true, 'running', true); _setOptimizerTesterRunning(false)");
  assert.equal(page.element('oPauseBtn').textContent, 'Ⅱ Pause');
  assert.equal(page.element('otPauseBtn').style.display, 'none');
  page.run("_setOptimizerRunning(true, 'running', false)");
  assert.equal(page.element('oPauseBtn').style.display, 'none');
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

test('balanced floors appear in both requests and reject an empty result', () => {
  const page = dashboard();
  page.context.document.querySelector = () => ({value: 'balanced'});
  for (const prefix of ['o', 'ot']) {
    page.element(prefix + 'MinWinRate').value = '62.5';
    page.element(prefix + 'MinClosedTrades').value = '40';
  }
  const optimizer = page.run('_optimizerPayload()');
  const tester = page.run('_optimizerTesterPayload()');
  assert.equal(optimizer.min_win_rate, 62.5);
  assert.equal(optimizer.min_closed_trades, 40);
  assert.equal(tester.min_win_rate, 62.5);
  assert.equal(tester.min_closed_trades, 40);
  page.run(`renderOptimizerResults({status:'done', objective:'balanced', no_eligible_result:true,
    min_win_rate:62.5, min_closed_trades:40})`);
  page.run(`renderOptimizerTesterResults({status:'done', objective:'balanced', no_eligible_result:true,
    min_win_rate:62.5, min_closed_trades:40})`);
  assert.match(page.element('oResults').innerHTML, /No eligible result/);
  assert.match(page.element('otResults').innerHTML, /No eligible result/);
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

test('Portfolio Manager replaces portfolio navigation and preserves exchange history rendering', () => {
  assert.match(html, /data-view="PortfolioManager"[^>]*>Portfolio Manager<\/button>/);
  assert.doesNotMatch(html, /data-view="Portfolio"/);
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

test('managed paper metrics stay separate and decision reasons are escaped', () => {
  const page = dashboard();
  page.run(`renderPortfolioManager({approved_assets:['BTC','XAUT'], paper:{
    assets:['BTC'], budget_usdt:'200', cash_usdt:'150', flow_offset_usdt:'0',
    running:true, holdings:{BTC:'0.5'}, equity:[{equity_usdt:199,drawdown_pct:0.5}],
    fills:[{asset:'BTC'}], decisions:[{at:'2026-09-27T12:00:00Z',reason:'<img src=x>'}]
  }, research:null})`);
  assert.match(page.element('managerMetrics').innerHTML, /199 USDT/);
  assert.match(page.element('managerStatus').textContent, /running/);
  assert.match(page.element('managerDecisions').innerHTML, /&lt;img src=x&gt;/);
});

test('ZEC shows live 10x delegation separately from Agentic AI 5x cap', () => {
  const page = dashboard();
  page.element('fSymbol').selectedOptions = [{
    value: 'zecusdt', dataset: {buy: 'true', sell: 'true', fee: '0.001'}
  }];
  page.element('fSymbol').value = 'zecusdt';
  page.element('fStrategy').value = 'AgenticAI';
  page.element('fLeverage').value = '10';
  page.run("marginMarketsSource = 'live_auth'; onMarketChange('f')");
  assert.match(page.element('fSymbolHint').innerHTML, /Checking this market/);
  assert.equal(page.element('fLeverage').max, 5);
  page.run("marketLeverageCache.ZECUSDT = {data:{buyMax:10,sellMax:10},expiresAt:Date.now()+30000}; onMarketChange('f')");
  assert.match(page.element('fSymbolHint').innerHTML, /long up to <strong>10×/);
  assert.match(page.element('fSymbolHint').innerHTML, /short up to <strong>10×/);
  assert.equal(page.element('fLeverage').max, 5);
  assert.equal(page.element('fLeverage').value, 5);
  assert.match(page.element('fLeverageHint').textContent, /Agentic AI bot accepts whole-number leverage up to 5×/);
  page.element('fStrategy').value = 'AdvRSI';
  page.element('fLeverage').value = '10';
  page.run("onMarketChange('f')");
  assert.equal(page.element('fLeverage').max, 10);
  assert.equal(page.run("marketLeverageError('f')"), '');
  page.element('fLeverage').value = '11';
  assert.match(page.run("marketLeverageError('f')"), /delegation allows up to 10×/);

  page.run("marginMarkets = [{symbol:'ZECUSDT',dstCurrency:'usdt',buyEnabled:true,sellEnabled:true,maxLeverage:5,feeRate:'0.001'}]; populateSymbolSelect('f','zecusdt')");
  assert.doesNotMatch(page.element('fSymbol').innerHTML, /your max 5×/);
});
