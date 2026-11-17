// Execute the actual homepage loader/click handler with isolated DOM/API fixtures.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(process.env.EVOLVMEM_APP_JS||path.resolve(__dirname,'../../evolvmem/web_static/designs/app.js'),'utf8');
const boot=source.slice(source.indexOf('  async function boot('),source.indexOf('  // Browser controllers'));
const click=source.slice(source.lastIndexOf("  document.addEventListener('click',event=>{"),source.indexOf("  document.addEventListener('evolvmem:project'"));
const directoryUrl='/api/project-archives?status=active&page_size=5&sort=recent';
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function render(directory,{galaxy=false,fail=false}={}) {
  const list={innerHTML:''},stat={dataset:{stat:'project_count'},textContent:''};
  const galaxyNode={append(){}};
  const selectors=new Map([['[data-slot=project-list]',[list]],['[data-stat]',[stat]],['[data-stat=project_count]',[stat]],['[data-galaxy]',galaxy?[galaxyNode]:[]]]);
  const requests=[],galaxyCalls=[];
  const context={state:{ready:true,page:'home',boot:0},memoryCache:new Map(),browsers:new Map(),
    $$:selector=>selectors.get(selector)||[],$:selector=>selector.split(',').flatMap(s=>selectors.get(s)||[])[0]||null,
    get:async url=>{requests.push(url);if(url===directoryUrl){if(fail)throw Error('fixture unavailable');return directory;}if(url==='/api/projects')return {projects:[{project:'legacy-only',active_items:999,status:'active'}]};return {rows:[],projects:[]};},
    refreshOrganizationMetrics:async()=>{},countUp:(el,value)=>{el.textContent=value??'—';},esc,errorBox:()=>'<p class="ui-error">fixture unavailable</p>',
    compactHome:()=>false,window:{EvolvGalaxy:{mount:(el,data)=>galaxyCalls.push(data)}},document:{createElement:()=>({})},console};
  vm.createContext(context);await vm.runInContext(boot+'\nboot()',context);
  return {list,stat,requests,galaxyCalls,projects:context.state.projects};
}
(async()=>{
  let checks=0;
  const data={items:[{project:'recent-project',display_name:'Recent project',has_archive:true},{project:'registered-only',display_name:'No history yet',has_archive:false}],stats:{active:95}};
  const actual=await render(data);
  assert.ok(actual.requests.includes(directoryUrl),'homepage must read the archive directory');
  assert.ok(actual.requests.includes('/api/projects'),'keep the full project source for experience/progress filter options');
  assert.equal(actual.projects[0].project,'legacy-only','switching the home list must preserve project filter options');
  assert.match(actual.list.innerHTML,/data-archive-project="recent-project"/);
  assert.match(actual.list.innerHTML,/data-archive-project="registered-only"/);
  assert.match(actual.list.innerHTML,/尚无项目摘要 →/);
  assert.ok(!actual.list.innerHTML.includes('暂无历史记录'));
  assert.ok(!actual.list.innerHTML.includes('legacy-only'));
  assert.equal(actual.stat.textContent,95);checks++;
  const withGalaxy=await render(data,{galaxy:true});
  assert.ok(withGalaxy.requests.includes('/api/projects'));
  assert.equal(withGalaxy.galaxyCalls[0].projects[0].project,'legacy-only');checks++;
  const empty=await render({items:[],stats:{active:0}});
  assert.match(empty.list.innerHTML,/ui-empty/);assert.equal(empty.stat.textContent,0);checks++;
  const failure=await render(null,{fail:true});
  assert.match(failure.list.innerHTML,/ui-error/);assert.ok(!failure.list.innerHTML.includes('ui-empty'));assert.equal(failure.stat.textContent,'—');checks++;
  const calls=[];let handler;
  const context={document:{addEventListener:(name,fn)=>{handler=fn;}},navigate:(...args)=>calls.push(args)};
  vm.createContext(context);vm.runInContext(click,context);
  handler({target:{closest:selector=>selector==='[data-archive-project]'?{dataset:{archiveProject:'A/B & C'}}:null},preventDefault(){}});
  assert.deepEqual(calls,[['memories','A/B & C',undefined,'archives']]);checks++;
  console.log(JSON.stringify({passed:true,checks,fixture:'actual homepage loader/click handler, no network'}));
})().catch(error=>{console.error(error);process.exitCode=1;});