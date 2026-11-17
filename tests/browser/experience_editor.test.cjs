const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const assets=path.resolve(__dirname,'../../evolvmem/web_static/v2');
const note={id:'manual-edit',revision:7,origin:'manual',status:'active',category:'业务经验',kind:'procedure',
 problem:'原问题 <input>',solution:'原解决办法',reusable:'原可复用经验',scope:{project:'Evolvmem'}};
function harness(){
 const context=vm.createContext({console,setTimeout,clearTimeout,location:{protocol:'http:',hash:''}});
 for(const [,file] of fs.readFileSync(path.join(assets,'index.html'),'utf8').matchAll(/<script src="([^"]+)"/g))
  if(!file.endsWith('sso-session.js'))vm.runInContext(fs.readFileSync(path.join(assets,file),'utf8'),context);
 const E=context.EvolvMemV2,fields={};
 for(const id of ['new-exp-cat','new-exp-prob','new-exp-sol','new-exp-reus','new-exp-status','new-exp-submit'])
  fields[id]={value:'',disabled:false,textContent:'',setAttribute(){},focus(){}};
 context.document={getElementById:id=>fields[id] || null};
 E.state.apiMode='live';E.state.ownerId=E.state.libraryCtx='alice';E.state.expSubtab='list';
 E.state.experiences=[{...note}];E.state.selectedExpId=note.id;E.state.expFilters.q='原问题';E.state.expOffset=60;
 E.canWrite=()=>true;E.renderAll=()=>{};E.openModal=(title,html)=>{E.lastModal={title,html};};
 return {E,fields,context,fill(){fields['new-exp-cat'].value='业务经验';fields['new-exp-prob'].value='修改后的问题';
  fields['new-exp-sol'].value='修改后的办法';fields['new-exp-reus'].value='修改后的规则';}};
}
test('only own active manual notes expose edit and prefill the shared modal',()=>{
 const {E}=harness();assert.match(E.views.experiences(),/data-act="edit-manual-exp"/);
 assert.equal(E.openExperienceEditor(note),true);
 assert.match(E.lastModal.html,/原问题 &lt;input&gt;/);assert.match(E.lastModal.html,/原解决办法/);
 for(const changes of [{origin:'legacy'},{status:'retired'}])assert.equal(E.openExperienceEditor({...note,...changes}),false);
 E.state.libraryCtx='bob';assert.doesNotMatch(E.views.experiences(),/data-act="edit-manual-exp"/);
 assert.equal(E.openExperienceEditor(note),false);
 E.state.libraryCtx='alice';E.canWrite=()=>false;assert.equal(E.openExperienceEditor(note),false);
});
test('editing sends CAS once, retains reading context and shows persistent success',async()=>{
 const h=harness(),calls=[];h.E.openExperienceEditor(note);h.fill();let finish;
 h.E.api.writeExperience=payload=>{calls.push(payload);return new Promise(r=>finish=r);};
 const first=h.E.submitExperienceEditor(),again=h.E.submitExperienceEditor();
 assert.equal(calls.length,1);assert.equal(calls[0].action,'update');assert.equal(calls[0].id,note.id);
 assert.equal(calls[0].expected_revision,7);assert.equal(h.fields['new-exp-submit'].disabled,true);
 assert.match(h.fields['new-exp-status'].textContent,/保存/);
 finish({ok:true,note:{...note,revision:8}});await first;await again;
 assert.match(h.fields['new-exp-status'].textContent,/保存成功/);assert.equal(h.fields['new-exp-submit'].disabled,true);
 assert.equal(h.E.state.expFilters.q,'原问题');assert.equal(h.E.state.expOffset,60);assert.equal(h.E.state.selectedExpId,note.id);
});
test('empty values never create placeholder notes; transient retry keys bind exact payload',async()=>{
 const h=harness(),calls=[];h.E.openExperienceEditor();h.E.api.writeExperience=async payload=>{
  calls.push(payload);return {ok:false,status:503,error:'experience_embedding_unavailable'};};
 await h.E.submitExperienceEditor();assert.equal(calls.length,0);assert.match(h.fields['new-exp-status'].textContent,/问题/);
 h.fill();await h.E.submitExperienceEditor();await h.E.submitExperienceEditor();
 assert.equal(calls[0].request_id,calls[1].request_id);
 h.fields['new-exp-sol'].value='另一解决办法';await h.E.submitExperienceEditor();
 assert.notEqual(calls[1].request_id,calls[2].request_id);
 assert.equal(h.fields['new-exp-sol'].value,'另一解决办法');
 assert.match(h.fields['new-exp-status'].textContent,/向量|编码/);
});
test('CAS conflict keeps inputs and original revision; retry cannot silently overwrite',async()=>{
 const h=harness(),calls=[];h.E.openExperienceEditor(note);h.fill();
 h.E.api.writeExperience=async payload=>{calls.push(payload);return {ok:false,status:409,error:'experience_revision_conflict'};};
 await h.E.submitExperienceEditor();await h.E.submitExperienceEditor();
 assert.equal(calls.length,1);assert.equal(calls[0].expected_revision,7);
 assert.equal(h.fields['new-exp-prob'].value,'修改后的问题');
 assert.match(h.fields['new-exp-status'].textContent,/输入已保留/);
 assert.match(h.fields['new-exp-status'].textContent,/重新读取/);
});

test('long existing text is fully prefilled rather than constrained by the old create limits',()=>{
 const {E}=harness();const long={...note,problem:'长'.repeat(350),solution:'解'.repeat(750),reusable:'规'.repeat(300)};
 E.openExperienceEditor(long);
 assert.ok(E.lastModal.html.includes(long.problem));assert.ok(E.lastModal.html.includes(long.solution));
 assert.ok(E.lastModal.html.includes(long.reusable));assert.match(E.lastModal.html,/maxlength="8000"/);
});

test('a confirmed write remains success when subsequent refresh fails, with a refresh-only retry',async()=>{
 const h=harness(),writes=[];h.E.openExperienceEditor(note);h.fill();
 h.E.api.writeExperience=async payload=>{writes.push(payload);return {ok:true,note:{...note,revision:8}};};
 let refreshes=0;h.E.api.refreshExperienceViews=async()=>{refreshes++;return refreshes>1;};
 await h.E.submitExperienceEditor();
 assert.match(h.fields['new-exp-status'].textContent,/保存成功/);
 assert.match(h.fields['new-exp-status'].textContent,/刷新失败/);
 assert.equal(h.fields['new-exp-submit'].disabled,true);
 await h.E.refreshExperienceEditor();
 assert.equal(writes.length,1);assert.equal(refreshes,2);
 assert.match(h.fields['new-exp-status'].textContent,/保存成功/);
});
test('a saved note outside the unchanged query is not injected into filtered results',async()=>{
 const h=harness();h.E.openExperienceEditor(note);h.fill();
 h.E.api.writeExperience=async()=>({ok:true,note:{...note,revision:8}});
 h.E.api.refreshExperienceViews=async()=>{h.E.state.experiences=[];return true;};
 await h.E.submitExperienceEditor();
 assert.equal(h.E.state.experiences.length,0);assert.equal(h.E.state.expFilters.q,'原问题');
 assert.equal(h.E.state.expOffset,60);
 assert.match(h.fields['new-exp-status'].textContent,/保存成功/);
 assert.match(h.fields['new-exp-status'].textContent,/筛选/);
});

test('lost write receipt is unknown and retries the exact payload and idempotency key',async()=>{
 const h=harness(),writes=[];h.E.openExperienceEditor(note);h.fill();
 h.E.api.writeExperience=async payload=>{writes.push({...payload});return writes.length===1?
  {ok:false,status:0,error:'network_unavailable'}:{ok:true,note:{...note,revision:8}};};
 h.E.api.refreshExperienceViews=async()=>true;
 await h.E.submitExperienceEditor();
 assert.match(h.fields['new-exp-status'].textContent,/保存结果暂未确认/);
 assert.equal(h.fields['new-exp-submit'].textContent,'核对并重试');
 assert.equal(h.fields['new-exp-sol'].disabled,true);
 h.fields['new-exp-sol'].value='禁止未知状态另换正文';
 await h.E.submitExperienceEditor();
 assert.equal(writes[0].request_id,writes[1].request_id);
 assert.equal(writes[1].solution,'修改后的办法');
 assert.match(h.fields['new-exp-status'].textContent,/保存成功/);
});
test('same-identity draft restore rehydrates a pending request and rejects another identity',async()=>{
 const h=harness();let scope='scope-alice';
 h.E.session={scope,isCurrent:t=>t.state===h.E.state,capture:()=>({state:h.E.state})};
 h.E.openExperienceEditor(note);h.fill();let finish;const writes=[];
 h.E.api.writeExperience=payload=>{writes.push({...payload});return new Promise(r=>finish=r);};
 h.E.submitExperienceEditor();
 const draft=h.E.captureExperienceEditorDraft();
 assert.equal(draft.unknown,true);
 h.E.discardExperienceEditor();
 h.E.state={...h.E.state,experiences:[note]};
 assert.equal(h.E.restoreExperienceEditorDraft(draft),true);
 assert.equal(h.fields['new-exp-submit'].disabled,false);
 assert.equal(h.fields['new-exp-submit'].textContent,'核对并重试');
 h.E.api.writeExperience=async payload=>{writes.push({...payload});return {ok:true,note:{...note,revision:8}};};
 h.E.api.refreshExperienceViews=async()=>true;
 await h.E.submitExperienceEditor();
 assert.equal(writes[0].request_id,writes[1].request_id);
 finish({ok:true,note:{...note,revision:8}});
 h.E.session.scope='scope-bob';
 assert.equal(h.E.restoreExperienceEditorDraft(draft),false);
});

test('closing and reopening an unconfirmed create resumes the original request instead of creating a new one',async()=>{
 const h=harness(),writes=[];h.E.openExperienceEditor();h.fill();
 h.E.api.writeExperience=async payload=>{writes.push({...payload});return writes.length===1?
  {ok:false,status:0,error:'network_unavailable'}:{ok:true,note:{...note,id:'created-once',revision:1}};};
 h.E.api.refreshExperienceViews=async()=>true;
 await h.E.submitExperienceEditor();
 h.E.openExperienceEditor();h.fill();
 await h.E.submitExperienceEditor();
 assert.equal(writes[0].action,'create');assert.deepEqual(writes[1],writes[0]);
});
