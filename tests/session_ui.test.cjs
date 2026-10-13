const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0]
  .replace(/checkSession\(\)\.then\(active => \{ if \(active\) openDashboard\(\); \}\)[\s\S]*$/, '');
const modulePath = path.join(__dirname, '..', 'static', 'session.js');

function dashboard(fetch) {
  const elements = new Map(), timers = new Map();
  let nextTimer = 0;
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {id,value:'',textContent:'',innerHTML:'',style:{},dataset:{},options:[],hidden:false,
        classList:{add: name => classes.add(name),remove:name => classes.delete(name),contains:name => classes.has(name)},
        focus() {}, addEventListener() {}, querySelectorAll:() => []});
    }
    return elements.get(id);
  };
  const document = {getElementById:element,addEventListener() {},
    querySelector:() => ({classList:{add() {},remove() {}}}),
    querySelectorAll:selector => selector === '.modal-overlay' ? [element('historyModal'),element('settingsModal')] : []};
  const context = vm.createContext({document,window:{fetch},console,Date,AbortController,Headers,Response,
    setTimeout(callback,delay) {const id=++nextTimer;timers.set(id,{callback,delay});return id;},
    clearTimeout:id => timers.delete(id),setInterval:() => 0,clearInterval() {}});
  if (fs.existsSync(modulePath)) vm.runInContext(fs.readFileSync(modulePath,'utf8'), context);
  vm.runInContext(script, context);
  const run = code => vm.runInContext(code, context);
  run("authenticated=true; csrfToken='account-A'; showToast=() => {}; var originalConnectWS=connectWS; connectWS=() => {}; loadMarginMarkets=async () => {}; ");
  return {element,run,context,timers};
}

test('visible bot chart refreshes once per candle and preserves viewport', async () => {
  let calls = 0;
  const page = dashboard(async () => {
    calls++;
    return new Response(JSON.stringify({ok:true,chart_revision:'rev-'+calls,
      chart_data:{candles:[{time:calls}]},trades:[]}),{status:200});
  });
  page.element('historyModal').classList.add('open');
  page.element('tabPriceChart').classList.add('active');
  page.run(`_histBotId='a'; bots=[{id:'a',chart_revision:'rev-1'}];
    var buildCount=0, viewport={from:10,to:20}, restored=null;
    _buildOptChart=(pane,data,trades,title,charts) => {
      buildCount++; charts.push({remove(){},timeScale(){return {
        getVisibleRange(){return viewport},setVisibleRange(range){restored=range}}}});
    }`);
  await page.run('loadBotPriceChart()');
  await page.run('refreshVisibleBotChart()');
  assert.equal(calls,1);
  page.run("bots[0].chart_revision='rev-2'");
  await page.run('refreshVisibleBotChart()');
  assert.equal(calls,2);
  assert.equal(page.run('buildCount'),2);
  assert.equal(page.run('JSON.stringify(restored)'),JSON.stringify({from:10,to:20}));
  page.element('historyModal').classList.remove('open');
  page.run("bots[0].chart_revision='rev-3'");
  await page.run('refreshVisibleBotChart()');
  assert.equal(calls,2);
});

test('chart requests coalesce and a closed modal discards late results', async () => {
  let resolve, calls=0;
  const page = dashboard(() => { calls++; return new Promise(done => {resolve=done}); });
  page.element('historyModal').classList.add('open');
  page.element('tabPriceChart').classList.add('active');
  page.run("_histBotId='a'; var buildCount=0; _buildOptChart=()=>{buildCount++}");
  const first = page.run('loadBotPriceChart()');
  const second = page.run('loadBotPriceChart()');
  assert.equal(calls,1);
  page.run("closeModal('historyModal')");
  page.element('tabPriceChart').innerHTML='closed';
  resolve(new Response(JSON.stringify({ok:true,chart_data:{candles:[{time:1}]}}),{status:200}));
  await Promise.all([first,second]);
  assert.equal(page.run('buildCount'),0);
  assert.equal(page.element('tabPriceChart').innerHTML,'closed');
});

test('bot chart follows new candles when viewing the latest candle', async () => {
  let calls = 0;
  const page = dashboard(async () => {
    calls++;
    return new Response(JSON.stringify({ok:true,chart_revision:'r'+calls,
      chart_data:{candles:Array.from({length:100+calls},(_,i)=>({time:i+1}))}}),{status:200});
  });
  page.element('historyModal').classList.add('open');
  page.element('tabPriceChart').classList.add('active');
  page.run(`_histBotId='a'; var logicalRestored=null;
    _buildOptChart=(pane,data,trades,title,charts) => charts.push({remove(){},timeScale(){return {
      getVisibleRange(){return {from:81,to:101}},getVisibleLogicalRange(){return {from:80,to:102}},
      setVisibleRange(){},setVisibleLogicalRange(range){logicalRestored=range}}}})`);
  await page.run('loadBotPriceChart()');
  await page.run('loadBotPriceChart()');
  assert.equal(page.run('JSON.stringify(logicalRestored)'),JSON.stringify({from:81,to:103}));
});

test('switching paired chart side ignores the prior side response', async () => {
  const pending = [];
  const page = dashboard(url => new Promise(resolve => {pending.push({url,resolve});}));
  page.element('historyModal').classList.add('open');
  page.element('tabPriceChart').classList.add('active');
  page.run("_histBotId='a'; _histPaired=true; var titles=[]; _buildOptChart=(pane,data,trades,title)=>titles.push(title)");
  const long = page.run('loadBotPriceChart()');
  page.run("_histSide='short'");
  const short = page.run('loadBotPriceChart()');
  pending[1].resolve(new Response(JSON.stringify({ok:true,chart_data:{candles:[{time:1}]}}),{status:200}));
  await short;
  pending[0].resolve(new Response(JSON.stringify({ok:true,chart_data:{candles:[{time:2}]}}),{status:200}));
  await long;
  assert.equal(page.run('JSON.stringify(titles)'),JSON.stringify(['SHORT STRATEGY CHART']));
});

test('late wallet success and transport failure cannot populate another account', async () => {
  for (const failure of [false, true]) {
    let resolve, reject, requestSignal;
    const page = dashboard((url,options) => {
      requestSignal=options.signal;
      return new Promise((res,rej) => {resolve=res;reject=rej;});
    });
    const pending = page.run('fetchWallet()');
    page.run("showLogin(); authenticated=true; csrfToken='account-B'");
    page.element('walletBar').innerHTML='B wallet';
    if (failure) reject(new Error('A private error'));
    else resolve(new Response(JSON.stringify({ok:true,usdt_available:12345.68}),{status:200}));
    await pending;
    assert.equal(page.element('walletBar').innerHTML,'B wallet');
    assert.equal(requestSignal?.aborted,true);
  }
});

test('late unauthorized response does not sign out the new account', async () => {
  let resolve;
  const page=dashboard(() => new Promise(res => {resolve=res;}));
  const pending=page.run('fetchWallet()');
  page.run("showLogin(); authenticated=true; csrfToken='account-B'");
  page.element('loginScreen').style.display='none';
  resolve(new Response('{}',{status:401}));
  await pending;
  assert.equal(page.run('authenticated'),true);
  assert.equal(page.run('csrfToken'),'account-B');
  assert.equal(page.element('loginScreen').style.display,'none');
});

test('session changes during JSON parsing discard exchange snapshots', async () => {
  let resolveJson;
  const page=dashboard(async () => ({ok:true,status:200,json:() => new Promise(res => {resolveJson=res;})}));
  const pending=page.run('loadExchangePortfolio()');
  for (let i=0;i<5&&!resolveJson;i++) await new Promise(setImmediate);
  page.run("showLogin(); authenticated=true; csrfToken='account-B'");
  page.element('portfolioStatus').textContent='B portfolio';
  page.element('portfolioMetrics').innerHTML='B metrics';
  resolveJson({snapshot:{captured_at:'2026-10-02',equity_usdt:99999,holdings:[],positions:[]},history:{}});
  await pending;
  assert.equal(page.element('portfolioMetrics').innerHTML,'B metrics');
  assert.equal(page.element('portfolioStatus').textContent,'B portfolio');
});

test('logout removes private panes, notes, modal drafts and caches', () => {
  const page=dashboard(async () => new Response('{}'));
  const privateIds=['portfolioFlows','portfolioMetrics','portfolioHoldings','portfolioPositions','portfolioEquityChart',
    'managerMetrics','managerDecisions','managerFills','managerEquity','trainerLossChart','tabTrades','tabActivity','historySummary',
    'aiEvidenceBody','backtestSummary','recoveryStatus','executionStatus','walletBar'];
  for (const id of privateIds) {page.element(id).innerHTML='A private data';page.element(id).textContent='A private data';}
  for (const id of ['portfolioFlowNote','portfolioFlowAmount','sToken','sPublicKey','sPrivateKey']) page.element(id).value='A draft';
  page.element('historyModal').classList.add('open');
  page.run("_createBudget={secret:1};marketLeverageCache={BTCUSDT:{data:{buyMax:5}}};prevClosedTrades={A:9}; showLogin()");
  for (const id of privateIds) {
    assert.doesNotMatch(page.element(id).innerHTML,/A private/);
    assert.doesNotMatch(page.element(id).textContent,/A private/);
  }
  for (const id of ['portfolioFlowNote','portfolioFlowAmount','sToken','sPublicKey','sPrivateKey']) assert.equal(page.element(id).value,'');
  assert.equal(page.element('historyModal').classList.contains('open'),false);
  assert.equal(page.run('_createBudget'),null);
  assert.equal(Object.keys(page.run('marketLeverageCache')).length,0);
  assert.equal(Object.keys(page.run('prevClosedTrades')).length,0);
});

test('successful login reloads the active private view', async () => {
  const page=dashboard(async url => new Response(JSON.stringify({authenticated:true,csrf_token:'B'})));
  page.run("var loadedViews=[];loadPortfolio=async () => loadedViews.push('PortfolioManager');fetchWallet=async () => {}; switchView('PortfolioManager'); showLogin();");
  page.run('loadedViews=[]');
  await page.run('signIn({preventDefault(){}})');
  assert.deepEqual(Array.from(page.run('loadedViews')),['PortfolioManager']);
});

test('a replaced websocket cannot render old financial state', () => {
  const page=dashboard(async () => new Response('{}'));
  const sockets=[];
  page.context.location={protocol:'http:',host:'testserver'};
  page.context.WebSocket=function() {sockets.push(this);this.close=() => {};};
  page.run('connectWS = originalConnectWS');
  page.run('renderGrid=() => {}; updateSummary=() => {};connectWS()');
  const oldMessage=sockets[0].onmessage;
  page.run("showLogin();authenticated=true;csrfToken='B'");
  oldMessage({data:JSON.stringify({bots:[{id:'A',name:'private'}]})});
  assert.equal(page.run('bots.length'),0);
});

test('a late login failure cannot replace the new session message', async () => {
  let reject;
  const page=dashboard(() => new Promise((resolve,rej) => {reject=rej;}));
  page.run("authenticated=false;csrfToken=null");
  const pending=page.run('signIn({preventDefault(){}})');
  page.run("showLogin();authenticated=true;csrfToken='B'");
  page.element('loginMessage').textContent='B message';
  reject(new Error('Old login transport error'));
  await pending;
  assert.equal(page.element('loginMessage').textContent,'B message');
});
