// Exercise the real router with a controller contract; no browser, network or data writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const appPath = process.env.EVOLVMEM_APP_JS || path.resolve(__dirname, '../../evolvmem/web_static/designs/app.js');
const source = fs.readFileSync(appPath, 'utf8');
const navigate = source.slice(source.indexOf('  function navigate('), source.indexOf('  function countUp('));
const fromLocation = source.slice(source.indexOf('  function navigateFromLocation('), source.indexOf("  window.addEventListener('hashchange'"));
assert.ok(navigate.includes('function navigate(') && fromLocation.includes('function navigateFromLocation('));

function setup(hash) {
  const loaded = [], writes = [];
  const values = {view:'projects',mode:'groups',tab:'formal',project:'__all__',query:'',lane:'overview',sort:'recent'};
  const controller = {
    kind:'memories', integrated:true, canLeave:()=>true,
    set project(value){values.project=value; values.view='library';},
    set query(value){values.query=value;},
    set view(value){values.view=value;},
    set intakeMode(value){values.mode=value==='records'?'records':'groups';},
    set capabilityTab(value){values.tab=['formal','intake','suggestions'].includes(value)?value:'formal';},
    set lane(value){values.lane=value;},
    set projectSort(value){values.sort=value;},
    hash(){
      const params = new URLSearchParams();
      if(values.project!=='__all__')params.set('project',values.project);
      if(values.view==='intake'&&values.mode==='records')params.set('mode','records');
      if(values.view==='capabilities'&&values.tab!=='formal')params.set('tab',values.tab);
      return '#knowledge/'+values.view+(params.size?'?'+params:'');
    },
    draw(){}, load(){loaded.push({...values});},
  };
  const mount = {};
  const context = {URLSearchParams,location:{hash},window:{EvolvKnowledge:{},scrollTo(){}},document:{body:{dataset:{}}},
    state:{ready:true,page:'home'},browsers:new Map([[mount,controller]]),$:()=>mount,$$:()=>[]};
  context.history = Object.fromEntries(['replaceState','pushState'].map(method=>[method,(_state,_title,url)=>{writes.push({method,url});context.location.hash=url;}]));
  vm.createContext(context);
  vm.runInContext(navigate+'\n'+fromLocation,context);
  return {context,controller,values,loaded,writes,route:()=>vm.runInContext('navigateFromLocation()',context),go:view=>vm.runInContext(`navigate('memories','__all__',undefined,${JSON.stringify(view)})`,context)};
}

let checks=0;
for(const route of ['knowledge/intake','intake']) {
  const f=setup('#'+route+'?mode=records');f.route();
  assert.equal(f.loaded.at(-1).mode,'records','record deep link must set controller before load');
  assert.equal(f.context.location.hash,'#knowledge/intake?mode=records','record deep link must survive URL normalization');checks++;
}
for(const tab of ['intake','suggestions']) {
  const f=setup('#knowledge/capabilities?tab='+tab);f.route();
  assert.equal(f.loaded.at(-1).tab,tab,'capability tab must be set before load');
  assert.equal(f.context.location.hash,'#knowledge/capabilities?tab='+tab);checks++;
}
for(const [view,param,field,previous,expected] of [['intake','mode','mode','records','groups'],['capabilities','tab','tab','intake','formal']]) {
  const f=setup('#knowledge/'+view+'?'+param+'='+previous);f.route();
  f.context.location.hash='#knowledge/'+view;f.route();
  assert.equal(f.loaded.at(-1)[field],expected,'back to a parameterless route must reset retained controller state');
  f.context.location.hash='#knowledge/'+view+'?'+param+'='+previous;f.route();
  f.go(view);
  assert.equal(f.loaded.at(-1)[field],expected,'main navigation must open the default view');
  assert.equal(f.context.location.hash,'#knowledge/'+view);checks++;
}
{
  const f=setup('#knowledge/intake?mode=invalid&tab=invalid&lane=sources&sort=name');f.route();
  assert.equal(f.values.mode,'groups');assert.equal(f.values.tab,'formal');
  assert.equal(f.values.lane,'sources');assert.equal(f.values.sort,'name');checks++;
}
{
  const f=setup('#knowledge/intake?mode=records');f.route();
  f.controller.canLeave=()=>false;
  f.context.location.hash='#knowledge/capabilities?tab=intake';f.route();
  assert.equal(f.context.location.hash,'#knowledge/intake?mode=records');
  assert.equal(f.loaded.length,1,'leave guard must prevent load/state changes');checks++;
}
{
  const f=setup('#knowledge/intake?mode=records');f.route();
  f.controller.load=()=>{throw Error('fixture load error');};
  assert.throws(()=>f.go('capabilities'),/fixture load error/);
  assert.equal(f.context.location.hash,'#knowledge/intake?mode=records','synchronous load failure must not publish a successful navigation URL');checks++;
}
console.log(JSON.stringify({passed:true,checks,fixture:'actual app router, no network'}));