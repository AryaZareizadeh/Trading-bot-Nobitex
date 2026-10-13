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
  const timers = new Map();
  let timerId = 0;
  let assetInputs = [];
  const tabs = ['Bots', 'AIBots', 'PortfolioManager', 'Optimizer'].map(view => ({
    dataset: {view},
    classList: classes(),
  }));
  function classes(initial = []) {
    const values = new Set(initial);
    return {
      add: name => values.add(name),
      remove: name => values.delete(name),
      contains: name => values.has(name),
      toggle(name, enabled) {
        if (enabled === undefined) enabled = !values.has(name);
        if (enabled) values.add(name); else values.delete(name);
        return enabled;
      },
    };
  }
  function element(id) {
    if (!elements.has(id)) {
      let markup = '';
      const attributes = {};
      const item = {
        id, value: '', textContent: '', style: {}, dataset: {}, options: [],
        disabled: false, checked: false, hidden: false,
        classList: classes(id === 'viewPortfolioManager' ? ['active'] : []),
        setAttribute: (name, value) => { attributes[name] = String(value); },
        getAttribute: name => attributes[name] ?? null,
        removeAttribute: name => { delete attributes[name]; },
        focus: () => {},
        checkValidity: () => true,
        reportValidity: () => true,
        querySelectorAll: selector => selector.includes('manager-asset') ? assetInputs : [],
      };
      Object.defineProperty(item, 'innerHTML', {
        get: () => markup,
        set(value) {
          markup = String(value);
          if (id === 'managerAssets') {
            assetInputs = [...markup.matchAll(/<input\b[^>]*class="[^"]*manager-asset[^>]*>/g)].map(([tag]) => ({
              value: tag.match(/\bvalue="([^"]*)"/)?.[1] || '',
              checked: /\schecked(?:[\s>]|=)/.test(tag),
              disabled: /\sdisabled(?:[\s>]|=)/.test(tag),
              dataset: {},
            }));
          }
        },
      });
      elements.set(id, item);
    }
    return elements.get(id);
  }
  const document = {
    getElementById: element,
    querySelectorAll(selector) {
      if (selector === '.manager-asset') return assetInputs;
      if (selector === '.manager-asset:checked') return assetInputs.filter(input => input.checked);
      if (selector === '.view-pane') return tabs.map(tab => element('view' + tab.dataset.view));
      if (selector === '.main-tab-btn') return tabs;
      return [];
    },
    querySelector(selector) {
      const view = selector.match(/data-view="([^"]+)"/)?.[1];
      return tabs.find(tab => tab.dataset.view === view) || null;
    },
    addEventListener: () => {},
  };
  const context = vm.createContext({
    document, window: {fetch: () => {}}, console, Date, AbortController,
    setTimeout(callback, delay) {
      const id = ++timerId;
      timers.set(id, {callback, delay});
      return id;
    },
    clearTimeout: id => timers.delete(id),
    setInterval: () => 0,
    clearInterval: () => {},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'session.js'), 'utf8'), context);
  vm.runInContext(script, context);
  vm.runInContext('var toastMessages = []; showToast = message => toastMessages.push(String(message));', context);
  element('managerBudget').value = '200';
  element('managerDays').value = '365';
  return {context, element, timers, assets: () => assetInputs, run: code => vm.runInContext(code, context)};
}

function renderReady(page, extra = '') {
  page.run(`renderPortfolioManager({approved_assets:['BTC','XAUT'], paper:null,
    model_status:{ready:true}, research:null ${extra}})`);
}

test('paper portfolio loads even when the exchange portfolio request fails', async () => {
  const page = dashboard();
  page.run(`var requestPaths = []; apiFetch = async url => {
    requestPaths.push(url);
    if (url === '/api/portfolio') throw new Error('Exchange unavailable');
    return {ok:true, json:async () => ({approved_assets:['BTC'], paper:{
      assets:['BTC'], budget_usdt:'200', cash_usdt:'200', running:false, holdings:{}, equity:[], fills:[]
    }, model_status:{ready:true}})};
  };`);
  await page.run('loadPortfolio()');
  assert.equal(page.run("requestPaths.includes('/api/portfolio-manager')"), true);
  assert.match(page.element('managerStatus').textContent, /stopped|paused/i);
  assert.match(page.element('portfolioStatus').textContent, /exchange|unavailable/i);
});

test('failed exchange snapshot capture does not prevent loading the managed portfolio', async () => {
  const page = dashboard();
  page.run(`var requestPaths = []; apiFetch = async url => {
    requestPaths.push(url);
    if (url === '/api/portfolio/snapshot') return {ok:false,json:async () => ({error:'No exchange credentials'})};
    return {ok:true,json:async () => ({approved_assets:['BTC'],paper:null,model_status:{ready:true}})};
  };`);
  await page.run('loadPortfolio(true)');
  assert.equal(page.run("requestPaths.includes('/api/portfolio-manager')"), true);
  assert.match(page.element('portfolioStatus').textContent, /credentials/i);
  assert.match(page.element('managerAssets').innerHTML, /BTC/);
});

test('saved paper setup locks its budget and uses the saved asset universe', () => {
  const page = dashboard();
  page.run(`renderPortfolioManager({approved_assets:['BTC','XAUT'],model_status:{ready:true},paper:{
    assets:['BTC'],budget_usdt:'325',cash_usdt:'325',running:false,holdings:{},equity:[],fills:[]}})`);
  assert.equal(String(page.element('managerBudget').value), '325');
  assert.equal(page.element('managerBudget').disabled, true);
  assert.equal(page.assets().every(input => input.disabled), true);
  page.assets().forEach(input => { input.checked = input.value === 'XAUT'; });
  assert.deepEqual(Array.from(page.run('selectedManagedAssets()')), ['BTC']);
  assert.match(page.element('managerStartBtn').textContent, /resume/i);
});

test('start, stop and research controls reflect running activity', () => {
  const page = dashboard();
  page.run(`renderPortfolioManager({approved_assets:['BTC'],model_status:{ready:true},paper:{
    assets:['BTC'],budget_usdt:'200',cash_usdt:'200',running:true,holdings:{},equity:[],fills:[]},
    research_status:{status:'running'}})`);
  assert.equal(page.element('managerStartBtn').disabled, true);
  assert.equal(page.element('managerStopBtn').disabled, false);
  assert.equal(page.element('managerResearchBtn').disabled, true);
  page.run(`renderPortfolioManager({approved_assets:['BTC'],model_status:{ready:true},paper:{
    assets:['BTC'],budget_usdt:'200',cash_usdt:'200',running:false,holdings:{},equity:[],fills:[]},
    research_status:{status:'complete'}})`);
  assert.equal(page.element('managerStartBtn').disabled, false);
  assert.equal(page.element('managerStopBtn').disabled, true);
  assert.equal(page.element('managerResearchBtn').disabled, false);
});

test('refresh preserves the user asset selection and budget before creating paper capital', () => {
  const page = dashboard();
  renderReady(page);
  page.assets().find(input => input.value === 'XAUT').checked = false;
  page.element('managerBudget').value = '350';
  renderReady(page);
  assert.deepEqual(Array.from(page.run('selectedManagedAssets()')), ['BTC']);
  assert.equal(page.element('managerBudget').value, '350');
  page.assets().forEach(input => { input.checked = false; });
  renderReady(page);
  assert.deepEqual(Array.from(page.run('selectedManagedAssets()')), []);
});

test('paper start waits for usable research while historical research stays available', () => {
  const page = dashboard();
  page.run(`renderPortfolioManager({approved_assets:['BTC'],paper:null,research:null,
    model_status:{ready:false,reason:'Run historical research first'}})`);
  assert.equal(page.element('managerStartBtn').disabled, true);
  assert.equal(page.element('managerStopBtn').disabled, true);
  assert.equal(page.element('managerResearchBtn').disabled, false);
  page.run(`renderPortfolioManager({approved_assets:['BTC'],paper:null,research:null,
    model_status:{ready:true}})`);
  assert.equal(page.element('managerStartBtn').disabled, false);
});

test('invalid budgets, empty assets and invalid research days never reach the API', async t => {
  for (const value of ['', '0', '-1', '100001', 'NaN']) {
    await t.test('budget ' + JSON.stringify(value), async () => {
      const page = dashboard();
      renderReady(page);
      page.element('managerBudget').value = value;
      page.run('var postRequests = 0; apiFetch = async () => { postRequests++; throw new Error("Unexpected request"); };');
      await page.run('startManagedPaper()');
      assert.equal(page.run('postRequests'), 0);
      assert.match(page.element('managerSetupError').textContent, /budget|USDT|amount/i);
    });
  }
  await t.test('no selected assets', async () => {
    const page = dashboard();
    renderReady(page);
    page.assets().forEach(input => { input.checked = false; });
    page.run('var postRequests = 0; apiFetch = async () => { postRequests++; };');
    await page.run('runManagedResearch()');
    assert.equal(page.run('postRequests'), 0);
    assert.match(page.element('managerSetupError').textContent, /asset/i);
  });
  for (const value of ['', '89', '731', '90.5', 'NaN']) {
    await t.test('days ' + JSON.stringify(value), async () => {
      const page = dashboard();
      renderReady(page);
      page.element('managerDays').value = value;
      page.run('var postRequests = 0; apiFetch = async () => { postRequests++; };');
      await page.run('runManagedResearch()');
      assert.equal(page.run('postRequests'), 0);
      assert.match(page.element('managerSetupError').textContent, /days|90|730/i);
    });
  }
});

test('valid setup accepts API budget and day boundaries', () => {
  const page = dashboard();
  renderReady(page);
  page.element('managerBudget').value = '0.5';
  page.element('managerDays').value = '90';
  assert.equal(page.run('managerSetupPayload(true).budget_usdt'), 0.5);
  assert.equal(page.run('managerSetupPayload(true).days'), 90);
  page.element('managerBudget').value = '100000';
  page.element('managerDays').value = '730';
  assert.equal(page.run('managerSetupPayload(true).budget_usdt'), 100000);
  assert.equal(page.run('managerSetupPayload(true).days'), 730);
});

test('manager actions report network failure and restore usable controls', async () => {
  const page = dashboard();
  renderReady(page);
  page.run('apiFetch = async () => { throw new Error("Network disconnected"); };');
  await page.run('startManagedPaper()');
  assert.match(page.element('managerFeedback').textContent, /network disconnected/i);
  assert.equal(page.run('managerActionPending'), false);
  assert.equal(page.element('managerStartBtn').disabled, false);
  assert.equal(page.element('managerResearchBtn').disabled, false);
});

test('pending manager actions suppress duplicate requests and restore state after completion', async () => {
  const page = dashboard();
  renderReady(page);
  page.run(`var postRequests = 0; var finishAction;
    apiFetch = (url, options = {}) => {
      if (options.method === 'POST') {
        postRequests++;
        return new Promise(resolve => { finishAction = resolve; });
      }
      return Promise.resolve({ok:true,json:async () => ({approved_assets:['BTC','XAUT'],paper:null,
        model_status:{ready:true}})});
    };`);
  const pending = page.run('startManagedPaper()');
  assert.equal(page.run('managerActionPending'), true);
  assert.equal(page.element('managerStartBtn').disabled, true);
  assert.equal(page.element('managerResearchBtn').disabled, true);
  await page.run('startManagedPaper()');
  assert.equal(page.run('postRequests'), 1);
  page.run('finishAction({ok:true,json:async () => ({ok:true})})');
  await pending;
  assert.equal(page.run('managerActionPending'), false);
  assert.equal(page.element('managerStartBtn').disabled, false);
});

test('API validation details become readable field errors', async () => {
  const page = dashboard();
  renderReady(page);
  page.run(`apiFetch = async () => ({ok:false,status:422,json:async () => ({detail:[
    {loc:['body','budget_usdt'],msg:'Input should be less than or equal to 100000',type:'less_than_equal'}
  ]})});`);
  await page.run('startManagedPaper()');
  assert.match(page.element('managerFeedback').textContent, /100000/);
  assert.doesNotMatch(page.element('managerFeedback').textContent, /\[object Object\]/);
  assert.equal(page.run('managerActionPending'), false);
});

test('non-JSON manager errors are handled without leaving actions stuck', async () => {
  const page = dashboard();
  renderReady(page);
  page.run(`apiFetch = async () => ({ok:false,status:503,json:async () => {
    throw new SyntaxError('Unexpected HTML response');
  }});`);
  await page.run('startManagedPaper()');
  assert.match(page.element('managerFeedback').textContent, /failed|503|response|unavailable/i);
  assert.equal(page.run('managerActionPending'), false);
  assert.equal(page.element('managerResearchBtn').disabled, false);
});

test('incomplete current paper marks never masquerade as starting capital or zero drawdown', () => {
  const page = dashboard();
  page.run(`renderPortfolioManager({approved_assets:['BTC'],model_status:{ready:true},paper:{
    assets:['BTC'],budget_usdt:'200',cash_usdt:'150',running:true,holdings:{BTC:'0.5'},fills:[],
    equity:[{complete:true,equity_usdt:190,drawdown_pct:5},{complete:false,unpriced:['BTC']}]}})`);
  const metrics = page.element('managerMetrics').innerHTML;
  assert.match(metrics, /—/);
  assert.doesNotMatch(metrics, /200 USDT|190 USDT|0\.00%/);
  assert.match(page.element('managerObservation').textContent, /missing prices.*BTC/i);
  assert.match(page.element('managerObservation').textContent, /equity and return are unavailable/i);
  assert.match(page.element('managerStatus').textContent, /incomplete|missing|BTC/i);
});

test('active manager polls with queued timers, then stops polling on navigation away', async () => {
  const page = dashboard();
  page.run(`apiFetch = async () => ({ok:true,json:async () => ({approved_assets:['BTC'],
    model_status:{ready:true},paper:{assets:['BTC'],budget_usdt:'200',cash_usdt:'200',running:true,
    holdings:{},equity:[],fills:[]}})});`);
  await page.run('loadPortfolioManager()');
  assert.equal(page.timers.size, 1);
  assert.equal([...page.timers.values()][0].delay, 15000);
  await page.run('loadPortfolioManager()');
  assert.equal(page.timers.size, 1);
  page.run("switchView('Bots')");
  assert.equal(page.timers.size, 0);
  assert.equal(page.run('managerRefreshTimer'), null);
});

test('research shows readable criteria, excludes unsafe markup and identifies an older report while running', () => {
  const page = dashboard();
  page.run(`renderPortfolioManager({approved_assets:['BTC'],paper:null,model_status:{ready:true,assets:['BTC']},
    research_status:{status:'running',started_at:'2026-10-01T08:00:00Z'},research:{selected:'momentum',
      validation_outcome:{passed:false,checks:{minimum_history_days:false,test_drawdown_at_most_10pct:true}},
      test:{return_pct:4,max_drawdown_pct:3},stress:{return_pct:2,max_drawdown_pct:4},
      benchmarks:{usdt:{return_pct:0,max_drawdown_pct:0},basket:{return_pct:1,max_drawdown_pct:5}},
      validation:{momentum:[{return_pct:2},{return_pct:1}]},forecast_quality:{4:{return_mae_bps:2.5}},
      data_quality:{BTC:{eligible:true},XAUT:{eligible:false,reason:'<script>bad</script>'}}}})`);
  const details = page.element('managerResearchDetails').innerHTML;
  assert.match(details, /Previous completed report shown below/);
  assert.match(details, /At least 365 days of history/);
  assert.match(details, /Test drawdown at most 10%/);
  assert.match(details, /Not met/);
  assert.match(details, /Passed/);
  assert.match(details, /Hold USDT|Equal asset basket/);
  assert.match(details, /&lt;script&gt;bad&lt;\/script&gt;/);
  assert.doesNotMatch(details, /<script>/);
});

test('partial model coverage explains skipped assets and disables starting an uncovered selection', () => {
  const page = dashboard();
  page.run(`renderPortfolioManager({approved_assets:['BTC','XAUT'],paper:{assets:['BTC','XAUT'],
    budget_usdt:'200',cash_usdt:'200',running:false,equity:[],holdings:{},fills:[]},
    model_status:{ready:true,assets:['BTC'],missing_paper_assets:['XAUT']}})`);
  assert.equal(page.element('managerStartBtn').disabled, false);
  assert.match(page.element('managerModelNote').textContent, /XAUT/);
  assert.match(page.element('managerModelNote').textContent, /skipped/i);
  page.run(`renderPortfolioManager({approved_assets:['BTC','XAUT'],paper:null,
    model_status:{ready:true,assets:['BTC']}})`);
  page.assets().forEach(input => { input.checked = input.value === 'XAUT'; });
  page.run('updateManagedControls()');
  assert.equal(page.element('managerStartBtn').disabled, true);
  page.assets().forEach(input => { input.checked = false; });
  page.run('updateManagedControls()');
  assert.equal(page.element('managerStartBtn').disabled, true);
  assert.equal(page.element('managerResearchBtn').disabled, true);
});

test('paper charts filter incomplete and invalid values and handle flat equity without NaN', () => {
  const page = dashboard();
  page.run(`renderPortfolioManager({approved_assets:['BTC'],model_status:{ready:true},paper:{assets:['BTC'],
    budget_usdt:'200',cash_usdt:'200',running:false,holdings:{},fills:[],equity:[
      {complete:true,equity_usdt:200,at:'2026-10-01T08:00:00Z'},
      {complete:true,equity_usdt:'invalid',at:'2026-10-01T09:00:00Z'},
      {complete:false,equity_usdt:900,at:'2026-10-01T10:00:00Z'},
      {complete:true,equity_usdt:null,at:'2026-10-01T11:00:00Z'},
      {complete:true,equity_usdt:200,drawdown_pct:0,at:'2026-10-01T12:00:00Z'}
    ]}})`);
  const chart = page.element('managerEquity').innerHTML;
  assert.match(chart, /<svg/);
  assert.match(chart, /2 recent complete observations/);
  assert.doesNotMatch(chart, /NaN|Infinity|900 USDT/);
  assert.match(chart, /200 USDT/);
});

test('an older manager refresh cannot undo a successful paper start', async () => {
  const page = dashboard();
  page.run(`var oldPaper = {assets:['BTC'],budget_usdt:'200',cash_usdt:'200',running:false,
    equity:[],holdings:{},fills:[]};
    var newPaper = {...oldPaper,running:true};
    renderPortfolioManager({approved_assets:['BTC'],paper:oldPaper,model_status:{ready:true,assets:['BTC']}});
    var managerReads = 0; var finishOldRefresh;
    apiFetch = async (url,options = {}) => {
      if (options.method === 'POST') return {ok:true,json:async () => ({ok:true,paper:newPaper})};
      managerReads++;
      if (managerReads === 1) return new Promise(resolve => { finishOldRefresh = resolve; });
      return {ok:true,json:async () => ({approved_assets:['BTC'],paper:newPaper,model_status:{ready:true,assets:['BTC']}})};
    };`);
  const refresh = page.run('loadPortfolioManager()');
  const start = page.run('startManagedPaper()');
  for (let turn = 0; turn < 8; turn++) await Promise.resolve();
  page.run(`finishOldRefresh({ok:true,json:async () => ({approved_assets:['BTC'],paper:oldPaper,
    model_status:{ready:true,assets:['BTC']}})})`);
  await Promise.all([refresh, start]);
  assert.equal(page.run('managerData.paper.running'), true);
  assert.equal(page.element('managerStartBtn').disabled, true);
  assert.equal(page.element('managerStopBtn').disabled, false);
});

test('reset is available while research is running and requires confirmation', async () => {
  const page = dashboard();
  assert.match(html, /id="managerResetBtn"[^>]*onclick="resetManagedPaper\(\)"/);
  renderReady(page, ",research_status:{status:'running'}");
  assert.equal(page.element('managerResetBtn').disabled, false);
  assert.equal(page.element('managerBudget').disabled, false);
  assert.equal(page.assets().every(input => !input.disabled), true);
  assert.equal(page.element('managerResearchBtn').disabled, true);
  page.run(`var resetRequests = 0; apiFetch = async () => { resetRequests++; throw new Error('Should not submit'); };`);
  await page.run('resetManagedPaper()');
  assert.equal(page.element('managerResetConfirm').hidden, false);
  assert.equal(page.run('resetRequests'), 0);
  page.run('cancelManagedReset()');
  assert.equal(page.element('managerResetConfirm').hidden, true);
  assert.equal(page.run('resetRequests'), 0);
  assert.match(html, /A backup of the saved paper portfolio is kept/);
});

test('confirmed reset clears paper metrics and unlocks fresh setup while keeping research', async () => {
  const page = dashboard();
  page.run(`var savedResearch = {selected:'momentum',test:{return_pct:1},stress:{return_pct:0}};
    var savedData = {approved_assets:['BTC','XAUT'],paper:{assets:['BTC'],budget_usdt:'500',
      cash_usdt:'400',running:true,holdings:{BTC:'1'},equity:[{equity_usdt:550,complete:true}],fills:[]},
      model_status:{ready:true,assets:['BTC']},paper_error:'Stale prices',research:savedResearch};
    renderPortfolioManager(savedData);
    var resetPaths = []; apiFetch = async (path,options = {}) => {
      resetPaths.push(path);
      return {ok:true,json:async () => options.method === 'POST'
        ? {ok:true,paper:null,paper_error:null,backup_id:'paper-backup',research_status:null}
        : {...savedData,paper:null,paper_error:null,research_status:null}};
    };`);
  await page.run('resetManagedPaper()');
  await page.run('confirmManagedReset()');
  assert.equal(page.element('managerResetConfirm').hidden, true);
  assert.equal(page.run("resetPaths[0]"), '/api/portfolio-manager/paper/reset');
  assert.equal(page.run('managerData.paper'), null);
  assert.equal(page.element('managerMetrics').innerHTML, '');
  assert.equal(page.element('managerBudget').disabled, false);
  assert.equal(String(page.element('managerBudget').value), '200');
  assert.equal(page.element('managerDays').value, '365');
  assert.equal(page.run("managerData.research.selected"), 'momentum');
  assert.equal(page.assets().every(input => !input.disabled && input.checked), true);
  assert.match(page.element('managerFeedback').textContent, /reset/i);
});

test('failed reset keeps the current paper portfolio and restores controls', async () => {
  const page = dashboard();
  page.run(`renderPortfolioManager({approved_assets:['BTC'],model_status:{ready:true},paper:{
    assets:['BTC'],budget_usdt:'500',cash_usdt:'400',running:false,holdings:{},equity:[]}});
    apiFetch = async () => ({ok:false,json:async () => ({error:'Reset could not be saved'})});`);
  await page.run('resetManagedPaper()');
  await page.run('confirmManagedReset()');
  assert.equal(page.element('managerResetConfirm').hidden, false);
  assert.equal(page.run('managerData.paper.budget_usdt'), '500');
  assert.equal(page.element('managerResetBtn').disabled, false);
  assert.match(page.element('managerFeedback').textContent, /Reset could not be saved/);
});

test('stalled action times out, aborts the request and releases reset controls', async () => {
  const page = dashboard();
  renderReady(page);
  page.run(`var stalledSignal; apiFetch = async (path,options) => {
    stalledSignal = options.signal; return new Promise(() => {});
  };`);
  const action = page.run("managedAction('/api/portfolio-manager/paper/start',{})");
  for (let turn = 0; turn < 8; turn++) await Promise.resolve();
  assert.equal(page.element('managerResetBtn').disabled, true);
  const timeout = [...page.timers.values()].find(timer => timer.delay === 20000);
  assert.ok(timeout, 'actions must have a request deadline');
  timeout.callback();
  await action;
  assert.equal(page.run('stalledSignal.aborted'), true);
  assert.equal(page.run('managerActionPending'), false);
  assert.equal(page.element('managerResetBtn').disabled, false);
  assert.match(page.element('managerFeedback').textContent, /timed out/i);
  assert.match(page.element('managerFeedback').textContent, /Refresh/i);
});

test('stalled refresh expires so Refresh and Reset remain usable', async () => {
  const page = dashboard();
  page.run('apiFetch = async () => new Promise(() => {})');
  const refresh = page.run('loadPortfolio()');
  for (let turn = 0; turn < 8; turn++) await Promise.resolve();
  const deadlines = [...page.timers.values()].filter(timer => timer.delay === 20000);
  assert.equal(deadlines.length, 2);
  deadlines.forEach(timer => timer.callback());
  await refresh;
  assert.equal(page.element('managerRefreshBtn').disabled, false);
  assert.equal(page.element('managerResetBtn').disabled, false);
  assert.match(page.element('managerStatus').textContent, /timed out/i);
});
