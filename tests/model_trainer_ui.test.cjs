const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0]
  .replace(/checkSession\(\)\.then\(active => \{ if \(active\) openDashboard\(\); \}\)[\s\S]*$/, '');
const modelId = 'a'.repeat(32);
const savedModel = {id:modelId,symbol:'btcusdt',timeframe:'60',horizon:4,lookback:96,
  architecture:'alexnet1d-v1',created_at:'2026-10-01T09:00:00Z',device:'cuda',
  validation_mae_bps:10,test_mae_bps:12,baseline_mae_bps:18,validation_passed:true};
const config = {architecture:'alexnet1d-v1',max_pairs:12,
  runtime:{torch_available:true,cuda_available:true,device_name:'NVIDIA test GPU',torch_version:'2.7',cuda_version:'12.8'},
  timeframes:[{value:'15',label:'15m'},{value:'60',label:'1h'},{value:'240',label:'4h'},{value:'D',label:'1d'}],
  defaults:{symbols:['btcusdt'],timeframes:['60'],candles:2000,lookback:96,horizon:4,epochs:10,batch_size:32,learning_rate:.001,device:'cuda'}};

function dashboard() {
  const elements = new Map(), timers = new Map();
  let nextTimer = 0, symbols = [], timeframes = [], specialists = [];
  function classes(initial = []) {
    const values = new Set(initial);
    return {add:name => values.add(name),remove:name => values.delete(name),contains:name => values.has(name)};
  }
  const tabs = ['Bots','AIBots','ModelTrainer','PortfolioManager','Optimizer'].map(view => ({dataset:{view},classList:classes()}));
  function checkboxes(markup, name) {
    return [...markup.matchAll(new RegExp(`<input\\b[^>]*class="${name}"[^>]*>`, 'g'))].map(([tag]) => ({
      value:tag.match(/value="([^"]*)"/)?.[1] || '',checked:/\schecked(?:\s|>)/.test(tag),disabled:false,
    }));
  }
  function element(id) {
    if (elements.has(id)) return elements.get(id);
    let markup = '', value = '';
    const attributes = {};
    const item = {id,textContent:'',style:{},dataset:{},options:[],checked:false,disabled:false,
      classList:classes(id === 'viewModelTrainer' ? ['active'] : []),
      setAttribute:(key,v) => attributes[key] = v,removeAttribute:key => delete attributes[key],
      querySelectorAll:() => [],focus() {},};
    Object.defineProperty(item,'value',{get:() => value,set:v => {value=String(v);}});
    Object.defineProperty(item,'selectedIndex',{get:() => item.options.findIndex(option => option.value === value),set:index => {value=item.options[index]?.value || '';}});
    Object.defineProperty(item,'selectedOptions',{get:() => item.options.filter(option => option.value === value)});
    Object.defineProperty(item,'innerHTML',{get:() => markup,set:v => {
      markup=String(v);
      if (id === 'trainerSymbols') symbols=checkboxes(markup,'trainer-symbol');
      if (id === 'trainerTimeframes') timeframes=checkboxes(markup,'trainer-timeframe');
      if (id === 'trainerSpecialistTimeframes') specialists=checkboxes(markup,'trainer-specialist-timeframe');
      if (markup.includes('<option')) {
        item.options=[...markup.matchAll(/<option\b([^>]*)>([^<]*)<\/option>/g)].map(([,attrs,label]) => ({
          value:attrs.match(/value="([^"]*)"/)?.[1] || '',textContent:label,dataset:{},hidden:false,
          selected:/\sselected(?:\s|$)/.test(attrs),
        }));
        value=(item.options.find(option => option.selected) || item.options[0])?.value || '';
      }
    }});
    const select = html.match(new RegExp(`<select\\b[^>]*id="${id}"[^>]*>([\\s\\S]*?)<\\/select>`));
    if (select) item.innerHTML=select[1];
    const input = html.match(new RegExp(`<input\\b[^>]*id="${id}"[^>]*>`));
    if (input) {item.value=input[0].match(/value="([^"]*)"/)?.[1] || '';item.checked=/\schecked(?:\s|>)/.test(input[0]);}
    elements.set(id,item);
    return item;
  }
  const document = {getElementById:element,addEventListener() {},
    querySelectorAll(selector) {
      if (selector.startsWith('.trainer-symbol')) return selector.includes(':checked') ? symbols.filter(input => input.checked) : symbols;
      if (selector.startsWith('.trainer-timeframe')) return selector.includes(':checked') ? timeframes.filter(input => input.checked) : timeframes;
      if (selector.startsWith('.trainer-specialist-timeframe')) return selector.includes(':checked') ? specialists.filter(input => input.checked) : specialists;
      if (selector === '.main-tab-btn') return tabs;
      if (selector === '.view-pane') return tabs.map(tab => element('view' + tab.dataset.view));
      return [];
    },
    querySelector(selector) {
      const view=selector.match(/data-view="([^"]+)"/)?.[1];
      if (view) return tabs.find(tab => tab.dataset.view === view);
      if (selector.includes('name="fMode"')) return {value:element('fModeReal').checked ? 'real' : 'simulation'};
      if (selector.includes('name="eMode"')) return {value:element('eModeReal').checked ? 'real' : 'simulation'};
      return null;
    },
  };
  const context=vm.createContext({document,window:{fetch() {}},console,Date,
    setTimeout(callback,delay) {const id=++nextTimer;timers.set(id,{callback,delay});return id;},
    clearTimeout:id => timers.delete(id),setInterval:() => 0,clearInterval() {},});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'session.js'), 'utf8'),context);
  vm.runInContext(script,context);
  const run=code => vm.runInContext(code,context);
  run(`authenticated=true; var toasts=[];showToast=(message,kind) => toasts.push({message,kind});`);
  context.fixtureConfig=config;
  context.fixtureModel=savedModel;
  run(`var calls=[];apiFetch=async (url,options={}) => {
    calls.push({url,options});
    const value=url.endsWith('/config') ? fixtureConfig : url.endsWith('/models') ? {models:[fixtureModel]} : url.endsWith('/jobs') ? {jobs:[]} : {ok:true,markets:[]};
    return {ok:true,json:async () => value};
  };`);
  return {element,context,run,timers,symbols:() => symbols,timeframes:() => timeframes,specialists:() => specialists};
}

const modelCatalog = [
  {id:'alexnet1d-v1',label:'AlexNet CNN',requires_torch:true,available:true,defaults:config.defaults,supported_timeframes:['15','60','240','D'],bot_compatible:true},
  {id:'transformer-ohlcv-v1',label:'Transformer OHLCV',requires_torch:true,available:true,defaults:{...config.defaults,candles:10000,lookback:100,horizon:10,epochs:30,batch_size:16,learning_rate:.0003},supported_timeframes:['15','60','240','D'],bot_compatible:false},
  {id:'price-ai-hgbr-v1',label:'PriceAI',requires_torch:false,available:true,min_candles:650,defaults:{...config.defaults,candles:3000,device:'cpu',epochs:1},supported_timeframes:['60','240'],bot_compatible:false},
  {id:'agentic-ai-mtf-hgb-v1',label:'Agentic AI',requires_torch:false,available:true,min_candles:1500,defaults:{...config.defaults,candles:10000,timeframes:['60'],specialist_timeframes:['60','240'],fee_bps:10,exit_fee_bps:10,slippage_bps:5,device:'cpu'},supported_timeframes:['60','240'],bot_compatible:false},
  {id:'spot-hgb-v1',label:'Spot HGB',requires_torch:false,available:true,min_candles:800,defaults:{...config.defaults,candles:10000,timeframes:['60'],horizon:72,device:'cpu'},supported_timeframes:['60'],bot_compatible:false},
  {id:'spot-lightgbm-v1',label:'Spot LightGBM',requires_torch:false,available:true,min_candles:800,defaults:{...config.defaults,candles:10000,timeframes:['60'],horizon:72,device:'cpu'},supported_timeframes:['60'],bot_compatible:false},
];

async function catalogDashboard(runtime = config.runtime) {
  const page = dashboard();
  page.context.fixtureConfig = {...config,runtime,models:modelCatalog};
  await page.run('loadModelTrainer()');
  return page;
}

function deferTrainingStart(page) {
  page.run(`var finishStart;var acceptedStatus='running';apiFetch=async (url,options={}) => {
    calls.push({url,options});
    if (url === '/api/model-trainer/run') return new Promise(resolve => finishStart=resolve);
    if (url.endsWith('/cancel')) acceptedStatus='cancelled';
    const job={id:'accepted-run',status:acceptedStatus,config:{architecture:'alexnet1d-v1'},history:[]};
    const value=url.endsWith('/config') ? fixtureConfig : url.endsWith('/models') ? {models:[fixtureModel]} : url.endsWith('/jobs') ? {jobs:[]} : job;
    return {ok:true,json:async () => value};
  };var pendingStart=startModelTraining()`);
}

test('trainer tab loads GPU hardware, checkbox defaults and saved model metrics', async () => {
  const page=dashboard();
  await page.run('loadModelTrainer()');
  assert.match(html,/data-view="ModelTrainer"/);
  assert.match(page.element('trainerRuntime').textContent,/NVIDIA test GPU.*CUDA 12.8/);
  assert.equal(page.element('trainerRunBtn').disabled,false);
  assert.equal(page.element('trainerDevice').value,'cuda');
  assert.equal(page.run("trainerChecked('.trainer-symbol').join()"),'btcusdt');
  assert.equal(page.run("trainerChecked('.trainer-timeframe').join()"),'60');
  assert.match(page.element('trainerModels').innerHTML,/10.00 bps.*12.00 bps.*18.00 bps/);
});

test('CUDA unavailability keeps GPU selection blocked until CPU is selected explicitly', async () => {
  const page=dashboard();
  page.context.fixtureConfig={...config,runtime:{torch_available:true,cuda_available:false,torch_version:'2.7'}};
  await page.run('loadModelTrainer()');
  assert.equal(page.element('trainerDevice').value,'cuda');
  assert.equal(page.element('trainerRunBtn').disabled,true);
  assert.throws(() => page.run('trainingPayload()'),/device is unavailable/);
  page.element('trainerDevice').value='cpu';
  page.run('updateTrainerControls()');
  assert.equal(page.element('trainerRunBtn').disabled,false);
  assert.equal(page.run('trainingPayload().device'),'cpu');
});

test('missing Docker PyTorch explains the server environment and container setup', async () => {
  const page=dashboard();
  page.context.fixtureConfig={...config,runtime:{torch_available:false,torch_installed:false,cuda_available:false,
    runtime_environment:'docker',python_executable:'/usr/local/bin/python',error:'PyTorch is not installed in this Python environment.'}};
  await page.run('loadModelTrainer()');
  assert.match(page.element('trainerRuntimeBadge').textContent,/missing.*Docker/i);
  assert.match(page.element('trainerRuntime').textContent,/Docker.*\/usr\/local\/bin\/python/);
  assert.match(page.element('trainerRuntime').textContent,/host.*not.*available.*container/i);
  assert.match(page.element('trainerRuntime').textContent,/docker compose -f compose\.yaml -f compose\.cuda\.yaml up --build -d dashboard/);
  assert.equal(page.element('trainerRunBtn').disabled,true);
});

test('installed PyTorch load errors show the cause without claiming it is absent', async () => {
  const page=dashboard();
  page.context.fixtureConfig={...config,runtime:{torch_available:false,torch_installed:true,cuda_available:false,
    runtime_environment:'python',python_executable:'C:\\venv\\Scripts\\python.exe',error:'DLL load failed: missing dependency'} };
  await page.run('loadModelTrainer()');
  assert.match(page.element('trainerRuntimeBadge').textContent,/load failed/i);
  assert.match(page.element('trainerRuntime').textContent,/DLL load failed: missing dependency/);
  assert.match(page.element('trainerRuntime').textContent,/C:\\venv\\Scripts\\python\.exe/);
  assert.doesNotMatch(page.element('trainerRuntime').textContent,/not installed/i);
  assert.equal(page.element('trainerRunBtn').disabled,true);
});

test('missing runtime data stays unknown instead of reporting PyTorch unavailable', async () => {
  const page=dashboard();
  page.context.fixtureConfig={...config,runtime:{}};
  await page.run('loadModelTrainer()');
  assert.match(page.element('trainerRuntimeBadge').textContent,/unknown/i);
  assert.equal(page.element('trainerRunBtn').disabled,true);
});

test('training request includes selected coins, multiple timeframes and the adjustable horizon', async () => {
  const page=dashboard();
  await page.run('loadModelTrainer()');
  page.symbols().find(item => item.value === 'ethusdt').checked=true;
  page.timeframes().find(item => item.value === '240').checked=true;
  page.element('trainerHorizon').value='8';
  const payload=JSON.parse(JSON.stringify(page.run('trainingPayload()')));
  assert.deepEqual(payload.symbols,['btcusdt','ethusdt']);
  assert.deepEqual(payload.timeframes,['60','240']);
  assert.equal(payload.horizon,8);
  assert.equal(payload.candles,2000);
  assert.equal(payload.batch_size,32);
  assert.equal(payload.device,'cuda');
});

test('invalid training settings and too many pairs never start an API request', async () => {
  const page=dashboard();
  await page.run('loadModelTrainer()');
  page.run('calls=[]');
  page.element('trainerCandles').value='499';
  await page.run('startModelTraining()');
  assert.equal(page.run('calls.length'),0);
  assert.match(page.element('trainerFeedback').textContent,/500 to 20000/);
  page.element('trainerCandles').value='2000';
  for (const input of page.symbols()) input.checked=true;
  for (const input of page.timeframes()) input.checked=true;
  await page.run('startModelTraining()');
  assert.equal(page.run('calls.length'),0);
  assert.match(page.element('trainerFeedback').textContent,/12 coin\/timeframe/);
});

test('saved active run recovers on reload and polling stops when leaving the trainer tab', async () => {
  const page=dashboard();
  page.run(`apiFetch=async url => ({ok:true,json:async () => url.endsWith('/config') ? fixtureConfig : url.endsWith('/models') ? {models:[]} : {jobs:[{id:'run-1',status:'running',stage:'training',current_pair:{symbol:'btcusdt',timeframe:'60'},pair_index:1,pair_count:2,epoch:3,epochs:10,progress:15,history:[{epoch:1,train_loss:.2,val_loss:.3},{epoch:3,train_loss:.1,val_loss:.2}]}]}})`);
  await page.run('loadModelTrainer()');
  assert.equal(page.run('trainerJobId'),'run-1');
  assert.equal(page.element('trainerProgress').value,'15');
  assert.match(page.element('trainerRunStatus').textContent,/BTCUSDT.*1h.*Epoch 3\/10/);
  assert.match(page.element('trainerLossChart').innerHTML,/Training and validation loss by epoch/);
  assert.equal(page.element('trainerCancelBtn').disabled,false);
  assert.equal(page.timers.size,1);
  page.run("switchView('Bots')");
  assert.equal(page.timers.size,0);
});

test('finished and interrupted runs show final state, disable cancellation and retain error details', () => {
  const page=dashboard();
  page.run(`renderTrainerJob({id:'run-2',status:'interrupted',stage:'interrupted',error:'Training stopped when the server restarted.',history:[]})`);
  assert.equal(page.element('trainerCancelBtn').disabled,true);
  assert.match(page.element('trainerRunError').textContent,/server restarted/);
  page.run(`renderTrainerJob({id:'run-2',status:'done',stage:'done',progress:100,history:[]})`);
  assert.equal(page.element('trainerProgress').value,'100');
  assert.equal(page.element('trainerCancelBtn').disabled,true);
  assert.equal(page.element('trainerRunError').textContent,'');
});

function lossCoordinates(markup, key) {
  const path=markup.match(new RegExp(`<path data-series="${key}" d="([^"]*)"`))?.[1] || '';
  return [...path.matchAll(/([ML])([\d.]+),([\d.]+)/g)].map(([,command,x,y]) => ({command,x:Number(x),y:Number(y)}));
}

test('loss chart uses actual epoch coordinates, shared zero-based loss axes and point values', () => {
  const page=dashboard();
  page.run(`renderTrainerLoss([{epoch:10,train_loss:.01,val_loss:.025},{epoch:1,train_loss:.03,val_loss:.04},{epoch:3,train_loss:.02,val_loss:.03}])`);
  const chart=page.element('trainerLossChart').innerHTML;
  const training=lossCoordinates(chart,'train_loss'), validation=lossCoordinates(chart,'val_loss');
  assert.equal(training.length,3);
  assert.ok(Math.abs((training[1].x-training[0].x)/(training[2].x-training[0].x)-2/9)<.001,'epochs 1, 3 and 10 must retain their uneven spacing');
  assert.equal(training[0].y,validation[1].y,'the same loss must have the same coordinate on both series');
  assert.match(chart,/MSE \(dimensionless\)/);
  assert.match(chart,/text-anchor="middle"[^>]*>Epoch<\/text>/);
  assert.match(chart,/dominant-baseline="middle"[^>]*>0<\/text>/);
  assert.match(chart,/dominant-baseline="middle"[^>]*>0\.02<\/text>/);
  const grid=[...chart.matchAll(/<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"/g)];
  assert.ok(grid.filter(([,x1,y1,x2,y2]) => x1===x2 && y1!==y2).length>=3,'epoch coordinates need a vertical grid');
  assert.ok(grid.filter(([,x1,y1,x2,y2]) => y1===y2 && x1!==x2).length>=3,'loss coordinates need a horizontal grid');
  assert.match(chart,/<title>Epoch 3 · Training MSE: 0\.02<\/title>/);
  assert.match(chart,/<title>Epoch 10 · Validation MSE: 0\.025<\/title>/);
});

test('loss chart shows a single epoch and tiny, constant or zero losses without rounding away coordinates', () => {
  const page=dashboard();
  for (const value of [0,1e-8,.25,1e308]) {
    page.context.lossFixture=value;
    page.run('renderTrainerLoss([{epoch:7,train_loss:lossFixture,val_loss:lossFixture}])');
    const chart=page.element('trainerLossChart').innerHTML;
    const training=lossCoordinates(chart,'train_loss'), validation=lossCoordinates(chart,'val_loss');
    const [,left,top,width,height]=chart.match(/<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"/).map(Number);
    assert.equal(training.length,1);
    assert.deepEqual(training,validation);
    assert.ok(training[0].x>left && training[0].x<left+width,'a single epoch should be visible inside the plot');
    assert.ok(training[0].y>=top && training[0].y<=top+height);
    assert.doesNotMatch(chart,/NaN|Infinity/);
    assert.match(chart,/<title>Epoch 7 · Training MSE:/);
    if (value===1e-8) assert.match(chart,/1\.000000e-8/,'coordinate tooltip must preserve a tiny nonzero loss');
  }
});

test('missing and invalid losses remain gaps while valid metrics retain their independent series', () => {
  const page=dashboard();
  page.run(`renderTrainerLoss([{epoch:1,train_loss:.1,val_loss:.2},{epoch:2,train_loss:.05,val_loss:null},{epoch:3,train_loss:.025,val_loss:.1},{epoch:4,train_loss:-1,val_loss:''},{epoch:5,train_loss:Infinity,val_loss:true},{epoch:null,train_loss:0,val_loss:0}])`);
  const chart=page.element('trainerLossChart').innerHTML;
  assert.deepEqual(lossCoordinates(chart,'train_loss').map(point => point.command),['M','L','L']);
  assert.deepEqual(lossCoordinates(chart,'val_loss').map(point => point.command),['M','M'],'a missing validation observation must break the line');
  assert.doesNotMatch(chart,/<title>Epoch [245] · Validation MSE:|<title>Epoch [45] · Training MSE:|Epoch 0 ·|NaN|Infinity/);
  assert.match(chart,/Training · epoch 3: 0\.025/);
  assert.match(chart,/Validation · epoch 3: 0\.1/);
  page.run('renderTrainerLoss([{epoch:1,train_loss:null,val_loss:NaN}])');
  assert.match(page.element('trainerLossChart').innerHTML,/after the first epoch/);
  assert.doesNotMatch(page.element('trainerLossChart').innerHTML,/<svg/);
});

test('model selector accepts only validated models matching market and timeframe', () => {
  const page=dashboard();
  page.run(`savedAIModels=[fixtureModel,{...fixtureModel,id:'bad',validation_passed:false},{...fixtureModel,id:'eth',symbol:'ethusdt'},{...fixtureModel,id:'daily',timeframe:'D'}]`);
  page.element('fStrategy').value='AlexNet';
  page.element('fSymbol').value='btcusdt';
  page.element('fTimeframe').value='60';
  page.element('fAIModel').dataset.desiredModel=modelId;
  page.run("updateAIModelSelector('f')");
  assert.equal(page.element('fAIModel').value,modelId);
  assert.equal(page.run("aiModelSelectionError('f')"),'');
  assert.doesNotMatch(page.element('fAIModel').innerHTML,/value="bad"|value="eth"|value="daily"/);
  page.element('fTimeframe').value='15';
  page.run("onAITimeframeChange('f')");
  assert.equal(page.element('fAIModel').value,'');
  assert.match(page.run("aiModelSelectionError('f')"),/matching this market and timeframe/);
});

test('failed validation disables Use in AI Bot and rejects direct draft creation', () => {
  const page=dashboard();
  page.run(`savedAIModels=[{...fixtureModel,validation_passed:false}];renderSavedAIModels();useTrainedModel(fixtureModel.id)`);
  assert.match(page.element('trainerModels').innerHTML,/disabled>Use in AI Bot/);
  assert.equal(page.element('createModal').classList.contains('open'),false);
  assert.match(page.run('toasts[0].message'),/not passed validation/);
});

test('Use in AI Bot drafts the right model and stale market refresh cannot change its symbol', async () => {
  const page=dashboard();
  page.run(`savedAIModels=[fixtureModel];var finishMarkets;apiFetch=async (url,options={}) => {
    calls.push({url,options});
    if (url==='/api/margin-markets') return new Promise(resolve => finishMarkets=resolve);
    return {ok:true,json:async () => url.endsWith('/models') ? {models:[fixtureModel]} : {ok:false,error:'no_credentials'}};
  };useTrainedModel(fixtureModel.id);`);
  assert.equal(page.element('fStrategy').value,'AlexNet');
  assert.equal(page.element('fTimeframe').value,'60');
  assert.equal(page.element('fAIModel').value,modelId);
  assert.equal(page.element('fSymbol').value,'btcusdt');
  assert.equal(page.element('fModeSim').checked,true);
  assert.equal(page.element('fModeReal').disabled,true);
  assert.equal(page.element('fLeverage').value,'1');
  assert.equal(page.element('createModal').classList.contains('open'),true);
  assert.equal(page.run("calls.some(call => call.url.includes('/start'))"),false);
  await page.run(`finishMarkets({ok:true,json:async () => ({ok:true,source:'live_auth',markets:[{symbol:'ZECUSDT',dstCurrency:'usdt',buyEnabled:true,sellEnabled:true,feeRate:0}]})});Promise.resolve().then(() => Promise.resolve())`);
  assert.equal(page.element('fSymbol').value,'btcusdt');
  assert.equal(page.element('fAIModel').value,modelId);
});

test('prediction preview displays price, target time and horizon without placing orders', async () => {
  const page=dashboard();
  page.run(`apiFetch=async (url,options) => {calls.push({url,options});return {ok:true,json:async () => ({symbol:'btcusdt',timeframe:'60',horizon:4,last_price:100,predicted_price:102,forecast_return_bps:198.02,target_time:'2026-10-01T14:00:00Z',candle_time:'2026-10-01T09:00:00Z'})}}`);
  await page.run(`previewTrainedModel('${modelId}')`);
  assert.match(page.element('trainerPrediction').textContent,/predicted 102 USDT.*198.02 bps; 4 candles/);
  assert.equal(page.run('calls.length'),1);
  assert.match(page.run('calls[0].url'),/\/models\/.+\/predict$/);
  assert.equal(page.run('calls[0].options.method'),'POST');
});

test('AlexNet remains in AI bot lists and shows price evidence rather than probabilities', async () => {
  const page=dashboard();
  assert.equal(page.run("isAIBot({strategy:'AlexNet'})"),true);
  page.run(`apiFetch=async () => ({ok:true,json:async () => ({name:'CNN',strategy:'AlexNet',timeframe:'60',ai_model_id:fixtureModel.id,ai_model:fixtureModel,ai_decisions:[{candle_time:'2026-10-01',predicted_price:102,forecast_return_bps:198,action:'abstain',reason:'<unsafe>'}]})})`);
  await page.run("showAIEvidence('bot-1')");
  assert.match(page.element('aiEvidenceBody').innerHTML,/Predicted price \(USDT\).*102.*198.00 bps/);
  assert.match(page.element('aiEvidenceBody').innerHTML,/&lt;unsafe&gt;/);
  assert.doesNotMatch(page.element('aiEvidenceBody').innerHTML,/specialist probabilities/);
});

test('AlexNet create and edit requests preserve the selected saved model ID', async () => {
  const page=dashboard();
  page.run(`savedAIModels=[fixtureModel];loadMarginMarkets=async () => {};
    apiFetch=async (url,options={}) => {calls.push({url,options});return {ok:true,json:async () => ({models:[fixtureModel]})}};
    useTrainedModel(fixtureModel.id);`);
  await page.run('submitCreate()');
  const created=JSON.parse(page.run("calls.find(call => call.url === '/api/bots').options.body"));
  assert.equal(created.ai_model_id,modelId);
  assert.equal(created.strategy_name,'AlexNet');
  assert.equal(created.symbol,'btcusdt');
  assert.equal(created.mode,'simulation');
  assert.equal(created.leverage,1);
  page.run(`bots=[{id:'cnn-1',name:'Saved CNN',strategy:'AlexNet',symbol:'BTCUSDT',timeframe:'60',ai_model_id:fixtureModel.id,credit:100,leverage:1,mode:'simulation',status:'idle'}];openEditModal('cnn-1')`);
  await page.run('Promise.resolve().then(() => Promise.resolve())');
  assert.equal(page.element('eAIModel').value,modelId);
  await page.run('submitEdit()');
  const updated=JSON.parse(page.run("calls.find(call => call.url === '/api/bots/cnn-1').options.body"));
  assert.equal(updated.ai_model_id,modelId);
  assert.equal(updated.mode,'simulation');
});

test('pending training submission suppresses duplicates and restores controls after failure', async () => {
  const page=dashboard();
  await page.run('loadModelTrainer()');
  page.run(`calls=[];var rejectStart;apiFetch=(url,options) => {calls.push({url,options});return new Promise((resolve,reject) => rejectStart=reject)};var firstStart=startModelTraining()`);
  await page.run('startModelTraining()');
  assert.equal(page.run('calls.length'),1);
  assert.equal(page.element('trainerRunBtn').disabled,true);
  await page.run(`rejectStart(new Error('Connection interrupted'));firstStart`);
  assert.equal(page.element('trainerRunBtn').disabled,false);
  assert.match(page.element('trainerFeedback').textContent,/Connection interrupted/);
});

test('logout stops polling, clears account models and ignores a late trainer response', async () => {
  const page=dashboard();
  await page.run('loadModelTrainer()');
  page.run(`renderTrainerJob({id:'old-account-run',status:'running',history:[]});scheduleTrainerPolling();
    var finishOldPoll;apiFetch=async () => new Promise(resolve => finishOldPoll=resolve);var oldPoll=pollTrainerJob();showLogin();`);
  assert.equal(page.timers.size,0);
  assert.equal(page.run('savedAIModels.length'),0);
  assert.equal(page.run('trainerJobId'),null);
  await page.run(`finishOldPoll({ok:true,json:async () => ({id:'old-account-run',status:'running',progress:95})});oldPoll`);
  assert.equal(page.run('trainerJobId'),null);
  assert.equal(page.timers.size,0);
  assert.match(page.element('trainerModels').innerHTML,/Sign in to load/);
});

test('catalog provides six isolated family tabs and preserves each family draft', async () => {
  const page = await catalogDashboard();
  const tabs = page.element('trainerFamilyTabs').innerHTML;
  assert.equal((tabs.match(/class="trainer-family-tab"/g) || []).length,6);
  assert.doesNotMatch(tabs,/class="tab-btn/,'trainer tabs must not use history tab handlers');
  page.element('trainerCandles').value = 5000;
  page.run("selectTrainerFamily('transformer-ohlcv-v1')");
  assert.equal(page.element('trainerLookback').value,'100');
  assert.equal(page.element('trainerHorizon').value,'10');
  assert.equal(page.element('trainerBatchSize').value,'16');
  assert.equal(page.element('trainerLearningRate').value,'0.0003');
  assert.equal(page.element('trainerLookback').disabled,true);
  page.element('trainerCandles').value = 12000;
  page.run("selectTrainerFamily('price-ai-hgbr-v1')");
  assert.equal(page.element('trainerCandles').value,'3000');
  assert.equal(page.element('trainerNeuralControls').style.display,'none');
  page.element('trainerCandles').value = 3500;
  page.run("selectTrainerFamily('alexnet1d-v1')");
  assert.equal(page.element('trainerCandles').value,'5000');
  page.run("selectTrainerFamily('transformer-ohlcv-v1')");
  assert.equal(page.element('trainerCandles').value,'12000');
  assert.equal(page.run('trainingPayload().architecture'),'transformer-ohlcv-v1');
  page.run("selectTrainerFamily('price-ai-hgbr-v1')");
  assert.equal(page.element('trainerCandles').value,'3500');
});

test('CPU tree families train without PyTorch and unavailable dependencies stay blocked', async () => {
  const page = await catalogDashboard({torch_available:false,cuda_available:false});
  assert.equal(page.element('trainerRunBtn').disabled,true);
  page.run("selectTrainerFamily('price-ai-hgbr-v1')");
  assert.equal(page.element('trainerRunBtn').disabled,false);
  assert.match(page.element('trainerRuntime').textContent,/CPU.*PyTorch.*not required/);
  const payload = JSON.parse(JSON.stringify(page.run('trainingPayload()')));
  assert.equal(payload.architecture,'price-ai-hgbr-v1');
  assert.equal(payload.device,'cpu');
  assert.equal(payload.horizon,4);
  assert.equal(payload.epochs,1);
  assert.equal(payload.batch_size,undefined);
  assert.equal(payload.learning_rate,undefined);
  assert.equal(page.element('trainerCandles').min,650);
  page.element('trainerCandles').value = 500;
  assert.throws(() => page.run('trainingPayload()'),/650 to 20000/);
  page.element('trainerCandles').value = 3000;
  page.run("selectTrainerFamily('spot-hgb-v1')");
  assert.equal(page.run('trainingPayload().horizon'),72);
  assert.equal(page.timeframes().length,1);
  assert.equal(page.timeframes()[0].value,'60');
  page.run("trainerConfig.models=trainerConfig.models.map(item => item.id === 'spot-lightgbm-v1' ? {...item,available:false,error:'Install LightGBM'} : item);selectTrainerFamily('spot-lightgbm-v1')");
  assert.equal(page.element('trainerRunBtn').disabled,true);
  assert.throws(() => page.run('trainingPayload()'),/Install LightGBM/);
});

test('Agentic requests separate primary and specialist timeframes and positive cost assumptions', async () => {
  const page = await catalogDashboard({torch_available:false,cuda_available:false});
  page.run("selectTrainerFamily('agentic-ai-mtf-hgb-v1')");
  const payload = JSON.parse(JSON.stringify(page.run('trainingPayload()')));
  assert.deepEqual(payload.timeframes,['60']);
  assert.deepEqual(payload.specialist_timeframes,['60','240']);
  assert.equal(payload.fee_bps,10);
  assert.equal(payload.exit_fee_bps,10);
  assert.equal(payload.slippage_bps,5);
  assert.equal(payload.horizon,4);
  assert.equal(page.element('trainerSpecialistControls').style.display,'');
  page.element('trainerFeeBps').value = 0;
  assert.throws(() => page.run('trainingPayload()'),/Fees and slippage must be positive/);
  page.element('trainerFeeBps').value = 10;
  page.specialists().find(item => item.value === '15').checked = true;
  assert.throws(() => page.run('trainingPayload()'),/primary must be shortest/);
});

test('family run and model lists remain separate while cancellation covers the account active job', async () => {
  const page = await catalogDashboard();
  page.run(`renderTrainerJob({id:'cnn-active',status:'running',config:{architecture:'alexnet1d-v1'},history:[]});
    renderTrainerJob({id:'tree-done',status:'done',config:{architecture:'price-ai-hgbr-v1'},history:[]});
    savedAIModels=[fixtureModel,{...fixtureModel,id:'tree-model',architecture:'price-ai-hgbr-v1'}];
    selectTrainerFamily('price-ai-hgbr-v1');`);
  assert.equal(page.run('trainerJobId'),'tree-done');
  assert.match(page.element('trainerJobSelect').innerHTML,/tree-done/);
  assert.doesNotMatch(page.element('trainerJobSelect').innerHTML,/cnn-active/);
  assert.match(page.element('trainerModels').innerHTML,/tree-mod/);
  assert.doesNotMatch(page.element('trainerModels').innerHTML,/Use in AI Bot/);
  assert.equal(page.element('trainerRunBtn').disabled,true);
  assert.equal(page.element('trainerCancelBtn').disabled,false);
  assert.match(page.element('trainerActiveNote').textContent,/AlexNet CNN.*running/);
  page.run(`calls=[];apiFetch=async (url,options={}) => {calls.push({url,options});return {ok:true,json:async () => ({id:'cnn-active',status:'cancelled',config:{architecture:'alexnet1d-v1'},history:[]})}}`);
  await page.run('cancelModelTraining()');
  assert.equal(page.run('calls[0].url'),'/api/model-trainer/run/cnn-active/cancel');
  assert.equal(page.run('trainerArchitecture'),'price-ai-hgbr-v1');
  assert.equal(page.run('trainerJobId'),'tree-done');
  assert.equal(page.element('trainerCancelBtn').disabled,true);
});

test('Transformer preview draws 100 observed candles, ten forecast candles and both volume series', async () => {
  const page = await catalogDashboard();
  page.run("selectTrainerFamily('transformer-ohlcv-v1')");
  function candle(index) { return {time:new Date(Date.UTC(2026,8,1,index)).toISOString(),open:100+index/100,high:102+index/100,low:99+index/100,close:101+index/100,volume:200+index}; }
  page.context.previewFixture = {architecture:'transformer-ohlcv-v1',symbol:'btcusdt',timeframe:'60',last_price:102,observed_candles:Array.from({length:100},(_,i) => candle(i)),forecast_candles:Array.from({length:10},(_,i) => candle(i+100))};
  page.run('apiFetch=async () => ({ok:true,json:async () => previewFixture})');
  await page.run(`previewTrainedModel('${modelId}')`);
  const chart = page.element('trainerForecastChart').innerHTML;
  assert.equal((chart.match(/data-series="observed-candle"/g) || []).length,100);
  assert.equal((chart.match(/data-series="forecast-candle"/g) || []).length,10);
  assert.equal((chart.match(/data-series="observed-volume"/g) || []).length,100);
  assert.equal((chart.match(/data-series="forecast-volume"/g) || []).length,10);
  assert.match(chart,/Price \(USDT\).*Volume/);
  assert.doesNotMatch(chart,/NaN|Infinity/);
  assert.match(page.element('trainerForecastTable').innerHTML,/<td>\+10<\/td>/);
  assert.match(page.element('trainerPrediction').textContent,/10 OHLCV candles.*sequentially/);
});

test('Transformer run charts label Huber loss and display recursive validation error separately', async () => {
  const page = await catalogDashboard();
  page.run("selectTrainerFamily('transformer-ohlcv-v1');renderTrainerJob({id:'transformer-run',status:'running',config:{architecture:'transformer-ohlcv-v1'},history:[{epoch:1,train_loss:.02,validation_mae_bps:14}]})");
  assert.match(page.element('trainerLossChart').innerHTML,/Huber \(standardized OHLCV\)/);
  assert.match(page.element('trainerLossChart').innerHTML,/recursive validation.*14.00 bps/);
  assert.match(page.element('trainerLossChart').innerHTML,/Recursive validation close error \(bps\)/);
  assert.doesNotMatch(page.element('trainerLossChart').innerHTML,/awaiting loss/);
  assert.doesNotMatch(page.element('trainerLossChart').innerHTML,/>MSE \(dimensionless\)</);
});

test('Agentic previews show calibrated probabilities and spot previews show return and downside horizons', async () => {
  const page = await catalogDashboard();
  page.run("selectTrainerFamily('agentic-ai-mtf-hgb-v1');renderTrainerForecast({architecture:'agentic-ai-mtf-hgb-v1',symbol:'btcusdt',timeframe:'60',last_price:100,long_probability:.72,short_probability:.18,decision:{action:'enter_long',reason:'cost-adjusted edge'},evidence:{'60':{long_probability:.71,short_probability:.19},agent:{long_probability:.72,short_probability:.18}}})");
  assert.match(page.element('trainerPrediction').textContent,/Long 72.0%.*Short 18.0%.*enter_long/);
  assert.match(page.element('trainerForecastTable').innerHTML,/Calibrated ensemble/);
  page.run("selectTrainerFamily('spot-hgb-v1');renderTrainerForecast({architecture:'spot-hgb-v1',symbol:'btcusdt',timeframe:'60',last_price:100,forecasts:{'4':{return:.01,downside:.005,predicted_price:101},'24':{return:.02,downside:.01,predicted_price:102},'72':{return:.03,downside:.015,predicted_price:103}}})");
  const table = page.element('trainerForecastTable').innerHTML;
  assert.match(table,/Downside magnitude/);
  assert.match(table,/4 hours.*100.00 bps.*50.00 bps/);
  assert.match(table,/24 hours/);
  assert.match(table,/72 hours/);
});

test('validated non-AlexNet models cannot enter saved model bot selectors or direct bot drafts', () => {
  const page = dashboard();
  page.run(`savedAIModels=[fixtureModel,...['transformer-ohlcv-v1','price-ai-hgbr-v1','agentic-ai-mtf-hgb-v1','spot-hgb-v1','spot-lightgbm-v1'].map((architecture,index) => ({...fixtureModel,id:'other-'+index,architecture}))]`);
  page.element('fStrategy').value = 'AlexNet';
  page.element('fSymbol').value = 'btcusdt';
  page.element('fTimeframe').value = '60';
  assert.equal(page.run("compatibleAIModels('f').length"),1);
  page.run("useTrainedModel('other-0')");
  assert.equal(page.element('createModal').classList.contains('open'),false);
  assert.match(page.run('toasts[0].message'),/Only saved AlexNet/);
});

test('late preview responses cannot replace another family or a more recent forecast', async () => {
  const page = await catalogDashboard();
  page.run('var finishPreview;apiFetch=async () => new Promise(resolve => finishPreview=resolve);var oldPreview=previewTrainedModel(fixtureModel.id)');
  page.run("selectTrainerFamily('price-ai-hgbr-v1')");
  await page.run("finishPreview({ok:true,json:async () => ({symbol:'btcusdt',timeframe:'60',predicted_price:999})});oldPreview");
  assert.equal(page.element('trainerPrediction').textContent,'');
  page.run(`var previewResolvers=[];apiFetch=async () => new Promise(resolve => previewResolvers.push(resolve));var firstPreview=previewTrainedModel('first');var secondPreview=previewTrainedModel('second')`);
  await page.run("previewResolvers[1]({ok:true,json:async () => ({symbol:'btcusdt',timeframe:'60',last_price:100,predicted_price:102,forecast_return_bps:200,horizon:4})});secondPreview");
  await page.run("previewResolvers[0]({ok:true,json:async () => ({symbol:'btcusdt',timeframe:'60',last_price:100,predicted_price:999,forecast_return_bps:9000,horizon:4})});firstPreview");
  assert.match(page.element('trainerPrediction').textContent,/predicted 102 USDT/);
  assert.doesNotMatch(page.element('trainerPrediction').textContent,/999/);
});

test('pending start remains visible and cancellable after switching model families', async () => {
  const page = await catalogDashboard();
  page.run(`var finishStart;apiFetch=async (url,options) => url === '/api/model-trainer/run' ? new Promise(resolve => finishStart=resolve) : {ok:true,json:async () => ({id:'new-cnn',status:'running',config:{architecture:'alexnet1d-v1'},history:[]})};var pendingStart=startModelTraining()`);
  page.run("selectTrainerFamily('price-ai-hgbr-v1')");
  await page.run("finishStart({ok:true,json:async () => ({job_id:'new-cnn'})});pendingStart");
  assert.equal(page.run('trainerArchitecture'),'price-ai-hgbr-v1');
  assert.equal(page.run('trainerJobId'),null);
  assert.equal(page.element('trainerCancelBtn').disabled,false);
  assert.equal(page.element('trainerRunBtn').disabled,true);
  assert.match(page.element('trainerActiveNote').textContent,/AlexNet CNN.*running/);
});

test('refresh during a pending start retains the accepted run and permits cancellation', async () => {
  const page = await catalogDashboard();
  deferTrainingStart(page);
  await page.run('loadModelTrainer()');
  assert.equal(page.run('trainerJobs.length'),0,'refresh finishes before the server accepts the POST');
  assert.equal(page.element('trainerRunBtn').disabled,true);
  await page.run("finishStart({ok:true,json:async () => ({job_id:'accepted-run'})});pendingStart");
  assert.equal(page.run('trainerActiveJob().id'),'accepted-run');
  assert.equal(page.element('trainerRunBtn').disabled,true);
  assert.equal(page.element('trainerCancelBtn').disabled,false);
  assert.equal(page.timers.size,1);
  await page.run('cancelModelTraining()');
  assert.equal(page.run("calls.some(call => call.url === '/api/model-trainer/run/accepted-run/cancel')"),true);
  assert.equal(page.element('trainerCancelBtn').disabled,true);
});

test('leaving and returning while a start is pending retains the accepted run and polling', async () => {
  const page = await catalogDashboard();
  deferTrainingStart(page);
  page.run(`var originalTrainerLoad=loadModelTrainer;var returningLoad;loadModelTrainer=(...args) => returningLoad=originalTrainerLoad(...args);switchView('Bots');switchView('ModelTrainer')`);
  await page.run('returningLoad');
  await page.run("finishStart({ok:true,json:async () => ({job_id:'accepted-run'})});pendingStart");
  assert.equal(page.run('trainerActiveJob().id'),'accepted-run');
  assert.equal(page.element('trainerRunBtn').disabled,true);
  assert.equal(page.element('trainerCancelBtn').disabled,false);
  assert.equal(page.timers.size,1);
});

test('an accepted start remains queued and polled if its first status fetch fails', async () => {
  const page = await catalogDashboard();
  deferTrainingStart(page);
  await page.run('loadModelTrainer()');
  page.run(`apiFetch=async () => {throw new Error('Status request interrupted')}`);
  await page.run("finishStart({ok:true,json:async () => ({job_id:'accepted-run'})});pendingStart");
  assert.equal(page.run('trainerActiveJob().id'),'accepted-run');
  assert.equal(page.run('trainerActiveJob().status'),'pending');
  assert.equal(page.element('trainerRunBtn').disabled,true);
  assert.equal(page.element('trainerCancelBtn').disabled,false);
  assert.equal(page.timers.size,1);
  assert.match(page.element('trainerFeedback').textContent,/Status request interrupted/);
});

test('a late start from a signed-out session cannot enter the new session or clear its pending request', async () => {
  const page = await catalogDashboard();
  deferTrainingStart(page);
  page.run("var finishOldStart=finishStart;var oldStart=pendingStart;showLogin();authenticated=true;csrfToken='new-account-session'");
  await page.run('loadModelTrainer()');
  deferTrainingStart(page);
  await page.run("finishOldStart({ok:true,json:async () => ({job_id:'old-account-run'})});oldStart");
  assert.equal(page.run('trainerJobs.length'),0);
  assert.equal(page.run('trainerRunPending'),true);
  assert.equal(page.element('trainerRunBtn').disabled,true);
  assert.equal(page.timers.size,0);
  await page.run("finishStart({ok:true,json:async () => ({job_id:'accepted-run'})});pendingStart");
  assert.equal(page.run('trainerActiveJob().id'),'accepted-run');
  assert.equal(page.run('trainerRunPending'),false);
});

test('explicitly incompatible AlexNet records stay out of bot selectors, drafts and symbol choices', () => {
  const page = dashboard();
  page.run(`savedAIModels=[fixtureModel,{...fixtureModel,id:'incompatible-cnn',symbol:'zecusdt',bot_compatible:false},{...fixtureModel,id:'tree',symbol:'dogeusdt',architecture:'price-ai-hgbr-v1'}]`);
  page.element('fStrategy').value = 'AlexNet';
  page.element('fTimeframe').value = '60';
  page.run("populateSymbolSelect('f','btcusdt')");
  assert.equal(page.run("compatibleAIModels('f').length"),1);
  assert.equal(page.element('fSymbol').options.some(option => ['zecusdt','dogeusdt'].includes(option.value)),false);
  page.run("useTrainedModel('incompatible-cnn')");
  assert.equal(page.element('createModal').classList.contains('open'),false);
  assert.match(page.run('toasts[0].message'),/not compatible/);
});
