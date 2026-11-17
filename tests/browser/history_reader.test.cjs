const {test} = require('node:test');
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assets = process.env.CONSOLE_V2_ROOT || path.resolve(__dirname, '../../evolvmem/web_static/v2');
function client(fetch) {
  const ctx=vm.createContext({console,fetch,setTimeout,clearTimeout,location:{protocol:'http:',hash:''}});
  for(const [,file] of fs.readFileSync(path.join(assets,'index.html'),'utf8').matchAll(/<script src="([^"]+)"/g))
    if(!file.endsWith('sso-session.js'))vm.runInContext(fs.readFileSync(path.join(assets,file),'utf8'),ctx);
  const E=ctx.EvolvMemV2;
  Object.assign(E.state,{apiMode:'live',apiError:null,simMode:'normal',ownerId:'owner',libraryCtx:'owner',histScope:'projects',histSubtab:'browse',histQuery:'',selectedProjectKey:'folder',selectedBlockId:'block',historyProjects:[{key:'folder',title:'Synthetic folder',kind:'workspace',blocks:11}],historyBlocks:[{id:'block',project:'folder',title:'&#x20;Fixture',messageCount:116,transcript:['用户：one','用户：two','助手：three','助手：four']}]});
  E.toast=()=>{}; return E;
}
test('consecutive real message roles determine bubble styles independently of ordinal',()=>{
  const html=client().views.history();
  const roles=[...html.matchAll(/class="chat-bubble ([^"]*)"[\s\S]*?class="chat-role">([^<]+)/g)].map(m=>[m[2],m[1].includes('assistant')]);
  assert.deepEqual(roles,[['用户',false],['用户',false],['助手',true],['助手',true]]);
});
test('injection folding preserves original text, ordinary prose and escaped code',()=>{
  const E=client();
  const html=E.renderHistoryContent('Before\n\n<skill>\nsecret instructions\n</skill>\n\nAfter\n```js\n<img src=x onerror=alert(1)>\n```');
  assert.match(html,/<details class="history-injection">/); assert.doesNotMatch(html,/<details[^>]*\sopen/);
  assert.match(html,/&lt;skill&gt;\nsecret instructions\n&lt;\/skill&gt;/);
  assert.match(html,/Before/);assert.match(html,/After/);assert.match(html,/<pre[^>]*><code/);
  assert.doesNotMatch(html,/<img|onerror="/);
  const code=E.renderHistoryContent('```xml\n<skill>literal example</skill>\n```');
  assert.doesNotMatch(code,/<details/);assert.match(code,/literal example/);
});
test('known external tags and file metadata fold without discarding trailing user text',()=>{
  const E=client();
  const html=E.renderHistoryContent('<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>\n\n# Files mentioned by the user\n- file.txt\n\nPlease review it.');
  assert.equal((html.match(/<details/g)||[]).length,2);assert.match(html,/Please review it\./);
  assert.equal(E.historyDisplayTitle('&#x20;Hello &amp; world'),'Hello & world');
});
test('folder stays visible during search and unassigned count uses server metadata',()=>{
  const E=client();E.state.histQuery='word';E.state.liveHistory={unassigned_count:161};
  const html=E.views.history();assert.match(html,/id="hist-project-picker"/);assert.match(html,/未归属片段<\/span><strong>161/);assert.doesNotMatch(html,/&amp;#x20;Fixture/);
});
test('116 messages load in contiguous pages without duplicates and retry preserves already read content',async()=>{
  let fail=false;const urls=[];
  const E=client(async url=>{
    urls.push(url);const p=new URL(url,'http://fixture').searchParams, offset=Number(p.get('offset')||0),limit=Number(p.get('limit'));
    if(fail)return {status:503,json:async()=>({ok:false,error:'temporary'})};
    const end=Math.min(offset+limit,116);return {status:200,json:async()=>({ok:true,messages:Array.from({length:end-offset},(_,i)=>({ordinal:offset+i,role:'user',content:'Message '+(offset+i)})),has_more:end<116,next_offset:end})};
  });
  await E.api.loadBlockTurns('block');assert.equal(E.state.historyBlocks[0].transcript.length,40);
  assert.match(E.views.history(),/已显示 40/);
  fail=true;await E.api.loadBlockTurns('block',true);assert.equal(E.state.historyBlocks[0].transcript.length,40);assert.equal(E.state.apiError,null);
  const callsAfterFailure=urls.length;await E.api.loadBlockTurns('block');assert.equal(urls.length,callsAfterFailure,'returning to a partially loaded conversation must preserve its failed-page retry state');
  fail=false;await E.api.loadBlockTurns('block',true);await E.api.loadBlockTurns('block',true);
  assert.equal(E.state.historyBlocks[0].transcript.length,116);assert.equal(new Set(E.state.historyBlocks[0].transcript).size,116);
  assert.equal(E.state.historyBlocks[0]._hasMore,false);assert.match(urls.at(-1),/offset=80/);
});
test('current folder scopes search, all-folders mode removes only that filter',async()=>{
  const searches=[];const E=client(async url=>{const p=new URL(url,'http://fixture').searchParams;let data={ok:true};
    if(p.get('view')==='projects')data.projects=[{project:'folder',display_name:'Synthetic folder',block_count:11}];
    if(p.get('view')==='search'){searches.push(p);data.items=[];}return {status:200,json:async()=>data};});
  E.state.histQuery='word';await E.api.loadHistory();assert.equal(searches[0].get('project'),'folder');
  E.state.histSearchRange='all';await E.api.loadHistory();assert.equal(searches[1].has('project'),false);
});
test('HTTP 200 database fallback remains retryable without declaring the conversation complete',async()=>{
  let unavailable=false;const offsets=[];
  const E=client(async url=>{const offset=Number(new URL(url,'http://fixture').searchParams.get('offset'));offsets.push(offset);
    return {status:200,json:async()=>unavailable?{ok:true,pg_connected:false,messages:[],detail:'history_pg_unconfigured'}:
      {ok:true,pg_connected:true,messages:Array.from({length:40},(_,i)=>({role:'user',content:'Message '+(offset+i)})),has_more:true,next_offset:offset+40}};
  });
  await E.api.loadBlockTurns('block');unavailable=true;await E.api.loadBlockTurns('block',true);
  const block=E.state.historyBlocks[0];assert.equal(block.messages.length,40);assert.equal(block._hasMore,true);assert.ok(block._turnsError);assert.equal(block._nextOffset,40);
  unavailable=false;await E.api.loadBlockTurns('block',true);assert.equal(block.messages.length,80);assert.deepEqual(offsets,[0,40,40]);assert.equal(block._turnsError,null);
});
test('stale search response cannot overwrite a later query',async()=>{
  let release;const E=client(async url=>{const p=new URL(url,'http://fixture').searchParams;let data={ok:true};
    if(p.get('view')==='projects')data.projects=[{project:'folder',display_name:'Synthetic folder',block_count:11}];
    if(p.get('view')==='search'){data.items=[{block_id:p.get('q'),title:p.get('q'),projects:['folder']}];if(p.get('q')==='old')await new Promise(r=>{release=r;});}
    if(p.get('view')==='turns')data.messages=[];return {status:200,json:async()=>data};});
  E.state.histQuery='old';const old=E.api.loadHistory();while(!release)await new Promise(r=>setTimeout(r,1));
  E.state.histQuery='new';await E.api.loadHistory();release();await old;assert.equal(E.state.historyBlocks[0].id,'new');
});
test('folder deletion opens scoped confirmation and cancel or changed library cannot submit',()=>{
  const E=client();let html='',posts=0;E.openModal=(title,body)=>{html=title+body;};E.closeModal=()=>{};E.api.manageHistoryProject=()=>{posts++;return Promise.resolve(true);};
  E.applyAction('toggle-proj-del',{key:'folder'});assert.equal(posts,0);assert.match(html,/Synthetic folder/);assert.match(html,/11/);assert.match(html,/文件夹/);
  E.state.libraryCtx='peer';E.applyAction('confirm-history-delete',{});assert.equal(posts,0);
});
test('confirmed deletion submits the reviewed folder once, including repeated clicks',()=>{
  const E=client();const calls=[];E.openModal=()=>{};E.closeModal=()=>{};E.api.manageHistoryProject=(...args)=>calls.push(args);
  E.applyAction('toggle-proj-del',{key:'folder'});E.applyAction('confirm-history-delete',{});E.applyAction('confirm-history-delete',{});
  assert.deepEqual(calls,[['delete','folder','Synthetic folder']]);
});
