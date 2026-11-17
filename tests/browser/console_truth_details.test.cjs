const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const jsRoot = process.env.EVOLVMEM_V2_JS_DIR || (fs.existsSync(path.resolve(__dirname, '../../evolvmem/web_static/v2/js')) ? path.resolve(__dirname, '../../evolvmem/web_static/v2/js') : path.resolve(__dirname, '../candidate/js'));
function setup(protocol = 'http:') {
  const escape = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let drawer;
  const E = {
    state: {apiMode:'live',libraryCtx:'owner',ownerId:'owner',r9Tripped:false,sessionRemainingSec:26840,
      stageFilter:'all',simMode:'normal',histSubtab:'capture',histDb:'owner',histQuery:'',histScope:'projects',
      historyProjects:[],historyBlocks:[],experiences:[],proposals:[],interceptions:[],agents:[],topoNodes:[],
      changesStream:[],llmCalls:[],pgDatabases:[],retiredSqlite:[],policyLocks:[],impressions:[],handbookBlocks:[],skills:[],intFilters:{layer:'all',track:'all',category:'all',q:''}},
    esc:escape,canWrite:()=>true,isProduction:()=>protocol === 'http:' || protocol === 'https:',
    plannedButton:label=>'<button disabled>'+escape(label)+' · 待开发</button>',
    unavailablePanel:(title,detail)=>'<section class="panel"><h2>'+escape(title)+'</h2><p>'+escape(detail)+'</p></section>',
    renderExpSubtabs:()=>'<nav>经验库与提炼中心</nav>',statusBadge:()=>'',HIST_DB_ZH:{owner:'本人历史库'},LAYER_META:[],
    openDrawer:(title,body,sub)=>{drawer={title,body,sub};},matchPostToolUse:()=>({sig:'fake',note:null})
  };
  const context = vm.createContext({EvolvMemV2:E,location:{protocol},console});
  for (const file of ['history-time.js','history-content.js','render-pipeline-and-history.js','l3-drawers.js','render-interceptions.js']) vm.runInContext(fs.readFileSync(path.join(jsRoot,file),'utf8'),context,{filename:file});
  return {E,context,drawer:()=>drawer};
}

test('HTTP pipeline never presents fixture budgets, cleaning rates or an unverified running state', () => {
  const {E}=setup();
  const html=E.views.pipelines();
  assert.doesNotMatch(html,/2\.5%|2\.6%|70%–80%|707 条|4\.0%|29 天|调度运行中|100 会话|模拟触发/);
  assert.match(html,/尚未接入|未核验/);
  assert.doesNotMatch(html,/<select[^>]+id="bf-scope/);
});

test('HTTP pipeline preserves actual circuits, segment status and cleanup registry counts', () => {
  const {E}=setup();
  E.state.livePipeline={circuits:[{track:'live',paused:true,pause_reason:'verified-cost-limit',updated_at:'2026-11-17'}],
    segments:[{track:'live',session_id:'actual-session',status:'deferred',moments_count:2,error_code:'circuit_paused',updated_at:'2026-11-17'}],
    llm_usage:[{track:'live',stage:'L2a',model:'actual-model',calls:3,input_tokens:111,output_tokens:22,cost_estimate:0.01}],
    backfill_cursors:[],backfill_job_stats:[],legacy_cleanups:[{cleanup_id:'actual-cleanup',deleted_total:18,migrated_keep_total:7,approved_at:'2026-11-16'}]};
  const html=E.views.pipelines();
  for (const value of ['verified-cost-limit','actual-session','deferred','actual-cleanup','actual-model']) assert.ok(html.includes(value),value);
  assert.match(html,/data-act="toggle-r9"/);
  assert.match(html,/待开发/);
});

test('HTTP capture shows missing verification instead of fixed desktop success receipts', () => {
  const {E}=setup();const html=E.views.history();
  assert.doesNotMatch(html,/零积压健康|最新 2 个|74 份|4 毫秒|上次调度成功/);
  assert.match(html,/尚未接入|未核验/);
  assert.match(html,/data-act="hist-subtab"/);
});

test('HTTP history does not turn placeholder search scores or SVG into actual evidence', () => {
  const {E}=setup();E.state.histSubtab='browse';E.state.histScope='unassigned';
  E.state.historyBlocks=[{id:'real-block',title:'真实会话',project:null,turns:2,transcript:['用户：真实消息'],rrf:1,bm25:0,trgm:0,vector:0,hasAttachment:true}];
  const html=E.views.history();assert.match(html,/真实消息/);
  assert.doesNotMatch(html,/混搜综合分 100|DR ISOLATION DRILL|roles_restored=true|已解密内嵌附件截图/);
});

test('HTTP diagnostics cannot reset production to sandbox or claim hardcoded runtime health', () => {
  const f=setup();f.E.openDiagDrawer();const html=f.drawer().body;
  assert.doesNotMatch(html,/data-act="(?:set-sim|reset-demo)"|\[ONLINE\]|7\/7 @ 0400|live-v2 \(PG|7h 27m/);
  assert.match(html,/尚未接入|未核验/);
});

test('HTTP L3 unsupported surfaces do not show fixture receipts, fake Beta or generated Skill source', () => {
  const f=setup();
  f.E.state.topoNodes=[{id:'topo',title:'真实导航节点',stage:'Stage 6',metrics:'5端全在线',detail:'模块职责',target:'evolution'}];
  f.E.state.impressions=[{id:'fake',signal:'fake-positive',deltaBeta:'+1 α'}];
  f.E.state.skills=[{id:'skill',name:'catalog-skill',summary:'真实目录描述',mdPreview:'# fabricated-skill-source'}];
  for (const act of ['open-topo-l3','open-stage4-l3','open-capture-l3','open-impressions-l3','open-handbook-l3','open-errsig-l3','open-dr-l3','open-policy-l3','open-skill']) {
    f.E.openL3Drawer(act,{id:act==='open-skill'?'skill':'topo'});
    const html=f.drawer().body;
    assert.doesNotMatch(html,/5端全在线|实测遥测|707 合并|4\.0%|2\/2 ack|74\/74|fake-positive|\+1 α|fabricated-skill-source/,act);
  }
});

test('HTTP exposure drawer keeps real retrieval counts separate from unknown outcomes', () => {
  const f=setup();f.E.state.liveEvolution={impressions:[{item_uid:'actual-note',channel:'mcp',agent_client:'codex',model:'model',hits:8}]};
  f.E.openL3Drawer('open-impressions-l3');
  assert.match(f.drawer().body,/actual-note/);assert.match(f.drawer().body,/mcp/);assert.match(f.drawer().body,/>8</);
  assert.match(f.drawer().body,/使用结果|效果归因/);
});

test('file previews are explicitly identified as demonstrations', () => {
  const f=setup('file:');assert.match(f.E.views.pipelines(),/演示/);assert.match(f.E.views.history(),/演示/);
  f.E.openDiagDrawer();assert.match(f.drawer().body,/演示/);
});

test('HTTP failed experience trace explicitly reports failure instead of leaving a loading or fixture result', async () => {
  const f=setup();const body={innerHTML:''};f.context.document={getElementById:id=>id==='drawer-body'?body:null};
  f.E.state.experiences=[{id:'actual-note',category:'业务经验',kind:'pitfall',origin:'manual',model:'model',revision:1,evidenceCount:1,scope:{}}];
  f.E.api={fetchExpTrace:()=>Promise.resolve(null)};
  f.E.openL3Drawer('open-exp-trace',{id:'actual-note'});await Promise.resolve();await Promise.resolve();
  assert.match(body.innerHTML,/读取失败/);assert.match(body.innerHTML,/未核验/);
});

test('HTTP experience trace preserves actual gate evidence without inventing missing gate passes or Beta', () => {
  const f=setup();
  f.E.state.experiences=[{id:'actual-note',category:'业务经验',kind:'pitfall',origin:'manual',model:'model',revision:1,evidenceCount:1,betaA:99,betaB:0,scope:{},gate_chain:{decisions:[{layer:'L2b',verdict:'pass',detail:'actual judge explanation',meta:{model_prediction:'actual blind answer'}}]}}];
  f.E.openL3Drawer('open-exp-trace',{id:'actual-note'});const html=f.drawer().body;
  assert.match(html,/actual judge explanation/);assert.match(html,/actual blind answer/);
  assert.doesNotMatch(html,/\(99, 0\)|>已通过</);assert.match(html,/效果归因尚未接入/);
});

function deferred() { let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject}; }
async function flush() { for(let n=0;n<8;n++) await Promise.resolve(); }
function asyncDrawerFixture() {
  const f=setup();const body={innerHTML:''};f.context.document={getElementById:id=>id==='drawer-body'?body:null};
  const open=f.E.openDrawer;
  f.E.openDrawer=(title,html,sub)=>{f.E.drawerEpoch=(f.E.drawerEpoch||0)+1;open(title,html,sub);body.innerHTML=html;};
  f.E.state.experiences=['A','B'].map(id=>({id,category:'业务经验',kind:'pitfall',origin:'manual',model:'model',revision:1,evidenceCount:1,scope:{}}));
  f.E.state.interceptions=['A','B'].map(id=>({id,layer:'L2b',reason_code:'reason',track:'live',gate_chain:[]}));
  return {...f,body};
}

test('late experience trace success and failure cannot overwrite a newer drawer', async () => {
  for (const outcome of ['success','null','error']) {
    const f=asyncDrawerFixture();const a=deferred(),b=deferred();f.E.api={fetchExpTrace:id=>id==='A'?a.promise:b.promise};
    f.E.openL3Drawer('open-exp-trace',{id:'A'});f.E.openL3Drawer('open-exp-trace',{id:'B'});
    b.resolve({events:[{action:'CURRENT-B'}]});await flush();const current=f.body.innerHTML;
    if(outcome==='error') a.reject(Error('old request'));else a.resolve(outcome==='null'?null:{events:[{action:'STALE-A'}]});
    await flush();assert.equal(f.body.innerHTML,current,outcome);assert.match(current,/CURRENT-B/);
  }
});

test('late interception trace cannot replace a newer diagnostic or its failure state', async () => {
  for (const outcome of ['success','http-failure','network-failure']) {
    const f=asyncDrawerFixture();const a=deferred();f.context.fetch=()=>a.promise;
    f.E.openL3Drawer('open-int-trace',{id:'A'});f.E.openDiagDrawer();const current=f.body.innerHTML;
    if(outcome==='network-failure') a.reject(Error('old request'));
    else a.resolve({ok:outcome==='success',json:async()=>({ok:true,item:{meta_json:{marker:'STALE-A'}}})});
    await flush();assert.equal(f.body.innerHTML,current,outcome);
  }
});

test('late evidence callbacks cannot replace a newer drawer on success, null or failure', async () => {
  for (const outcome of ['success','null','error']) {
    const f=asyncDrawerFixture();const a=deferred();f.E.api={fetchEvidenceContext:()=>a.promise};
    f.E.openL3Drawer('open-evidence',{ref:'archive:A'});f.E.openDiagDrawer();const current=f.body.innerHTML;
    if(outcome==='error') a.reject(Error('old request'));else a.resolve(outcome==='null'?null:{turns:[{role:'user',content:'STALE-A'}]});
    await flush();assert.equal(f.body.innerHTML,current,outcome);
  }
});

test('close, library change and rebootstrap invalidate pending drawer requests', async () => {
  for (const invalidate of ['close','library','epoch','state']) {
    const f=asyncDrawerFixture();const a=deferred();f.E.api={fetchExpTrace:()=>a.promise};
    f.E.openL3Drawer('open-exp-trace',{id:'A'});const current=f.body.innerHTML;
    if(invalidate==='close') f.E.drawerEpoch=(f.E.drawerEpoch||0)+1;
    else if(invalidate==='library') f.E.state.libraryCtx='other';
    else if(invalidate==='epoch') f.E.state.libraryEpoch=(f.E.state.libraryEpoch||0)+1;
    else f.E.state={...f.E.state};
    a.resolve({events:[{action:'STALE-A'}]});await flush();assert.equal(f.body.innerHTML,current,invalidate);
  }
});

test('HTTP missing L1 diagnostics do not fabricate similarity, threshold or duplicate side effects', () => {
  const f=setup();f.E.state.interceptions=[{id:'L1',layer:'L1',problem:'问题',solution:'解法',detail:'说明',gate_chain:[],diagnostic:{},track:'live'}];
  const html=f.E.renderInterceptionsView();assert.doesNotMatch(html,/0\.900|0\.88|width:90%|证据次数已自动累加/);assert.match(html,/未返回/);
});

test('HTTP missing L3 trial counts do not fabricate a zero out of two success baseline', () => {
  const f=setup();f.E.state.interceptions=[{id:'L3',layer:'L3',problem:'问题',solution:'解法',detail:'说明',gate_chain:[],diagnostic:{trial:{}},track:'live'}];
  const html=f.E.renderInterceptionsView();assert.doesNotMatch(html,/试用成功 0 \/ 2|失败 0 次|width:0%/);assert.match(html,/未返回/);
});

test('HTTP missing judge names stay unknown and actual zero diagnostic values remain zero', () => {
  const f=setup();const html=f.E.renderL2BlindComparison({finalLayer:'L2b',chain:[{layer:'L2a',meta:{model_prediction:'actual A'}},{layer:'L2b',meta:{model_prediction:'actual B'}}]});
  assert.doesNotMatch(html,/deepseek-v4-flash|deepseek-chat/);assert.match(html,/未返回/);
  f.E.state.interceptions=[{id:'L1',layer:'L1',problem:'问题',solution:'解法',detail:'说明',gate_chain:[{layer:'L1',meta:{cosine_similarity:0,threshold:0}}],diagnostic:{target_note:{id:'real',problem:'原问题',solution:'原解法'}},track:'live'}];
  assert.match(f.E.renderInterceptionsView(),/0\.000 \/ 阈值 0/);
  f.E.state.interceptions=[{id:'L3',layer:'L3',problem:'问题',solution:'解法',detail:'说明',gate_chain:[],diagnostic:{trial:{success_count:0,required_successes:0,failure_count:0}},track:'live'}];
  assert.match(f.E.renderInterceptionsView(),/试用成功 0 \/ 0 次 · 失败 0 次/);
});
