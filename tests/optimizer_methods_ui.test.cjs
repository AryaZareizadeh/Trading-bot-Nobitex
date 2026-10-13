const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const markup = html.split('<script>')[0];
const script = html.split('<script>')[1].split('</script>')[0]
  .replace(/checkSession\(\)\.then\(active => \{ if \(active\) openDashboard\(\); \}\)[\s\S]*$/, '');
const deStrategies = [
  'best1bin','best1exp','rand1bin','rand1exp','rand2bin','rand2exp',
  'randtobest1bin','randtobest1exp','currenttobest1bin','currenttobest1exp','best2bin','best2exp',
];
const config = {
  symbol:'btcusdt', strategy:'AdvRSI', timeframe:'60', candles:1000, credit:100,
  long_credit:120, short_credit:300, leverage:5, maxiter:30, n_trials:100,
  cores:0, default_cores:2, max_cores:4, objective:'profit',
  default_de_strategy:'best2bin',
  available_de_strategies:deStrategies.map(value => ({value,label:value})),
  available_timeframes:[{value:'60',label:'1h'}, {value:'240',label:'4h'}],
  available_strategies:[
    {value:'AdvRSI',label:'Advanced RSI',params:[{name:'rsi_length',low:5,high:40,default:14}]},
    {value:'AdvRSIIndependent',label:'Independent RSI',paired:true,params:[
      {name:'long_oversold',low:1,high:35,default:22},
      {name:'short_overbought',low:65,high:99,default:78},
    ]},
  ],
};

function dashboard(configOverrides={}) {
  const elements = new Map(), timers = new Map();
  let nextTimer = 0;
  function classes() {
    const values = new Set();
    return {add:value => values.add(value),remove:value => values.delete(value),contains:value => values.has(value),
      toggle(value,force) {if (force ?? !values.has(value)) values.add(value);else values.delete(value);}};
  }
  function element(id) {
    if (elements.has(id)) return elements.get(id);
    let innerHTML = '', value = '';
    const attributes = {};
    const item = {id,textContent:'',style:{},dataset:{},options:[],checked:false,disabled:false,hidden:false,
      classList:classes(),setAttribute:(key,next) => attributes[key]=String(next),
      getAttribute:key => attributes[key] ?? null,removeAttribute:key => delete attributes[key],
      querySelector:() => null,querySelectorAll:() => [],focus() {},scrollIntoView() {},
      add(option) {this.options.push(option);},
      replaceChildren(...options) {this.options=options;value=options[0]?.value || '';innerHTML='';},
    };
    Object.defineProperty(item,'value',{get:() => value,set:next => {value=String(next);}});
    Object.defineProperty(item,'selectedOptions',{get:() => item.options.filter(option => option.value===value)});
    Object.defineProperty(item,'innerHTML',{get:() => innerHTML,set:next => {
      innerHTML=String(next);
      if (innerHTML.includes('<option')) {
        item.options=[...innerHTML.matchAll(/<option\b([^>]*)>([^<]*)<\/option>/g)].map(([,attrs,label]) => ({
          value:attrs.match(/value="([^"]*)"/)?.[1] || '',textContent:label,dataset:{},
          selected:/\sselected(?:\s|$)/.test(attrs),
        }));
        value=(item.options.find(option => option.selected) || item.options[0])?.value || '';
      }
      for (const [tag,inputId] of innerHTML.matchAll(/<input\b[^>]*id="([^"]*)"[^>]*>/g)) {
        element(inputId).value=tag.match(/value="([^"]*)"/)?.[1] || '';
      }
    }});
    elements.set(id,item);
    const opening=markup.match(new RegExp(`<[^>]+\\bid="${id}"[^>]*>`))?.[0] || '';
    item.hidden=/\shidden(?:\s|>)/.test(opening);
    const input=markup.match(new RegExp(`<input\\b[^>]*id="${id}"[^>]*>`));
    if (input) {item.value=input[0].match(/value="([^"]*)"/)?.[1] || '';item.checked=/\schecked(?:\s|>)/.test(input[0]);}
    const select=markup.match(new RegExp(`<select\\b[^>]*id="${id}"[^>]*>([\\s\\S]*?)<\\/select>`));
    if (select) item.innerHTML=select[1];
    return item;
  }
  const objectiveIds=['oObjProfit','oObjWinrate','oObjBalanced','oObjLargeMove'];
  const objectiveValues=['profit','winrate','balanced','large_move'];
  function objective(selector) {
    const requested=selector.match(/value="([^"]*)"/)?.[1];
    const index=requested ? objectiveValues.indexOf(requested) : objectiveIds.findIndex(id => element(id).checked);
    if (index<0) return null;
    const result=element(objectiveIds[index]);
    result.value=objectiveValues[index];
    return result;
  }
  const document = {getElementById:element,addEventListener() {},
    querySelector(selector) {return selector.includes('name="oObjective"') ? objective(selector) : null;},
    querySelectorAll(selector) {
      if (selector.includes('name="oObjective"')) return objectiveIds.map((id,index) => {const item=element(id);item.value=objectiveValues[index];return item;});
      return [];
    },
  };
  function Option(label,value) {this.textContent=label;this.value=value;this.dataset={};}
  const context=vm.createContext({document,window:{fetch() {}},console,Date,Option,
    setTimeout(callback,delay) {const id=++nextTimer;timers.set(id,{callback,delay});return id;},
    clearTimeout:id => timers.delete(id),setInterval:() => 0,clearInterval() {},
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'session.js'), 'utf8'),context);
  vm.runInContext(script,context);
  const run=code => vm.runInContext(code,context);
  context.fixtureConfig={...config,...configOverrides};
  run(`authenticated=true;optimizerConfig=fixtureConfig;_optimizerConfigLoaded=true;
    marginMarkets=[{symbol:'BTCUSDT'}, {symbol:'ETHUSDT'}];
    var calls=[],toasts=[];showToast=(message,kind) => toasts.push({message,kind});
    apiFetch=async (url,options={}) => {calls.push({url,options});return {ok:true,status:200,json:async () => url.endsWith('/config') ? fixtureConfig : []};};
    populateOptimizerControls();onOptimizerStrategyChange();`);
  timers.clear();
  return {element,context,run,timers,elements};
}

function savedRun(id,method='de',status='done',extra={}) {
  return {id,method,status,symbol:'BTCUSDT',strategy:'AdvRSI',strategy_label:'Advanced RSI',timeframe:'60',
    created_at:'2026-10-02T09:00:00Z',candles:1000,candle_count:1000,credit:100,leverage:5,
    maxiter:30,n_trials:80,iteration:4,evaluations:40,objective:'profit',history:[],
    no_eligible_result:true,...extra};
}

test('optimizer offers DE and TPE on the shared form with no Tester pane', () => {
  assert.match(markup,/switchOptimizerSubTab\('de'\)/);
  assert.match(markup,/switchOptimizerSubTab\('tpe'\)/);
  assert.match(markup,/id="oTrials"/);
  assert.doesNotMatch(markup,/id="optimizerTesterPane"|Optimizer Tester/);
  const page=dashboard();
  assert.equal(page.run('optimizerMethod'),'de');
});

test('DE selector offers every built-in variant and starts with the configured default', () => {
  const page=dashboard();
  const select=page.element('oDEStrategy');
  assert.deepEqual(select.options.map(option => option.value).sort(),[...deStrategies].sort());
  assert.equal(select.value,'best2bin');
  assert.match(markup,/<select\b[^>]*id="oDEStrategy"/,'DE variants must use a native select');
  assert.match(markup,/id="oDEStrategyGroup"[\s\S]*?<label\b[^>]*>DE strategy<\/label>[\s\S]*?id="oDEStrategy"/);
  page.run('updateOptimizerMethodControls()');
  assert.equal(page.element('oDEStrategyGroup').hidden,false);
  assert.notEqual(page.element('oDEStrategyGroup').style.display,'none');
});

test('DE selector restores the persisted variant and uses server-provided labels', () => {
  const page=dashboard({de_strategy:'rand2exp',available_de_strategies:deStrategies.map(value => ({
    value,label:value==='rand2exp' ? 'Random two · exponential crossover' : value,
  }))});
  assert.equal(page.element('oDEStrategy').value,'rand2exp');
  assert.equal(page.element('oDEStrategy').selectedOptions[0]?.textContent,'Random two · exponential crossover');
});

test('older optimizer config still offers every DE variant with the historical best2bin default', () => {
  const page=dashboard({available_de_strategies:undefined,default_de_strategy:undefined,de_strategy:undefined});
  assert.deepEqual(page.element('oDEStrategy').options.map(option => option.value).sort(),[...deStrategies].sort());
  assert.equal(page.element('oDEStrategy').value,'best2bin');
  assert.equal(page.run('_optimizerPayload().de_strategy'),'best2bin');
});

test('saving optimizer preferences posts the selected DE variant', () => {
  const page=dashboard();
  page.element('oDEStrategy').value='rand2exp';
  page.run('calls=[];saveOptimizerPrefs()');
  page.timers.get(page.run('_optimizerPrefsTimer')).callback();
  const request=page.run("calls.find(call => call.options.method==='POST' && call.url==='/api/optimizer/prefs')");
  assert.ok(request,'The selected settings must be posted to optimizer preferences');
  const body=JSON.parse(request.options.body);
  assert.equal(body.method,'de');
  assert.equal(body.de_strategy,'rand2exp');
});

test('starting DE sends the selected variant to the common optimizer endpoint', async () => {
  const page=dashboard();
  page.element('oDEStrategy').value='currenttobest1exp';
  page.context.fixtureJob=savedRun('de-variant-started','de','done',{de_strategy:'currenttobest1exp'});
  page.run(`calls=[];apiFetch=async (url,options={}) => {
    calls.push({url,options});return {ok:true,json:async () => options.method==='POST' ? {job_id:'de-variant-started'} : fixtureJob};
  };`);
  await page.run('runOptimizer()');
  const request=page.run("calls.find(call => call.options.method==='POST' && call.url==='/api/optimizer/run')");
  assert.ok(request,'DE must start through /api/optimizer/run');
  const body=JSON.parse(request.options.body);
  assert.equal(body.method,'de');
  assert.equal(body.de_strategy,'currenttobest1exp');
});

test('switching to TPE hides the DE selector and returning restores the DE draft variant', async () => {
  const page=dashboard();
  page.element('oDEStrategy').value='randtobest1exp';
  page.element('oMaxiter').value='75';
  await page.run("switchOptimizerSubTab('tpe')");
  const group=page.element('oDEStrategyGroup');
  assert.ok(group.hidden || group.style.display==='none','TPE must hide the DE strategy control');
  page.element('oTrials').value='123';
  await page.run("switchOptimizerSubTab('de')");
  assert.equal(page.element('oDEStrategy').value,'randtobest1exp');
  assert.equal(page.run('_optimizerPayload().de_strategy'),'randtobest1exp');
  assert.equal(page.element('oMaxiter').value,'75');
  assert.equal(group.hidden,false);
  assert.notEqual(group.style.display,'none');
  await page.run("switchOptimizerSubTab('tpe')");
  assert.equal(page.element('oTrials').value,'123');
  await page.run("switchOptimizerSubTab('de')");
  assert.equal(page.element('oDEStrategy').value,'randtobest1exp');
});

test('saving TPE settings retains the saved DE variant when the dashboard reloads', async () => {
  const page=dashboard();
  page.element('oDEStrategy').value='rand2exp';
  page.run('calls=[];saveOptimizerPrefs()');
  page.timers.get(page.run('_optimizerPrefsTimer')).callback();
  const deRequest=page.run("calls.find(call => call.options.method==='POST' && call.url==='/api/optimizer/prefs')");
  assert.equal(JSON.parse(deRequest.options.body).de_strategy,'rand2exp');
  await page.run("switchOptimizerSubTab('tpe')");
  page.element('oTrials').value='137';
  page.run('saveOptimizerPrefs()');
  page.timers.get(page.run('_optimizerPrefsTimer')).callback();
  const tpeRequest=page.run("calls.filter(call => call.options.method==='POST' && call.url==='/api/optimizer/prefs').at(-1)");
  const savedPrefs=JSON.parse(tpeRequest.options.body);
  assert.equal(savedPrefs.method,'tpe');
  assert.equal(savedPrefs.n_trials,137);
  assert.equal(savedPrefs.de_strategy,'rand2exp','Saving TPE settings must retain the saved DE variant');
  const reloaded=dashboard(savedPrefs);
  await reloaded.run('initOptimizerTab()');
  assert.equal(reloaded.run('optimizerMethod'),'tpe');
  assert.equal(reloaded.element('oTrials').value,'137');
  await reloaded.run("switchOptimizerSubTab('de')");
  assert.equal(reloaded.element('oDEStrategy').value,'rand2exp');
  assert.equal(reloaded.run('_optimizerPayload().de_strategy'),'rand2exp');
});

test('a saved DE summary displays its persisted variant instead of the current form selection', () => {
  const page=dashboard();
  page.element('oDEStrategy').value='rand2bin';
  page.context.fixtureJob=savedRun('saved-de-variant','de','done',{de_strategy:'currenttobest1bin'});
  page.run('showOptimizerJob(fixtureJob)');
  assert.match(page.element('oCurrentRun').innerHTML,/currenttobest1bin/);
  assert.doesNotMatch(page.element('oCurrentRun').innerHTML,/rand2bin/);
});

test('a legacy saved DE summary displays the historical best2bin variant', () => {
  const page=dashboard();
  page.context.fixtureJob=savedRun('saved-legacy-de',undefined,'done',{method:undefined});
  page.run('showOptimizerJob(fixtureJob)');
  assert.match(page.element('oCurrentRun').innerHTML,/best2bin/);
});

test('a saved TPE summary omits DE variants even if obsolete settings include one', () => {
  const page=dashboard();
  page.context.fixtureJob=savedRun('saved-tpe-with-old-de-setting','tpe','done',{de_strategy:'rand2exp'});
  page.run("switchOptimizerSubTab('tpe');showOptimizerJob(fixtureJob)");
  assert.doesNotMatch(page.element('oCurrentRun').innerHTML,/DE strategy|rand2exp|best2bin/i);
});

test('method payloads preserve independent credits, objective floors and custom bounds', () => {
  const page=dashboard();
  page.run(`optimizerMethod='tpe'`);
  const values={oStrategy:'AdvRSIIndependent',oSymbol:'ethusdt',oTimeframe:'240',oCandles:'1500',
    oCredit:'999',oLongCredit:'125',oShortCredit:'375',oLeverage:'3',oCores:'2',oMaxiter:'55',oTrials:'137',
    oMinWinRate:'61',oMinClosedTrades:'35','op-lo-long_oversold':'9','op-hi-long_oversold':'27',
    'op-lo-short_overbought':'72','op-hi-short_overbought':'96'};
  for (const [id,value] of Object.entries(values)) page.element(id).value=value;
  page.element('oObjProfit').checked=false;
  page.element('oObjBalanced').checked=true;
  const tpe=JSON.parse(JSON.stringify(page.run('_optimizerPayload()')));
  assert.equal(tpe.method,'tpe');
  assert.equal(tpe.n_trials,137);
  assert.equal(tpe.long_credit,125);
  assert.equal(tpe.short_credit,375);
  assert.equal(tpe.symbol,'ethusdt');
  assert.equal(tpe.timeframe,'240');
  assert.equal(tpe.candles,1500);
  assert.equal(tpe.objective,'balanced');
  assert.equal(tpe.min_win_rate,61);
  assert.equal(tpe.min_closed_trades,35);
  assert.deepEqual(tpe.custom_bounds,{long_oversold:[9,27],short_overbought:[72,96]});
  page.run(`optimizerMethod='de'`);
  assert.equal(page.run('_optimizerPayload().method'),'de');
  assert.equal(page.run('_optimizerPayload().maxiter'),55);
});

test('starting TPE sends the trial budget to the common optimizer endpoint', async () => {
  const page=dashboard();
  await page.run("switchOptimizerSubTab('tpe')");
  page.element('oTrials').value='91';
  page.context.fixtureJob=savedRun('tpe-started','tpe');
  page.run(`calls=[];apiFetch=async (url,options={}) => {
    calls.push({url,options});return {ok:true,json:async () => options.method==='POST' ? {job_id:'tpe-started'} : fixtureJob};
  };`);
  await page.run('runOptimizer()');
  const request=JSON.parse(JSON.stringify(page.run("calls.find(call => call.options.method==='POST' && call.url==='/api/optimizer/run')")));
  assert.ok(request,'TPE must start through /api/optimizer/run');
  const body=JSON.parse(request.options.body);
  assert.equal(body.method,'tpe');
  assert.equal(body.n_trials,91);
  assert.equal(body.symbol,'btcusdt');
  assert.deepEqual(body.custom_bounds,{rsi_length:[5,40]});
});

test('a delayed DE launch stays under DE and preserves the selected TPE result', async () => {
  const page=dashboard();
  page.context.fixtureTpe=savedRun('kept-tpe','tpe','done',{
    no_eligible_result:false,best_pnl:7,best_summary:{total_pnl:7,win_rate:60,trade_count:30},
    params:[{name:'rsi_length',value:14,default:14,low:5,high:40}],
  });
  page.context.fixtureDe=savedRun('delayed-de','de','done');
  page.run(`switchOptimizerSubTab('tpe');showOptimizerJob(fixtureTpe);switchOptimizerSubTab('de');
    calls=[];var resolveLaunch;
    apiFetch=async (url,options={}) => {
      calls.push({url,options});
      if (options.method==='POST') return new Promise(resolve => {resolveLaunch=resolve;});
      return {ok:true,json:async () => url.endsWith('/jobs') ? [fixtureDe,fixtureTpe] : fixtureDe};
    };`);
  const launch=page.run('runOptimizer()');
  assert.equal(JSON.parse(page.run("calls.find(call => call.options.method==='POST').options.body")).method,'de');
  page.run("switchOptimizerSubTab('tpe')");
  const visibleSummary=page.element('oCurrentRun').innerHTML;
  const visibleResults=page.element('oResults').innerHTML;
  page.run("resolveLaunch({ok:true,json:async () => ({job_id:'delayed-de'})})");
  await launch;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(page.run('optimizerMethod'),'tpe');
  assert.equal(page.run('optimizerJobId'),'kept-tpe');
  assert.equal(page.run('_optimizerMethodStates.de.jobId'),'delayed-de');
  assert.equal(page.run('_optimizerMethodStates.tpe.jobId'),'kept-tpe');
  assert.equal(page.element('oCurrentRun').innerHTML,visibleSummary);
  assert.equal(page.element('oResults').innerHTML,visibleResults);
  assert.equal(page.run('optimizerPollTimer'),null);
  assert.equal(page.element('oRunBtn').style.display,'');

  page.run("switchOptimizerSubTab('de')");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(page.run('optimizerJobId'),'delayed-de');
  assert.match(page.element('oCurrentRun').innerHTML,/delayed-de/);
  assert.doesNotMatch(page.element('oCurrentRun').innerHTML,/kept-tpe/);
});

test('switching methods restores each draft including bounds and trial or generation budget', async () => {
  const page=dashboard();
  page.element('oCandles').value='1200';
  page.element('oMaxiter').value='75';
  page.element('op-lo-rsi_length').value='8';
  page.element('op-hi-rsi_length').value='25';
  await page.run("switchOptimizerSubTab('tpe')");
  assert.equal(page.run('optimizerMethod'),'tpe');
  page.element('oCandles').value='2200';
  page.element('oTrials').value='123';
  page.element('op-lo-rsi_length').value='12';
  page.element('op-hi-rsi_length').value='38';
  await page.run("switchOptimizerSubTab('de')");
  assert.equal(page.run('optimizerMethod'),'de');
  assert.equal(page.element('oCandles').value,'1200');
  assert.equal(page.element('oMaxiter').value,'75');
  assert.deepEqual(JSON.parse(JSON.stringify(page.run('_optimizerPayload().custom_bounds'))),{rsi_length:[8,25]});
  await page.run("switchOptimizerSubTab('tpe')");
  assert.equal(page.element('oCandles').value,'2200');
  assert.equal(page.element('oTrials').value,'123');
  assert.deepEqual(JSON.parse(JSON.stringify(page.run('_optimizerPayload().custom_bounds'))),{rsi_length:[12,38]});
});

test('TPE workload, saved settings and metric plots use trials without population convergence', () => {
  const page=dashboard();
  page.run("optimizerMethod='tpe'");
  page.element('oTrials').value='80';
  page.run('updateOptimizerNote()');
  assert.match(page.element('oWorkload').innerHTML,/80[\s\S]*trials/i);
  assert.doesNotMatch(page.element('oWorkload').innerHTML,/generations/i);
  page.context.fixtureJob=savedRun('tpe-live','tpe','paused',{iteration:4,evaluations:4,current_delta:null,
    history:[{iteration:1,pnl:null,win_rate:null,trade_count:null},{iteration:4,pnl:12,win_rate:60,trade_count:32}]});
  page.run('renderOptimizerProgress(fixtureJob)');
  assert.match(page.element('oCurrentRun').innerHTML,/80[\s\S]*trials/i);
  assert.match(page.element('oProgressCount').textContent,/trial\s*4\s*\/\s*80/i);
  assert.match(page.element('oProgressCurrent').textContent,/Paused at trial 4/i);
  const charts=page.element('oDeltaChart').innerHTML;
  assert.match(charts,/Best net P&L/);
  assert.match(charts,/trial 4/i);
  assert.doesNotMatch(charts,/Convergence|generation|gen 1/i);
  assert.equal((charts.match(/<svg\b/g) || []).length,3);
});

test('paired TPE side cards show each side trial budget', () => {
  const page=dashboard();
  page.context.fixturePair=savedRun('paired-tpe','tpe','paused',{
    paired:true,active_side:'short',credit:420,iteration:83,n_trials:80,
    sides:{
      long:savedRun('paired-tpe-long','tpe','done',{credit:120,iteration:80,n_trials:80,maxiter:0}),
      short:savedRun('paired-tpe-short','tpe','paused',{credit:300,iteration:3,n_trials:80,maxiter:0}),
    },
  });
  page.run("switchOptimizerSubTab('tpe');showOptimizerJob(fixturePair)");
  const cards=page.element('oPairResults').innerHTML;
  assert.match(cards,/Long[\s\S]*trial\s+80\/80/i);
  assert.match(cards,/Short[\s\S]*trial\s+3\/80/i);
  assert.doesNotMatch(cards,/\bgen\b|generation/i);
  assert.match(page.element('oProgressCount').textContent,/trial\s+83\/160/i);
});

test('paired legacy runs switch the held-out summary and trades with the selected side', async () => {
  const page=dashboard();
  const child=(id,side,pnl,testPnl) => savedRun(id,undefined,'done',{
    method:undefined,strategy:side==='Long' ? 'AdvRSILong' : 'AdvRSIShort',
    credit:side==='Long' ? 120 : 300,no_eligible_result:false,train_pct:70,test_candle_count:300,
    best_pnl:pnl,best_params:{rsi_length:14},best_summary:{total_pnl:pnl,win_rate:60,trade_count:30},
    params:[{name:'rsi_length',value:14,default:14,low:5,high:40}],
    test_summary:{total_pnl:testPnl,win_rate:side==='Long' ? 65 : 45,trade_count:side==='Long' ? 20 : 11,max_drawdown_pct:2},
    test_trades:[{'Trade #':1,Position:side,'Start Time':`${side.toLowerCase()}-held-out-trade`,'P&L ($)':testPnl}],
  });
  page.context.fixturePair=savedRun('legacy-pair',undefined,'done',{
    method:undefined,paired:true,active_side:'long',strategy:'AdvRSIIndependent',strategy_label:'Independent RSI',
    no_eligible_result:false,credit:420,train_pct:70,test_candle_count:300,combined_pnl:30,combined_test_pnl:33.75,
    sides:{long:child('legacy-pair-long','Long',12,42.5),short:child('legacy-pair-short','Short',18,-8.75)},
  });
  page.run(`apiFetch=async () => ({ok:true,json:async () => fixturePair});`);
  await page.run("selectOptimizerSavedRun('legacy-pair')");
  assert.equal(page.run('optimizerMethod'),'de');
  assert.match(page.element('oLegacyTest').innerHTML,/Saved held-out result/);
  assert.match(page.element('oLegacyTest').innerHTML,/\$42\.50/);
  assert.match(page.element('oLegacyTest').innerHTML,/long-held-out-trade/);
  assert.doesNotMatch(page.element('oLegacyTest').innerHTML,/short-held-out-trade/);

  page.run("selectIndependentSide('o','short')");
  assert.match(page.element('oLegacyTest').innerHTML,/\$-8\.75|-\$8\.75/);
  assert.match(page.element('oLegacyTest').innerHTML,/45\.0%/);
  assert.match(page.element('oLegacyTest').innerHTML,/short-held-out-trade/);
  assert.doesNotMatch(page.element('oLegacyTest').innerHTML,/\$42\.50|long-held-out-trade/);
  assert.equal(page.context.window._lastOptimizerJob.strategy,'AdvRSIShort');

  page.run("selectIndependentSide('o','long')");
  assert.match(page.element('oLegacyTest').innerHTML,/\$42\.50/);
  assert.match(page.element('oLegacyTest').innerHTML,/long-held-out-trade/);
  assert.doesNotMatch(page.element('oLegacyTest').innerHTML,/short-held-out-trade/);
});

test('saved-run cards search and filter the current method while escaping stored labels', () => {
  const page=dashboard();
  page.context.fixtureRuns=[
    savedRun('de-btc','de','done',{strategy_label:'Alpha <img src=x onerror=alert(1)>'}),
    savedRun('de-eth','de','error',{symbol:'ETHUSDT',strategy_label:'Beta RSI'}),
    savedRun('tpe-eth','tpe','done',{symbol:'ETHUSDT',strategy_label:'Gamma TPE'}),
    savedRun('legacy-de',undefined,'interrupted',{method:undefined,strategy_label:'Legacy run'}),
  ];
  page.run("optimizerSavedRuns=fixtureRuns;optimizerMethod='de';optimizerHistoryPage=1");
  page.element('optimizerHistorySearch').value='';
  page.element('optimizerHistoryFilter').value='all';
  page.run('renderOptimizerHistory()');
  let cards=page.element('optimizerHistoryList').innerHTML;
  assert.match(cards,/Alpha &lt;img/);
  assert.match(cards,/Legacy run/);
  assert.doesNotMatch(cards,/<img\b|Gamma TPE/);
  assert.match(cards,/selectOptimizerSavedRun/);
  assert.match(cards,/requestOptimizerRunDeletion/);
  assert.doesNotMatch(cards,/<button\b[^>]*>(?:(?!<\/button>)[\s\S])*?<button\b/,
    'Open and Delete must remain separate buttons');

  page.element('optimizerHistorySearch').value='eth';
  page.element('optimizerHistoryFilter').value='error';
  page.run('renderOptimizerHistory()');
  cards=page.element('optimizerHistoryList').innerHTML;
  assert.match(cards,/Beta RSI/);
  assert.doesNotMatch(cards,/Alpha|Legacy run|Gamma TPE/);
  page.run("optimizerMethod='tpe'");
  page.element('optimizerHistoryFilter').value='done';
  page.run('renderOptimizerHistory()');
  assert.match(page.element('optimizerHistoryList').innerHTML,/Gamma TPE/);
  assert.doesNotMatch(page.element('optimizerHistoryList').innerHTML,/Beta RSI|Alpha/);
});

test('saved-run browsing uses six-card pages and clamps both navigation ends', () => {
  const page=dashboard();
  page.context.fixtureRuns=Array.from({length:13},(_,index) => savedRun(`de-page-${index}`,'de','done',{
    strategy_label:`Saved page item ${index}`,created_at:1800000000-index,
  }));
  page.run("optimizerSavedRuns=fixtureRuns;optimizerMethod='de';optimizerHistoryPage=1");
  page.element('optimizerHistorySearch').value='';
  page.element('optimizerHistoryFilter').value='all';
  page.run('renderOptimizerHistory()');
  const actions=() => (page.element('optimizerHistoryList').innerHTML.match(/selectOptimizerSavedRun\(/g) || []).length;
  assert.equal(actions(),6);
  const firstPage=page.element('optimizerHistoryList').innerHTML;
  page.run('navigateOptimizerRuns(1)');
  assert.equal(page.run('optimizerHistoryPage'),2);
  assert.equal(actions(),6);
  assert.notEqual(page.element('optimizerHistoryList').innerHTML,firstPage);
  page.run('navigateOptimizerRuns(1)');
  assert.equal(page.run('optimizerHistoryPage'),3);
  assert.equal(actions(),1);
  page.run('navigateOptimizerRuns(1)');
  assert.equal(page.run('optimizerHistoryPage'),3);
  page.run('navigateOptimizerRuns(-10)');
  assert.equal(page.run('optimizerHistoryPage'),1);
  assert.equal(page.element('optimizerHistoryList').innerHTML,firstPage);
  page.run('navigateOptimizerRuns(1)');
  page.element('optimizerHistorySearch').value='Saved page item 12';
  page.run('renderOptimizerHistory()');
  assert.equal(page.run('optimizerHistoryPage'),1);
  assert.equal(actions(),1);
});

test('opening saved DE, TPE and legacy runs uses the common endpoint and restores method selection', async () => {
  const page=dashboard();
  page.context.fixtureRuns=[savedRun('de-saved'),savedRun('tpe-saved','tpe'),savedRun('legacy-saved',undefined,'interrupted',{method:undefined})];
  page.run(`optimizerSavedRuns=fixtureRuns;
    apiFetch=async (url,options={}) => {calls.push({url,options});
      return {ok:true,json:async () => fixtureRuns.find(job => url.endsWith('/'+job.id)) || fixtureRuns};};`);
  await page.run("selectOptimizerSavedRun('de-saved')");
  assert.equal(page.run('optimizerMethod'),'de');
  assert.equal(page.run('optimizerJobId'),'de-saved');
  await page.run("selectOptimizerSavedRun('tpe-saved')");
  assert.equal(page.run('optimizerMethod'),'tpe');
  assert.equal(page.run('optimizerJobId'),'tpe-saved');
  assert.match(page.element('oCurrentRun').innerHTML,/tpe-saved/);
  await page.run("switchOptimizerSubTab('de')");
  assert.equal(page.run('optimizerJobId'),'de-saved');
  assert.match(page.element('oCurrentRun').innerHTML,/de-saved/);
  await page.run("switchOptimizerSubTab('tpe')");
  assert.equal(page.run('optimizerJobId'),'tpe-saved');
  await page.run("selectOptimizerSavedRun('legacy-saved')");
  assert.equal(page.run('optimizerMethod'),'de');
  assert.equal(page.run('optimizerJobId'),'legacy-saved');
  assert.equal(page.run("calls.filter(call => call.url==='/api/optimizer/run/tpe-saved').length"),1);
  assert.equal(page.run("calls.some(call => call.url.includes('/tester/'))"),false);
});

test('deletion requires an inline confirmation and cancellation leaves the run untouched', async () => {
  const page=dashboard();
  page.context.fixtureRuns=[savedRun('delete-me','de','done',{strategy_label:'Delete <script>alert(1)</script>'})];
  page.run("optimizerSavedRuns=fixtureRuns;optimizerMethod='de';calls=[]");
  await page.run("requestOptimizerRunDeletion('delete-me')");
  assert.equal(page.run("calls.filter(call => call.options.method==='DELETE').length"),0);
  const confirmation=page.element('optimizerDeleteConfirm');
  assert.notEqual(confirmation.style.display,'none');
  assert.equal(confirmation.hidden,false);
  assert.match(confirmation.innerHTML+confirmation.textContent+page.element('optimizerDeleteMessage').textContent,/delete-me|Delete &lt;script&gt;/);
  assert.doesNotMatch(confirmation.innerHTML,/<script\b/);
  page.run('cancelOptimizerRunDeletion()');
  await page.run('confirmOptimizerRunDeletion()');
  assert.equal(page.run("calls.filter(call => call.options.method==='DELETE').length"),0);
  assert.equal(page.run('optimizerSavedRuns.length'),1);
});

test('confirmed deletion removes only the deleted saved run and clears its displayed results', async () => {
  const page=dashboard();
  page.context.fixtureRuns=[savedRun('delete-selected'),savedRun('keep-this','tpe')];
  page.run(`optimizerSavedRuns=fixtureRuns;
    apiFetch=async (url,options={}) => {calls.push({url,options});
      const data=options.method==='DELETE' ? {ok:true,deleted:'delete-selected'}
        : url.endsWith('/jobs') ? fixtureRuns.filter(job => job.id!=='delete-selected')
        : fixtureRuns.find(job => url.endsWith('/'+job.id));
      return {ok:true,status:200,json:async () => data};};`);
  await page.run("selectOptimizerSavedRun('delete-selected')");
  assert.match(page.element('oCurrentRun').innerHTML,/delete-selected/);
  await page.run("requestOptimizerRunDeletion('delete-selected')");
  assert.equal(page.run("calls.filter(call => call.options.method==='DELETE').length"),0);
  await page.run('confirmOptimizerRunDeletion()');
  assert.equal(page.run("calls.filter(call => call.options.method==='DELETE').length"),1);
  assert.equal(page.run("calls.find(call => call.options.method==='DELETE').url"),'/api/optimizer/jobs/delete-selected');
  assert.equal(page.run("optimizerSavedRuns.some(job => job.id==='delete-selected')"),false);
  assert.equal(page.run("optimizerSavedRuns.some(job => job.id==='keep-this')"),true);
  assert.equal(page.run('optimizerJobId'),null);
  assert.ok(page.element('oCurrentRun').style.display==='none' || !page.element('oCurrentRun').innerHTML.includes('delete-selected'),
    'Deleted selection must stop showing its saved result');
  assert.equal(page.element('oResults').innerHTML,'');
});

for (const status of ['pending','running','pausing','paused']) {
  test(`${status} runs cannot be deleted through the card or direct confirmation helpers`, async () => {
    const page=dashboard();
    page.context.fixtureRuns=[savedRun('active-run','de',status)];
    page.run("optimizerSavedRuns=fixtureRuns;optimizerMethod='de';calls=[];optimizerHistoryPage=1");
    page.element('optimizerHistoryFilter').value='all';
    page.run('renderOptimizerHistory()');
    const cards=page.element('optimizerHistoryList').innerHTML;
    const deletionButton=cards.match(/<button\b[^>]*onclick="[^"]*requestOptimizerRunDeletion[^"]*"[^>]*>/)?.[0];
    assert.ok(!deletionButton || /\bdisabled\b/.test(deletionButton),`${status} card must hide or disable Delete`);
    await page.run("requestOptimizerRunDeletion('active-run')");
    await page.run('confirmOptimizerRunDeletion()');
    assert.equal(page.run("calls.filter(call => call.options.method==='DELETE').length"),0);
    assert.equal(page.run('optimizerSavedRuns.length'),1);
  });
}

for (const failure of [
  {status:409,error:'Run is now running. Pause or cancel it first.'},
  {status:500,error:'Saved result could not be removed.'},
]) {
  test(`deletion HTTP ${failure.status} keeps the saved result and shows the server error`, async () => {
    const page=dashboard();
    page.context.fixtureRuns=[savedRun('delete-failed')];
    page.context.fixtureFailure=failure;
    page.run(`optimizerSavedRuns=fixtureRuns;optimizerMethod='de';
      apiFetch=async (url,options={}) => {calls.push({url,options});
        return {ok:false,status:fixtureFailure.status,json:async () => ({error:fixtureFailure.error})};};`);
    await page.run("requestOptimizerRunDeletion('delete-failed')");
    await page.run('confirmOptimizerRunDeletion()');
    assert.equal(page.run("calls.filter(call => call.options.method==='DELETE').length"),1);
    assert.equal(page.run('optimizerSavedRuns.length'),1);
    const feedback=page.element('optimizerHistoryStatus').textContent+page.element('optimizerDeleteConfirm').textContent+
      page.element('optimizerDeleteConfirm').innerHTML+page.run('toasts.map(toast => toast.message).join()');
    assert.ok(feedback.includes(failure.error),'Deletion failure must be visible');
  });
}

test('a method switch discards an in-flight polling response and its scheduled timer', async () => {
  const page=dashboard();
  page.context.fixtureJob=savedRun('de-poll','de','running');
  page.run(`optimizerMethod='de';optimizerJobId='de-poll';
    var resolvePoll;
    apiFetch=() => new Promise(resolve => {resolvePoll=resolve;});
    optimizerPollTimer=setTimeout(() => {},1000);`);
  const oldTimer=page.run('optimizerPollTimer');
  const polling=page.run('pollOptimizer()');
  await page.run("switchOptimizerSubTab('tpe')");
  assert.equal(page.timers.has(oldTimer),false);
  page.run('resolvePoll({ok:true,json:async () => fixtureJob})');
  await polling;
  assert.equal(page.run('optimizerMethod'),'tpe');
  assert.doesNotMatch(page.element('oCurrentRun').innerHTML,/de-poll/);
  assert.equal(page.run('optimizerPollTimer'),null);
});

test('a failed old polling request cannot restart polling after a method switch', async () => {
  const page=dashboard();
  page.run(`optimizerMethod='de';optimizerJobId='de-poll';
    var rejectOldPoll;
    apiFetch=() => new Promise((resolve,reject) => {rejectOldPoll=reject;});`);
  const polling=page.run('pollOptimizer()');
  await page.run("switchOptimizerSubTab('tpe')");
  const afterSwitch=[...page.timers.keys()];
  page.run("rejectOldPoll(new Error('Network request failed'))");
  await polling;
  assert.equal(page.run('optimizerMethod'),'tpe');
  assert.equal(page.run('optimizerPollTimer'),null);
  assert.deepEqual([...page.timers.keys()],afterSwitch,'An old failure must not schedule a retry for the new method');
});

test('a late DE pause response cannot change the idle TPE controls', async () => {
  const page=dashboard();
  page.run(`optimizerMethod='de';optimizerJobId='de-pausing';
    _setOptimizerRunning(true,'running',true);
    var resolvePause;
    apiFetch=(url,options={}) => {
      calls.push({url,options});
      return new Promise(resolve => {resolvePause=resolve;});
    };`);
  const pausing=page.run('toggleOptimizerPause()');
  assert.equal(page.run('calls[0].url'),'/api/optimizer/run/de-pausing/pause');
  page.run("switchOptimizerSubTab('tpe')");
  assert.equal(page.run('optimizerJobId'),null);
  assert.equal(page.element('oRunBtn').style.display,'');
  page.run('resolvePause({ok:true})');
  await pausing;
  assert.equal(page.run('optimizerMethod'),'tpe');
  assert.equal(page.run('optimizerJobId'),null);
  assert.equal(page.element('oRunBtn').style.display,'');
  assert.equal(page.element('oPauseBtn').style.display,'none');
  assert.equal(page.element('oCancelBtn').style.display,'none');
  assert.equal(page.element('oProgressWrap').style.display,'none');
  assert.equal(page.run('toasts.length'),0,'A late acknowledgement must not describe the current method as pausing');
});

test('an older saved-run response cannot overwrite the later selection', async () => {
  const page=dashboard();
  page.context.fixtureRuns=[savedRun('slow-de'),savedRun('fast-tpe','tpe')];
  page.run(`optimizerSavedRuns=fixtureRuns;
    var resolveOldSelection;
    apiFetch=async url => {
      if (url.endsWith('/slow-de')) return new Promise(resolve => {resolveOldSelection=resolve;});
      return {ok:true,json:async () => fixtureRuns[1]};
    };`);
  const older=page.run("selectOptimizerSavedRun('slow-de')");
  await page.run("selectOptimizerSavedRun('fast-tpe')");
  page.run('resolveOldSelection({ok:true,json:async () => fixtureRuns[0]})');
  await older;
  assert.equal(page.run('optimizerMethod'),'tpe');
  assert.equal(page.run('optimizerJobId'),'fast-tpe');
  assert.match(page.element('oCurrentRun').innerHTML,/fast-tpe/);
  assert.doesNotMatch(page.element('oCurrentRun').innerHTML,/slow-de/);
});

test('signing out clears optimizer drafts, results and timers before loading another account', async () => {
  const page=dashboard();
  page.context.fixtureDe=savedRun('private-de');
  page.context.fixturePair=savedRun('private-pair','tpe','paused',{
    paired:true,active_side:'long',credit:420,sides:{
      long:savedRun('private-long','tpe','done',{credit:120}),
      short:savedRun('private-short','tpe','paused',{credit:300}),
    },
  });
  page.run(`optimizerSavedRuns=[fixtureDe,fixturePair];showOptimizerJob(fixtureDe);
    document.getElementById('oCandles').value='1700';switchOptimizerSubTab('tpe');
    showOptimizerJob(fixturePair);selectIndependentSide('o','short');
    document.getElementById('oTrials').value='777';saveOptimizerPrefs();
    _optimizerControlsInitialized=true;
    optimizerPollTimer=setTimeout(() => {},1000);
    requestOptimizerRunDeletion('private-de');`);
  const pollTimer=page.run('optimizerPollTimer');
  const prefsTimer=page.run('_optimizerPrefsTimer');
  assert.ok(page.timers.has(pollTimer));
  assert.ok(page.timers.has(prefsTimer));
  assert.match(page.element('oPairResults').innerHTML,/Independent Long/);
  page.run('showLogin()');
  assert.equal(page.run('authenticated'),false);
  assert.equal(page.run('optimizerJobId'),null);
  assert.equal(page.run('optimizerSavedRuns.length'),0);
  assert.equal(page.run('Object.keys(optimizerConfig || {}).length'),0);
  assert.equal(page.run('_optimizerConfigLoaded'),false);
  assert.equal(page.run('_optimizerControlsInitialized'),false);
  assert.equal(page.run('Object.values(_optimizerMethodStates).every(state => state.draft===null && state.jobId===null && state.job===null)'),true);
  assert.equal(page.run('_independentJobs.o == null && _independentSelected.o == null && _independentRendered.o == null'),true);
  assert.equal(page.run('optimizerDeleteRunId'),null);
  assert.equal(page.element('optimizerDeleteConfirm').hidden,true);
  assert.equal(page.element('oPairResults').innerHTML,'');
  assert.equal(page.element('oResults').innerHTML,'');
  assert.equal(page.element('oLegacyTest').innerHTML,'');
  assert.equal(page.element('oCurrentRun').style.display,'none');
  assert.equal(page.context.window._lastOptimizerJob,null);
  assert.equal(page.timers.has(pollTimer),false);
  assert.equal(page.timers.has(prefsTimer),false);

  page.context.fixtureNextConfig={...config,method:'tpe',candles:2300,credit:250,n_trials:125};
  page.run(`authenticated=true;calls=[];
    apiFetch=async (url,options={}) => {calls.push({url,options});return {ok:true,json:async () => url.endsWith('/config') ? fixtureNextConfig : []};};`);
  await page.run('initOptimizerTab()');
  assert.equal(page.run("calls.some(call => call.url==='/api/optimizer/config')"),true);
  assert.equal(page.run('optimizerMethod'),'tpe');
  assert.equal(page.element('oCandles').value,'2300');
  assert.equal(page.element('oCredit').value,'250');
  assert.equal(page.element('oTrials').value,'125');
  page.run("switchOptimizerSubTab('de')");
  assert.equal(page.element('oCandles').value,'2300','The previous account DE draft must not return');
});

test('a launch response from a signed-out session cannot enter the next account state', async () => {
  const page=dashboard();
  page.context.fixtureNewJob=savedRun('new-account-run','tpe');
  page.run(`var resolveLaunchJson;
    apiFetch=async (url,options={}) => {calls.push({url,options});return {
      ok:true,json:() => new Promise(resolve => {resolveLaunchJson=resolve;})
    };};`);
  const launching=page.run('runOptimizer()');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(page.run('typeof resolveLaunchJson'),'function');
  page.run(`showLogin();authenticated=true;optimizerConfig=fixtureConfig;_optimizerConfigLoaded=true;
    populateOptimizerControls();onOptimizerStrategyChange();switchOptimizerSubTab('tpe');
    optimizerSavedRuns=[fixtureNewJob];showOptimizerJob(fixtureNewJob);renderOptimizerHistory();`);
  const newSummary=page.element('oCurrentRun').innerHTML;
  const newHistory=page.element('optimizerHistoryList').innerHTML;
  const callsBeforeResponse=page.run('calls.length');
  page.run("resolveLaunchJson({job_id:'old-account-launch'})");
  await launching;
  assert.equal(page.run('authenticated'),true);
  assert.equal(page.run('optimizerJobId'),'new-account-run');
  assert.equal(page.run('_optimizerMethodStates.de.jobId'),null);
  assert.equal(page.run('_optimizerMethodStates.tpe.jobId'),'new-account-run');
  assert.equal(page.element('oCurrentRun').innerHTML,newSummary);
  assert.equal(page.element('optimizerHistoryList').innerHTML,newHistory);
  assert.equal(page.run('calls.length'),callsBeforeResponse,'An obsolete launch must not refresh another account history');
  assert.equal(page.run('toasts.length'),0);
});

test('a deferred history body from a signed-out session cannot replace the next account runs', async () => {
  const page=dashboard();
  page.context.fixtureOldRuns=[savedRun('old-account-history')];
  page.context.fixtureNewRuns=[savedRun('new-account-history')];
  page.run(`var resolveHistoryJson;
    apiFetch=async (url,options={}) => {calls.push({url,options});return {
      ok:true,json:() => new Promise(resolve => {resolveHistoryJson=resolve;})
    };};`);
  const loading=page.run("loadResearchHistory('optimizer')");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(page.run('typeof resolveHistoryJson'),'function');
  page.run(`showLogin();authenticated=true;optimizerConfig=fixtureConfig;
    optimizerSavedRuns=fixtureNewRuns;renderOptimizerHistory();`);
  const newHistory=page.element('optimizerHistoryList').innerHTML;
  const newStatus=page.element('optimizerHistoryStatus').textContent;
  page.run('resolveHistoryJson(fixtureOldRuns)');
  await loading;
  assert.equal(page.run('optimizerSavedRuns.length'),1);
  assert.equal(page.run('optimizerSavedRuns[0].id'),'new-account-history');
  assert.equal(page.element('optimizerHistoryList').innerHTML,newHistory);
  assert.equal(page.element('optimizerHistoryStatus').textContent,newStatus);
  assert.doesNotMatch(page.element('optimizerHistoryList').innerHTML,/old-account-history/);
});

test('a paired bot creation response from a signed-out session cannot add bots or redraw old results', async () => {
  const page=dashboard();
  const pair=id => savedRun(id,'de','done',{
    paired:true,active_side:'long',credit:420,sides:{
      long:savedRun(`${id}-long`,'de','done',{credit:120,best_params:{rsi_length:14}}),
      short:savedRun(`${id}-short`,'de','done',{credit:300,best_params:{rsi_length:21}}),
    },
  });
  page.context.fixtureOldPair=pair('old-account-pair');
  page.context.fixtureNewPair=pair('new-account-pair');
  page.run(`showOptimizerJob(fixtureOldPair);
    var resolveCreationJson,pairRenders=[],gridRenders=0;
    var originalPairRenderer=renderIndependentResults;
    renderIndependentResults=(prefix,job) => {pairRenders.push(job?.id);return originalPairRenderer(prefix,job);};
    renderGrid=() => {gridRenders++;};
    apiFetch=async (url,options={}) => {calls.push({url,options});return {
      ok:true,json:() => new Promise(resolve => {resolveCreationJson=resolve;})
    };};`);
  const creating=page.run("createIndependentBots('o',true)");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(page.run('_independentCreating'),true);
  assert.equal(page.run('typeof resolveCreationJson'),'function');
  page.run('showLogin()');
  assert.equal(page.run('_independentCreating'),false,'Logout must release the previous account creation lock');
  page.run(`authenticated=true;optimizerConfig=fixtureConfig;showOptimizerJob(fixtureNewPair);
    bots=[{id:'new-account-bot'}];`);
  const newPairHtml=page.element('oPairResults').innerHTML;
  const rendersBeforeResponse=page.run('pairRenders.length');
  page.run("resolveCreationJson({bots:[{id:'old-account-created-bot',paired:true}]})");
  await creating;
  assert.equal(page.run('bots.length'),1);
  assert.equal(page.run('bots[0].id'),'new-account-bot');
  assert.equal(page.run('gridRenders'),0);
  assert.equal(page.run('pairRenders.length'),rendersBeforeResponse,'An obsolete creation must not render results in the new session');
  assert.equal(page.element('oPairResults').innerHTML,newPairHtml);
  assert.equal(page.run('_independentJobs.o.id'),'new-account-pair');
  assert.equal(page.run('_independentCreating'),false);
  assert.equal(page.run('toasts.length'),0);
});
