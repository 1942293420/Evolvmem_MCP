/* People archive material; AI previews only supply optional suggestions. */
(() => {
  window.EvolvReviewGroups = {async render({mount, api, esc, projects, onSaved, onDirty, onBusy, historyMount=mount, onReady=()=>{}}) {
    let page=1, proposal=null, busy=false, selectedGroup='', detail=null, pendingTasks=[], recoveryTimer=null, recoveryGroup='', listSnapshot='', loadSequence=0;
    const prepared=new Set(), modelReports=new Map();let elapsedTimer=null,previewStarted=0;
    const drafts=new Map(), analyses=new Map(), manualPreviews=new Map(), decisionDrafts=new Map(), detailCache=new Map();let analysisTimer=null,chooseSequence=0,savedBatch=null;
    let detailRequest=null,requestedGroup='';
    const $=s=>mount.querySelector(s);
    const projectName=p=>projects.find(x=>x.project===p)?.display_name||p;
    const messages={review_preview_changed:'资料或规则已变化，请刷新资料并重新预览。',review_condition_required:'请说明按什么业务依据判断这些资料的适用范围。',review_ai_invalid_response:'AI 返回的判断不完整，请重试。',review_ai_failed:'AI 判断未完成，资料尚未修改，请重试。',review_retrieval_unavailable:'暂时无法查找相似资料。可重试批量建议，或直接保存当前这条。',review_ai_not_ready:'AI 还在判断，请等待预览完成。',extraction_provider_unavailable:'整理模型暂不可用，资料尚未修改。',review_excluded:'你已确认这条不归入当前范围，请先撤销该决定再归入。',review_preview_required:'请先预览并核对本次归类范围。',invalid_project:'请先选择适用项目。',project_required:'请勾选至少一个项目，或选择“个人通用”。',invalid_projects:'请选择有效的项目。',invalid_review_model:'请选择列表中的判断模型。',review_model_unavailable:'这个模型尚未配置，请选择其他模型。',revision_conflict:'资料已更新，请刷新原文后重试。',review_reprocess_not_needed:'当前没有需要按新规则重新整理的资料。'};
    mount.innerHTML=`<section class="review-groups"><div class="section-head"><h2>待归档的新内容</h2><button data-rg="refresh">刷新资料</button></div><p data-rg-status role="status"></p><div class="review-workspace"><div class="review-list-column"><h3>1. 选择要审核的资料</h3><p data-rg-total></p><div data-rg-list></div><div class="actions"><button data-rg="previous">上一页</button><span data-rg-page></span><button data-rg="next">下一页</button></div></div>
    <div class="review-editor"><div data-rg-empty><h3>点击左侧资料，在这里核对原文和归属</h3></div><form data-rg-form hidden>
      <h3 data-rg-heading>2. 看清需求、处理经过和结果</h3><h4 data-rg-title></h4><p data-rg-source-label></p>
      <label data-rg-unit-picker>这份来源会话中的待确认任务<select name="unit"></select></label>
      <p class="review-question" data-rg-reason></p><div data-rg-narrative></div><pre class="review-original" data-rg-original></pre>
      <div data-rg-legacy hidden role="status"><p>这段会话正在等待自动整理成完整任务。完成后会显示需求、处理经过和结果。</p></div>
      <details data-rg-source open><summary>查看完整上下文</summary><div data-rg-source-text></div></details>
      <div data-rg-background></div><div data-rg-equivalence></div>
      <div data-rg-decision>
      <div class="section-head"><h3>3. 归入项目</h3><button type="button" class="write" data-rg="project-new">创建新项目</button></div><div data-rg-scope></div>
      <div class="actions review-archive-actions"><button type="button" class="primary write" data-rg="file" disabled>保存归档</button><button type="button" class="write" data-rg="defer">暂不归档</button></div><p data-rg-action-status role="status" aria-live="polite"></p>
      <details data-rg-ai><summary>AI 批量建议（可选）</summary><div class="organization-settings">

        <label>判断模型<select name="model_choice" aria-label="本次 AI 建议模型"><option value="auto">自动选择（正在读取模型）</option></select></label><label>本次处理哪些资料<select name="selection"><option value="item">只处理正在看的这一条</option><option value="group">从这份来源会话的待确认任务中查找</option><option value="all">从全部待确认资料中查找相似记忆</option></select></label>
      </div><div data-rg-batch hidden><label>归类依据<textarea rows="4" name="condition" maxlength="1000" placeholder="例如：介绍技术概念、原理或通用使用方法的资料，归入知识问答；实际修改某个项目的需求不属于这一类。"></textarea></label>
        <label>不适用的情况（选填）<textarea name="exceptions" rows="2" maxlength="500" placeholder="例如：虽提到某项技术，但主要是在安排具体项目的开发任务。"></textarea></label>
        <label>以后遇到同样内容<select name="scope"><option value="batch">只处理本次预览中的资料</option><option value="future">让 AI 继续按这份依据提供建议</option></select></label>
      </div><details><summary>补充说明（选填）</summary><input name="guidance" maxlength="1000" aria-label="补充归类说明"></details>
      <input type="hidden" name="group_id"><input type="hidden" name="task_id"><input type="hidden" name="digest"><input type="hidden" name="continue_job_id">
      <button type="button" data-rg="preview">预览建议</button><div data-rg-analysis class="review-analysis-status" role="status" aria-live="polite" hidden><span class="review-spinner" aria-hidden="true"></span><span data-rg-analysis-text></span><button type="button" data-rg="stop-analysis">停止判断</button><progress aria-label="归类判断进度"></progress></div><div data-rg-model-status class="review-model-status" role="status" hidden></div><div data-rg-reprocess-status role="status"></div><div data-rg-preview></div>
      <div class="actions review-archive-actions" data-rg-failure-actions hidden><button type="button" class="primary write" data-rg="archive-current">仅归档当前这条</button><button type="button" class="write" data-rg="defer-current">当前这条暂不归档</button><p data-rg-failure-save-status role="status"></p></div>
      <div class="review-apply-bar" hidden><p data-rg-save-status role="status"></p><button type="button" class="primary write" data-rg="apply" disabled>保存本批选择</button></div></details>
    </div></form></div></div></section>`;
    const form=$('[data-rg-form]');
    let scopePicker=EvolvProjects.scopePicker({mount:$('[data-rg-scope]'),projects,esc});
    const equivalents=EvolvKnowledgeEquivalence.create({mount:$('[data-rg-equivalence]'),api,esc,projects,onConfirm:async body=>{
      if(busy)return;setBusy(true);
      try{await api('equivalences/confirm',body);equivalents.invalidate();receipt('已确认是同一条知识','归属已沿用，两种表述与来源保存在项目知识目录中。');await refreshSaved('同义表达已保存，之后遇到已确认的表述会复用这次归属。');}
      finally{setBusy(false);}
    }});
    function notice(text){$('[data-rg-status]').textContent=text;}
    async function loadModels(){
      try{const result=await api('organization/groups/models'),select=form.elements.model_choice,chosen=select.value;
        select.innerHTML=result.items.map(x=>`<option value="${esc(x.id)}">${esc(x.label)}</option>`).join('');select.value=chosen||'auto';
      }catch(_){form.elements.model_choice.options[0].textContent='自动选择（模型列表暂不可用）';}
    }
    function modelReport(job){
      if(job?.id)modelReports.set(selectedGroup,job);
      const result=modelReports.get(selectedGroup),box=$('[data-rg-model-status]');box.hidden=!result;
      if(!result){box.replaceChildren();return;}
      const name=x=>({mimo:'MiMo',deepseek:'DeepSeek',kimi:'Kimi'}[x.provider]||x.provider||'判断模型')+' · '+(x.model||'');
      const calls=result.model_calls||[],legacy=result.model_status;
      const state={running:'调用中',succeeded:'调用成功',failed:'调用失败'};
      if(calls.length){
        box.innerHTML='<strong>本次模型调用</strong><ol>'+calls.map(x=>`<li>${esc(name(x))} · ${esc(state[x.state]||'状态未记录')}${x.fallback?' · 备用模型':''}${Number.isFinite(x.elapsed_ms)?` · ${(x.elapsed_ms/1000).toFixed(1)} 秒`:''}</li>`).join('')+'</ol>';
      }else if(legacy?.provider){
        box.textContent=`本批使用 ${name(legacy)}${legacy.fallback?'（备用模型）':''} · ${result.status==='completed'?'批次已完成；旧记录未保留逐次调用结果':'调用结果尚未记录'}`;
      }else{
        box.textContent=result.mode==='single'?'当前资料预览不调用 AI，按你选择的项目保存。':result.status==='completed'?(result.reused_count?'已复用此前判断，本次未调用判断模型':result.total===0?'未找到符合条件的待判断资料，本次未调用判断模型':'本批未记录模型调用信息'):
          result.status==='failed'?'本次尚无判断模型调用记录':'尚未调用判断模型，正在准备或检索资料';
      }
      if(['completed','failed','stopped'].includes(result.status)&&Number.isFinite(result.elapsed_seconds))box.insertAdjacentHTML('beforeend',`<p>本次总用时 ${result.elapsed_seconds} 秒</p>`);
    }
    function previewState(stage='',job={},error=''){
      const button=$('[data-rg=preview]'),status=$('[data-rg-analysis]'),active=['preparing','pending','running'].includes(stage);
      clearInterval(elapsedTimer);if(stage==='preparing'){previewStarted=Date.now();elapsedTimer=setInterval(()=>{if(!mount.isConnected){clearInterval(elapsedTimer);return;}$('[data-rg-analysis-text]').textContent=`正在准备预览 · 已用时 ${Math.floor((Date.now()-previewStarted)/1000)} 秒`;},1000);}
      modelReport(job);
      button.disabled=active;button.setAttribute('aria-busy',String(active));
      button.textContent=stage==='preparing'?'正在准备预览…':stage==='pending'?'等待 AI 开始…':stage==='running'?'AI 正在判断…':stage==='failed'?'重试预览':'预览建议';
      status.hidden=!stage;status.classList.toggle('is-error',stage==='failed');
      status.querySelector('.review-spinner').hidden=!active;
      let text=stage==='preparing'?'正在准备预览…':stage==='pending'?(job.waiting_for==='organization'?'等待当前整理任务结束后开始判断':'已提交，等待 AI 开始判断'):stage==='running'?'AI 正在阅读资料并判断归属':error;
      if(stage==='running'&&job.phase==='retrieval')text=`正在查找相似记忆${Number.isFinite(job.pool_count)?` · 待确认 ${job.pool_count} 条，已准备 ${job.indexed||0} 条`:''}`;
      if(stage==='running'&&job.phase==='reranking')text=`已召回 ${job.recalled_count||0} 条相似记忆，正在按相关度排序`;
      if(stage==='running'&&job.phase==='judging')text=`召回 ${job.recalled_count||0} 条相似记忆 · 本批已判断 ${job.processed} / ${job.total} 条`;
      if(stage==='running'&&job.model_status?.fallback){
        const provider={mimo:'MiMo',deepseek:'DeepSeek',kimi:'Kimi'}[job.model_status.provider]||'';
        text+=` · 已切换备用模型${provider?' '+provider:''}`;
      }
      $('[data-rg=stop-analysis]').hidden=!active||stage==='preparing';
      if(active&&job.elapsed_seconds)text+=` · 已用时 ${job.elapsed_seconds} 秒`;
      $('[data-rg-analysis-text]').textContent=text;
      $('[data-rg-failure-actions]').hidden=stage!=='failed';
      $('.review-apply-bar').hidden=!proposal;
      updateArchive();
      const progress=status.querySelector('progress');progress.hidden=!active;progress.max=job.total||1;
      if(Number.isFinite(job.processed))progress.value=job.processed;else progress.removeAttribute('value');
      if(active){$('[data-rg=apply]').disabled=true;$('[data-rg-save-status]').textContent='';}
    }
    function setBusy(value){busy=value;onBusy(value);const resume=$('[data-rg=resume-batch]');if(resume)resume.disabled=value;updateArchive();}
    function updateArchive(){const target=scopePicker.read();mount.querySelectorAll('[data-rg=file],[data-rg=archive-current]').forEach(b=>b.disabled=busy||!(target.projects.length||target.knowledge_scope==='global'));mount.querySelectorAll('[data-rg=defer],[data-rg=defer-current]').forEach(b=>b.disabled=busy);}
    function values(){const data=Object.fromEntries(new FormData(form));data.task_id=Number(data.task_id);return {...data,...scopePicker.read(),matching_mode:data.selection==='item'?'phrase':'semantic'};}
    function remember(){if(selectedGroup)drafts.set(selectedGroup,{values:values(),proposal,aiOpen:$('[data-rg-ai]').open});}
    function invalidate(){modelReports.delete(selectedGroup);form.elements.continue_job_id.value='';manualPreviews.clear();proposal=null;previewState();analyses.delete(selectedGroup);$('[data-rg="apply"]').hidden=false;$('[data-rg="apply"]').disabled=true;$('[data-rg-preview]').replaceChildren();$('[data-rg-save-status]').textContent='';updateArchive();remember();onDirty(true);}
    function highlight(){mount.querySelectorAll('[data-rg="choose"]').forEach(b=>{const yes=b.dataset.id===(requestedGroup||selectedGroup);b.setAttribute('aria-pressed',String(yes));b.closest('.review-source-card')?.classList.toggle('selected',yes);});}
    function closeEditor(){
      detailRequest?.controller.abort();detailRequest=null;requestedGroup='';$('.review-editor').setAttribute('aria-busy','false');
      ++chooseSequence;clearTimeout(analysisTimer);
      previewState();
      analyses.delete(selectedGroup);drafts.delete(selectedGroup);
      selectedGroup='';detail=null;proposal=null;manualPreviews.clear();
      form.hidden=true;form.inert=false;equivalents.detach();form.reset();$('[data-rg-action-status]').textContent='';scopePicker.set({});
      $('[data-rg-narrative]').replaceChildren();$('[data-rg-original]').textContent='';
      $('[data-rg-source]').ontoggle=null;$('[data-rg-source-text]').replaceChildren();
      $('[data-rg-preview]').replaceChildren();$('[data-rg-reprocess-status]').textContent='';
      $('[data-rg=apply]').disabled=true;highlight();onDirty(false);
    }
    function receipt(title,body,jobId=null){
      closeEditor();savedBatch=jobId;
      const pane=$('[data-rg-empty]');pane.hidden=false;
      pane.innerHTML=`<h3>${esc(title)}</h3><p>${esc(body)}</p>${jobId?'<button type="button" data-rg="resume-batch">继续核对本批资料</button>':''}`;
      const resume=pane.querySelector('[data-rg=resume-batch]');if(resume)resume.disabled=busy;
      pane.scrollIntoView({block:'nearest'});
    }
    async function refreshSaved(message){
      detailCache.clear();
      notice(message);
      try{await load();await onSaved();}
      catch(_){notice(message+' 页面刷新失败，请点击“刷新资料”重试，无需再次保存。');}
    }
    async function refreshEditor(){
      if(!selectedGroup||form.hidden)return;
      try{await choose(selectedGroup,{preserve:true,refresh:true});}
      catch(error){
        if(error.message!=='review_preview_changed')throw error;
        receipt('这条资料已处理','可以选择左侧其他资料继续审核。');
      }
    }
    async function resumeBatch(jobId=savedBatch){
      const job=await api('organization/groups/analysis?'+new URLSearchParams({id:jobId}));
      const entries=[...job.proposal.eligible,...job.proposal.excluded];
      const candidates=[...entries.filter(selectable),...entries.filter(u=>!selectable(u))];
      let group=null;
      for(const u of candidates){
        try{group=await api('organization/groups/detail?'+new URLSearchParams({task_id:u.task_id,digest:u.digest}));break;}
        catch(error){if(error.message!=='review_preview_changed')throw error;}
      }
      if(!group){receipt('本批审核已完成','审核结果已保存，可以选择其他资料继续处理。');return;}
      drafts.set(group.id,{values:job.request,proposal:job.proposal});
      await choose(group.id,{preserve:true,refresh:true});savedBatch=null;
      notice('已打开保存后的本批结果，可继续核对剩余资料，或修改之前的不录入决定。');
    }
    function narrative(record,overview={}){
      const rows=[['requirement','需求'],['analysis','原因'],['steps','处理重点'],['result','结果'],['outstanding','待完成']];
      const body=rows.map(([key,title])=>{const text=overview[key]||'';return text?`<div class="task-summary-row"><dt>${title}</dt><dd>${esc(text)}</dd></div>`:'';}).join('');
      const previous=record.prior_decisions||[];
      const decisions=[...new Set(previous.filter(p=>p.decision==='manual'||p.excluded_scopes?.length).map(p=>`「${p.title||'旧资料'}」：${p.decision==='manual'?`已确认用于「${EvolvProjects.scopeLabel(p,projects)}」`:''}${p.excluded_scopes?.length?` 不归入「${p.excluded_scopes.map(x=>x==='global'?'个人通用':projectName(x.replace('project:',''))).join('、')}」`:''}`))];
      return `<div class="task-narrative"><dl>${body}${decisions.length?`<div class="task-summary-row"><dt>旧片段判断</dt><dd>${esc(decisions.join('；'))}</dd></div>`:''}</dl></div>`;
    }
    async function load(){
      const sequence=++loadSequence;
      let result=await api('organization/groups?'+new URLSearchParams({page}));
      if(sequence!==loadSequence)return;
      const ids=[...new Set(result.items.flatMap(g=>g.examples.map(u=>u.task_id)))].filter(id=>!prepared.has(id)).slice(0,20);
      if(ids.length&&EvolvAuth.canWrite){
        const upgraded=await api('organization/prepare-review',{task_ids:ids});ids.forEach(id=>prepared.add(id));
        if(sequence!==loadSequence)return;
        if(upgraded.changed){detailCache.clear();result=await api('organization/groups?'+new URLSearchParams({page}));}
      }
      const lastPage=Math.max(1,Math.ceil(result.total/result.page_size));
      if(page>lastPage){page=lastPage;result=await api('organization/groups?'+new URLSearchParams({page}));}
      if(sequence!==loadSequence)return;
      await refreshLegacy();
      const snapshot=JSON.stringify(result);
      if(snapshot===listSnapshot)return;
      listSnapshot=snapshot;
      $('[data-rg-total]').textContent=`共 ${result.unit_count} 条待确认资料，分在 ${result.total} 组中`;
      $('[data-rg-page]').textContent=`第 ${page} / ${Math.max(1,Math.ceil(result.total/result.page_size))} 页`;
      $('[data-rg="previous"]').disabled=page<=1;$('[data-rg="next"]').disabled=page*result.page_size>=result.total;
      $('[data-rg-list]').innerHTML=result.items.map(g=>`<article class="review-source-card"><button type="button" data-rg="choose" data-id="${esc(g.id)}" aria-pressed="false"><strong>${esc(g.examples[0]?.title||g.title)}</strong><span>${g.count} 条 · 待归档</span><span class="review-source-name">来源：${esc(g.title)}</span></button></article>`).join('')||'<div class="empty">没有待归档的资料</div>';
      highlight();
    }
    async function refreshLegacy(){
      if(busy||detailRequest||!detail||form.hidden)return;
      const u=detail.items.find(x=>x.digest===form.elements.digest.value);
      if(!u||u.task_record?.version)return;
      let state=await api('organization/state?'+new URLSearchParams({task_id:u.task_id}));
      const original=state.id;
      for(let i=0;i<20&&state.superseded_by;i++)state=await api('organization/state?'+new URLSearchParams({task_id:state.superseded_by}));
      if(state.id===original)return;
      const status=$('[data-rg-legacy]');
      if(['pending','running'].includes(state.status)){status.textContent='正在自动整理完整任务，完成后会显示需求、处理经过和结果。';return;}
      if(state.status==='failed'){status.textContent='这段会话整理失败，请在“系统问题”查看原因并重试。';return;}
      const result=await api('organization/groups?'+new URLSearchParams({task_id:state.id}));
      if(result.total===1){await choose(result.items[0].id);notice('已自动更新为完整任务，可以直接核对需求、处理经过和结果。');return;}
      form.hidden=true;const pane=$('[data-rg-empty]');pane.hidden=false;
      pane.innerHTML=result.total?`<h3>这段会话已整理为 ${result.total} 个待确认任务</h3>${result.items.map(g=>`<p><button type="button" data-rg="choose" data-id="${esc(g.id)}">${esc(g.title)}</button></p>`).join('')}`:'<h3>这段会话已整理完成</h3><p>项目归属已确定，可在“已完成”或项目历史查看结果。</p>';
    }
    function showUnit(){
      const u=detail?.items.find(x=>`${x.task_id}:${x.digest}`===form.elements.unit.value);if(!u)return;
      form.elements.task_id.value=u.task_id;form.elements.digest.value=u.digest;
      $('[data-rg-title]').textContent=u.title;$('[data-rg-source-label]').textContent=`来源：${detail.title}`;
      equivalents.show(u);
      $('[data-rg-heading]').textContent=u.task_record?.new_content?'2. 核对新增内容':'2. 看清需求、处理经过和结果';
      const background=u.task_record?.confirmed_background||[];
      $('[data-rg-background]').innerHTML=background.length?`<details><summary>已确认内容 · ${background.length} 条，无需再次归类</summary>${background.map(x=>`<section><p>${x.action==='defer'?'已选择暂不归档':`已归入「${esc(x.projects.map(projectName).join('、')||'个人通用')}」`}</p><pre class="review-original">${esc(x.text)}</pre></section>`).join('')}</details>`:'';
      const complete=!!u.task_record?.version&&!u.task_record?.new_content;
      $('[data-rg-narrative]').innerHTML=complete?narrative(u.task_record,u.overview):'';
      $('[data-rg-original]').hidden=complete;$('[data-rg-original]').textContent=complete?'':u.text;
      $('[data-rg-legacy]').hidden=complete||!!u.task_record?.new_content;
      $('[data-rg-decision]').hidden=false;
      const suggested=u.suggestion?.projects?.length||u.suggestion?.knowledge_scope==='global';
      $('[data-rg-reason]').textContent=suggested?`AI 建议录入「${EvolvProjects.scopeLabel(u.suggestion,projects)}」`:'待你选择归入项目或暂不归档';
      const source=$('[data-rg-source]');source.ontoggle=null;source.open=!complete&&!u.task_record?.new_content;$('[data-rg-source-text]').replaceChildren();
      let loaded=false;const show=()=>{if(loaded||!source.open)return;loaded=true;EvolvTaskContext.render({mount:$('[data-rg-source-text]'),api,esc,query:'organization/context?'+new URLSearchParams({task_id:u.task_id,digest:u.digest})});};source.ontoggle=show;show();
    }
    async function choose(id,{preserve=false,member=null,signal,refresh=false}={}){
      const sequence=++chooseSequence;
      if(!preserve)remember();
      const cached=!refresh&&!member&&detailCache.get(id);
      const next=cached||await api('organization/groups/detail?'+new URLSearchParams(member?{task_id:member.task_id,digest:member.digest}:{group_id:id}),undefined,{signal});
      if(sequence!==chooseSequence)return false;
      detailCache.set(next.id,next);
      if(member){const old=drafts.get(selectedGroup);drafts.delete(selectedGroup);if(old)drafts.set(next.id,old);id=next.id;}
      selectedGroup=id;detail=next;savedBatch=null;$('[data-rg-action-status]').textContent='';$('[data-rg-failure-save-status]').textContent='';
      const draft=drafts.get(id);equivalents.detach();form.reset();form.elements.group_id.value=id;
      form.elements.unit.innerHTML=detail.items.map((u,i)=>`<option value="${esc(u.task_id+':'+u.digest)}">${i+1}. ${esc(u.title)}</option>`).join('');
      if(draft)for(const [k,v]of Object.entries(draft.values))if(!['projects','knowledge_scope'].includes(k)&&form.elements[k])form.elements[k].value=v;
      scopePicker.set(draft?.values||{});
      form.elements.group_id.value=id;
      // A reprocessed task has a new task id. A retained digest identifies the
      // same fragment; otherwise show the first new fragment for fresh review.
      if(!form.elements.unit.value){const old=detail.items.find(u=>u.digest===draft?.values.digest);form.elements.unit.value=old?`${old.task_id}:${old.digest}`:form.elements.unit.options[0]?.value||'';}
      $('[data-rg-unit-picker]').hidden=detail.items.length===1;showUnit();
      $('[data-rg-batch]').hidden=form.elements.selection.value==='item';proposal=draft?.proposal||null;
      $('[data-rg-ai]').open=!!(draft?.aiOpen||proposal||analyses.has(id));
      if(proposal?.job_id&&refresh){
        const job=await api('organization/groups/analysis?'+new URLSearchParams({id:proposal.job_id}),undefined,{signal});
        if(sequence!==chooseSequence)return false;
        proposal=job.proposal;modelReport(job);remember();
      }
      previewState();checkAnalysis();highlight();updateArchive();if(proposal)drawPreview();else{$('[data-rg-preview]').replaceChildren();$('[data-rg="apply"]').hidden=false;$('[data-rg="apply"]').disabled=true;$('[data-rg-save-status]').textContent='';}
      form.hidden=false;$('[data-rg-empty]').hidden=true;$('[data-rg-empty]').textContent='点击左侧资料，在这里核对原文和归属';
      return true;
    }
    async function openDetail(id,title){
      detailRequest?.controller.abort();clearTimeout(analysisTimer);remember();
      const request={controller:new AbortController(),timedOut:false};detailRequest=request;requestedGroup=id;
      const pane=$('[data-rg-empty]');form.hidden=true;form.inert=true;pane.hidden=false;
      pane.innerHTML=`<h3>${esc(title)}</h3><p class="actions" role="status"><span class="review-spinner" aria-hidden="true"></span> 正在打开这条资料…</p>`;
      $('.review-editor').setAttribute('aria-busy','true');highlight();notice('');
      const timer=setTimeout(()=>{request.timedOut=true;request.controller.abort();},10000);
      try{
        if(await choose(id,{preserve:true,signal:request.controller.signal}))requestedGroup='';
      }catch(error){
        if(detailRequest!==request)return;
        form.hidden=true;pane.hidden=false;
        const message=request.timedOut?'资料加载超时，请重试。':messages[error.message]||'资料暂时无法打开，请重试。';
        pane.innerHTML=`<h3>${esc(title)}</h3><p role="status">${esc(message)}</p><button type="button" data-rg="retry-detail" data-id="${esc(id)}" data-title="${esc(title)}">重新打开</button>`;
      }finally{
        clearTimeout(timer);
        if(detailRequest===request){detailRequest=null;form.inert=false;$('.review-editor').setAttribute('aria-busy','false');highlight();if(!form.hidden)checkAnalysis();}
      }
    }
    const unitKey=u=>`${u.task_id}:${u.digest}`;
    const selectable=u=>!u.reason_code||['ai_uncertain','ai_excluded','manual_excluded','value_review','batch_limit'].includes(u.reason_code);
    function decisionState(){
      if(!proposal)return null;
      let state=decisionDrafts.get(proposal.revision);
      if(!state){
        try{const saved=JSON.parse(sessionStorage.getItem('evolvmem.review.decisions.'+proposal.group_id)||'null');if(saved?.revision===proposal.revision)state=saved;}catch(_){}
        state=state||{revision:proposal.revision,choices:Object.fromEntries(proposal.eligible.map(u=>[unitKey(u),'include'])),picked:[]};
        decisionDrafts.set(proposal.revision,state);
      }
      return state;
    }
    function selectedDecisions(){
      const state=decisionState();
      return proposal?[...proposal.eligible,...proposal.excluded].filter(u=>selectable(u)&&['include','defer'].includes(state.choices[unitKey(u)]))
        .map(u=>({task_id:u.task_id,digest:u.digest,action:state.choices[unitKey(u)]})):[];
    }
    function updateDecisions(){
      const state=decisionState();if(!state)return;
      try{sessionStorage.setItem('evolvmem.review.decisions.'+proposal.group_id,JSON.stringify(state));}catch(_){}
      mount.querySelectorAll('[data-rg-pick]').forEach(el=>{el.checked=state.picked.includes(el.dataset.rgPick);});
      mount.querySelectorAll('[data-rg-choice]').forEach(el=>{el.value=state.choices[el.dataset.rgChoice]||'';});
      const decisions=selectedDecisions(), included=decisions.filter(d=>d.action==='include').length, excluded=decisions.length-included;
      const status=$('[data-rg-selected-count]');if(status)status.textContent=`已勾选 ${state.picked.length} 条`;
      for(const action of ['batch-include','batch-exclude']){const b=$(`[data-rg=${action}]`);if(b)b.disabled=!state.picked.length;}
      const button=$('[data-rg=apply]');button.hidden=false;button.disabled=!decisions.length||decisions.length>100;button.textContent='保存本批选择';
      $('.review-apply-bar').hidden=false;
      $('[data-rg-save-status]').textContent=decisions.length?`待保存：归入项目 ${included} 条；暂不归档 ${excluded} 条${decisions.length>100?'。每次最多保存 100 条。':''}`:
        proposal.excluded.some(u=>['rule_changed','review_changed'].includes(u.reason_code))?'有资料需要重新核对，完成后可保存。':'请选择归入项目或暂不归档。';
      remember();onDirty(decisions.length>0);
    }
    function decorateDecisions(){
      const items=[...proposal.eligible,...proposal.excluded].filter(selectable);
      if(!items.length){updateDecisions();return;}
      const toolbar=document.createElement('div');toolbar.className='review-batch-toolbar';
      toolbar.innerHTML='<div class="actions"><button type="button" data-rg="select-batch">全选本批</button><button type="button" data-rg="clear-batch">取消勾选</button><strong data-rg-selected-count></strong></div><div class="actions"><button type="button" data-rg="batch-include">选中项归入项目</button><button type="button" data-rg="batch-exclude">选中项暂不归档</button></div><p>本次归入：'+esc(EvolvProjects.scopeLabel(proposal,projects))+'</p>';
      if(items.length>100)toolbar.querySelector('[data-rg=select-batch]').textContent='勾选前 100 条';
      const first=$('[data-rg-preview] details');$('[data-rg-preview]').insertBefore(toolbar,first);
      mount.querySelectorAll('[data-review-key]').forEach(row=>{
        const u=items.find(x=>unitKey(x)===row.dataset.reviewKey);if(!u)return;
        const controls=document.createElement('div');controls.className='review-choice-row';
        controls.innerHTML=`<label class="review-pick"><input type="checkbox" data-rg-pick="${esc(unitKey(u))}" aria-label="勾选：${esc(u.title)}"><strong>${esc(u.title)}</strong></label><label>本次选择<select data-rg-choice="${esc(unitKey(u))}" aria-label="${esc(u.title)}的归档选择"><option value="">尚未选择</option><option value="include">归入项目</option><option value="defer">暂不归档</option></select></label>`;
        row.querySelector(':scope > strong')?.remove();row.prepend(controls);
      });
      updateDecisions();
    }
    function setDecision(keys,action){
      const state=decisionState();for(const key of keys)state.choices[key]=action;
      state.picked=[];updateDecisions();notice('选择已暂存，点击“保存本批选择”后生效。');
    }
    function drawPreview(){
      const p=proposal, target=EvolvProjects.scopeLabel(p,projects), semantic=p.matching_mode==='semantic';
      const pending=p.excluded.filter(u=>['ai_uncertain','review_changed'].includes(u.reason_code));
      const manualPending=p.excluded.filter(u=>['value_review','batch_limit'].includes(u.reason_code));
      const rejected=p.excluded.filter(u=>u.reason_code==='ai_excluded');
      const manuallyExcluded=p.excluded.filter(u=>u.reason_code==='manual_excluded');
      const unjudged=p.excluded.filter(u=>!['ai_uncertain','review_changed','ai_excluded','manual_excluded','value_review','batch_limit'].includes(u.reason_code));
      const judged=p.ai_judged_count??(p.eligible.length+rejected.length+p.excluded.filter(u=>u.reason_code==='ai_uncertain').length+(p.handled_count||0));
      const rows=(items,manual=false)=>items.map(u=>`<li data-review-key="${esc(unitKey(u))}"><strong>${esc(u.title)}</strong>${u.reason?`<p>${esc(u.reason)}</p>`:''}${Number.isFinite(u.relevance)?`<p>相关度 ${Math.round(u.relevance*100)}% · ${u.reason_code==='ai_excluded'?'AI 判断不符合':u.reason_code==='ai_uncertain'?'需要你判断':['value_review','batch_limit'].includes(u.reason_code)?'尚未判断':'AI 建议录入'}</p>`:''}${manual?`<button type="button" data-rg="review-one" data-task="${u.task_id}" data-digest="${esc(u.digest)}">核查记忆</button><div data-rg-manual-original hidden></div>`:`<button type="button" data-rg="original" data-task="${u.task_id}" data-digest="${esc(u.digest)}">展开原文</button><div data-rg-preview-original hidden></div>`}</li>`).join('');
      const section=(name,items,open=false,manual=false)=>items.length?`<details ${open?'open':''}><summary>${name} · ${items.length} 条</summary><ul>${rows(items,manual)}</ul></details>`:'';
      const stale=[...new Set(p.excluded.filter(u=>u.reason_code==='rule_changed').map(u=>u.source_key))];
      $('[data-rg-preview]').innerHTML=`<h3>AI 建议与本批选择</h3><p>${manualPending.length?`本次待处理资料：${p.eligible.length+p.excluded.length} 条${judged?`；已由 AI 判断：${judged} 条`:""}。`:semantic&&!Number.isFinite(p.recalled_count)?`这是之前完成的判断结果。点击预览，按相似度重新查找候选。已判断 ${judged} 条。`:semantic?`本次查找范围：${p.selection==='all'?'全部待确认资料':'这份来源会话的待确认任务'}。待确认 ${p.pool_count} 条，召回 ${p.recalled_count} 条相似记忆，筛选出 ${p.ranked_count} 条；本批已判断 ${judged} 条。`:`正在看的这一条：共 ${p.searched_count} 条。`}</p>
        ${semantic?`<p>判断依据：${esc(p.condition)}</p><p>${manualPending.length&&!rejected.length?"":`AI 判断不符合：${rejected.length} 条；`}需要你判断：${pending.length+manualPending.length} 条${manuallyExcluded.length?`；你已确认不归入：${manuallyExcluded.length} 条`:''}${p.handled_count?`；已人工处理：${p.handled_count} 条`:''}</p>`:''}<div class="review-counts">${manualPending.length?`<strong>待你决定是否录入「${esc(target)}」：${manualPending.length} 条</strong>`:''}${p.eligible.length||!manualPending.length?`<strong>${semantic?'AI 建议录入':'本次可归入'}「${esc(target)}」：${p.eligible.length} 条</strong>`:''}${p.excluded.length>manualPending.length||!manualPending.length?`<strong>未推荐或此前不录入：${semantic?rejected.length+pending.length+manuallyExcluded.length:p.excluded.length-manualPending.length} 条</strong>`:''}</div>
        ${p.scope==='future'?`<p>以后 AI 将按这份依据建议录入「${esc(target)}」，由你保存后归档。</p>`:''}
        ${stale.length?`<div class="review-recovery"><p>${stale.length} 份来源还需按最新规则整理。完成后会自动更新本次归类结果。</p><button type="button" class="write" data-rg="reprocess">按最新规则重新整理 ${Math.min(20,stale.length)} 份来源</button>${stale.length>20?'<p>其余来源留待下一批处理。</p>':''}</div>`:''}
        ${section('待你决定是否录入',manualPending,true)}
        ${semantic?section('按相关度排序的判断结果',[...p.eligible,...pending,...rejected].sort((a,b)=>(b.relevance||0)-(a.relevance||0)),true,true):section('待保存的资料',p.eligible,true)}
        ${semantic?section('以前已确认不归入',manuallyExcluded)+section('尚未判断的相似记忆',unjudged):section('本次未归入',p.excluded.filter(u=>!['value_review','batch_limit'].includes(u.reason_code)),true)}
        ${semantic&&p.remaining&&!p.needs_recheck?`<p>还有 ${p.remaining} 条相似记忆尚未判断；不相关时可到此结束。</p><button type="button" data-rg="next-batch">继续判断下一批（最多 20 条）</button>`:''}
        ${semantic&&form.elements.continue_job_id.value?'<button type="button" data-rg="previous-batch">查看上一批结果</button>':''}`;
      decorateDecisions();
      if(!pendingTasks.length&&$('[data-rg-reprocess-status]').textContent.includes('自动更新'))$('[data-rg-reprocess-status]').textContent='重新整理完成，归类结果已更新。';
    }
    async function checkAnalysis(){
      clearTimeout(analysisTimer);if(detailRequest||!mount.isConnected)return;
      const group=selectedGroup, pending=analyses.get(group);
      if(!pending){previewState();return;}
      if($('[data-rg-analysis]').hidden)previewState('pending');
      try{
        const job=await api('organization/groups/analysis?'+new URLSearchParams({id:pending.id}));
        if(detailRequest||selectedGroup!==group||analyses.get(group)?.id!==pending.id)return;
        if(['completed','stopped'].includes(job.status)){
          proposal=job.proposal;analyses.delete(group);previewState('',job);drawPreview();remember();notice(job.status==='stopped'?'已停止，已完成的判断仍可核对和保存。':job.total===0?'未找到符合条件的待判断资料，可直接归档当前这条。':'AI 建议已生成，选择后可保存归档。');return;
        }
        if(job.status==='failed'){analyses.delete(group);previewState('failed',job,messages[job.error]||job.error||'AI 判断未完成，请重试。');return;}
        previewState(job.status,job);
      }catch(error){if(detailRequest||selectedGroup!==group||analyses.get(group)?.id!==pending.id||!mount.isConnected)return;analyses.delete(group);previewState('failed',{},messages[error.message]||error.message);return;}
      analysisTimer=setTimeout(checkAnalysis,1500);
    }
    async function checkRecovery(){
      clearTimeout(recoveryTimer);if(!pendingTasks.length||!mount.isConnected)return;
      try{
        const states=await Promise.all(pendingTasks.map(id=>api('organization/detail?'+new URLSearchParams({task_id:id}))));
        const waiting=states.filter(t=>['pending','running'].includes(t.status));
        const failed=states.filter(t=>t.status==='failed');
        $('[data-rg-reprocess-status]').textContent=waiting.length?`正在重新整理 ${waiting.length} 份来源，项目选择与条件不变。`:failed.length?`有 ${failed.length} 份重新整理失败，请到“系统问题”查看原因并重试。`:'重新整理完成，正在自动更新归类结果。';
        if(!waiting.length){pendingTasks=[];detailCache.clear();await load();if(selectedGroup===recoveryGroup)try{await choose(selectedGroup,{preserve:true,refresh:true});if(!failed.length)await refreshPreview();}catch(error){if(error.message!=='review_preview_changed')throw error;form.hidden=true;$('[data-rg-empty]').hidden=false;$('[data-rg-empty]').textContent='重新整理后，这组资料已退出待确认列表；可在“已完成”查看处理结果。';}return;}
      }catch(error){$('[data-rg-reprocess-status]').textContent='暂时无法读取重新整理进度，可点击“刷新资料”重试。';}
      recoveryTimer=setTimeout(checkRecovery,2000);
    }
    async function refreshPreview(){
      const request=values(), requestGroup=selectedGroup;
      const unchanged=()=>selectedGroup===requestGroup&&JSON.stringify(values())===JSON.stringify(request);
      modelReports.delete(selectedGroup);previewState('preparing');
      if(request.selection==='item'){
        const result=await api('organization/groups/preview',request);if(!unchanged())return;proposal=result;previewState('',{id:'single:'+selectedGroup,mode:'single',status:'completed'});drawPreview();remember();notice([...proposal.eligible,...proposal.excluded].some(selectable)?'预览已更新，请选择本次录入的资料。':'预览已更新，暂时没有可保存的资料。');
      }else{
        const job=await api('organization/groups/analyze',request);if(!unchanged())return;proposal=null;analyses.set(selectedGroup,{id:job.id});$('[data-rg-preview]').replaceChildren();previewState(job.status,job);remember();await checkAnalysis();
      }
    }
    mount.addEventListener('input',e=>{if(e.target.closest('[data-rg-form]')&&!e.target.matches('select,[data-scope-search],[data-rg-pick],[data-rg-choice]'))invalidate();});
    mount.addEventListener('change',e=>{if(!e.target.closest('[data-rg-form]'))return;if(e.target.matches('[data-rg-pick]')){const state=decisionState();state.picked=[...mount.querySelectorAll('[data-rg-pick]:checked')].map(x=>x.dataset.rgPick);updateDecisions();return;}if(e.target.matches('[data-rg-choice]')){setDecision([e.target.dataset.rgChoice],e.target.value);return;}if(e.target.name==='unit'){showUnit();if(form.elements.selection.value!=='item'){remember();return;}}$('[data-rg-batch]').hidden=form.elements.selection.value==='item';invalidate();});
    form.onsubmit=e=>e.preventDefault();
    mount.addEventListener('click',async e=>{
      const b=e.target.closest('[data-rg]');if(!b)return;e.preventDefault();e.stopPropagation();if(busy)return;
      if(['choose','retry-detail'].includes(b.dataset.rg)){
        await openDetail(b.dataset.id,b.dataset.title||b.querySelector('strong')?.textContent||b.textContent);
        return;
      }
      const action=b.dataset.rg,saving=['apply','file','defer','archive-current','defer-current'].includes(action);
      const feedback=action==='apply'?$('[data-rg-save-status]'):action.endsWith('-current')?$('[data-rg-failure-save-status]'):$('[data-rg-action-status]');
      const finish=saving?EvolvUI.busy(b,'正在保存…'):()=>{};
      if(saving){feedback.textContent='正在保存，请稍候…';form.inert=true;}
      setBusy(true);
      try{
        if(action==='project-new'){
          await EvolvProjects.create({api,esc,onSaved:async project=>{
            const selected=scopePicker.read(),fresh=(await api('projects')).projects;
            projects.splice(0,projects.length,...fresh);
            scopePicker=EvolvProjects.scopePicker({mount:$('[data-rg-scope]'),projects,esc,value:{knowledge_scope:'project',projects:[...new Set([...selected.projects,project])]}});
            equivalents.invalidate();invalidate();$('[data-rg-action-status]').textContent=`已创建并选中「${projectName(project)}」`;
          }});
        }else if(['select-batch','clear-batch','batch-include','batch-exclude'].includes(action)){
          const state=decisionState();if(action==='select-batch')state.picked=[...mount.querySelectorAll('[data-rg-pick]')].slice(0,100).map(x=>x.dataset.rgPick);
          else if(action==='clear-batch')state.picked=[];else setDecision(state.picked,action==='batch-include'?'include':'defer');updateDecisions();
        }else if(action==='preview'){
          if(pendingTasks.length&&selectedGroup===recoveryGroup){notice('这份资料正在重新整理，请完成后再预览。');return;}
          await refreshPreview();
        }else if(action==='stop-analysis'){
          const pending=analyses.get(selectedGroup);if(pending){await api('organization/groups/stop',{id:pending.id});await checkAnalysis();}
        }else if(action==='next-batch'){
          form.elements.continue_job_id.value=proposal.job_id;await refreshPreview();
        }else if(action==='previous-batch'){
          await resumeBatch(form.elements.continue_job_id.value);
        }else if(action==='review-one'){
          const reviewedBatch=proposal,requestGroup=selectedGroup;
          const member={task_id:Number(b.dataset.task),digest:b.dataset.digest};
          const group=await api('organization/groups/detail?'+new URLSearchParams(member));
          const unit=group.items.find(u=>u.task_id===member.task_id&&u.digest===member.digest);
          const single=await api('organization/groups/preview',{...member,group_id:group.id,selection:'item',projects:reviewedBatch.projects,knowledge_scope:reviewedBatch.knowledge_scope});
          if(proposal!==reviewedBatch||selectedGroup!==requestGroup)return;
          if(single.eligible.length&&single.eligible[0].revision!==unit.revision)throw Error('review_preview_changed');
          manualPreviews.set(member.task_id+':'+member.digest,single);
          const dest=b.nextElementSibling;dest.hidden=false;const readable=await EvolvTaskContext.render({mount:dest,api,esc,query:'organization/context?'+new URLSearchParams(member)});
          if(!readable||proposal!==reviewedBatch||selectedGroup!==requestGroup||!dest.isConnected)return;
          const actions=document.createElement('div');actions.className='actions review-decision-actions';dest.appendChild(actions);
          if(single.eligible.length){const confirm=document.createElement('button');confirm.type='button';confirm.className='primary write';confirm.dataset.rg='confirm-one';confirm.dataset.task=member.task_id;confirm.dataset.digest=member.digest;confirm.textContent=`归入「${EvolvProjects.scopeLabel(single,projects)}」`;actions.appendChild(confirm);}
          else{const reason=document.createElement('p');reason.textContent=single.excluded[0]?.reason||'本条已处理，请刷新结果。';dest.appendChild(reason);}
          if(single.excluded[0]?.reason_code!=='manual_excluded'){const reject=document.createElement('button');reject.type='button';reject.dataset.rg='exclude-one';reject.dataset.task=member.task_id;reject.dataset.digest=member.digest;reject.textContent='暂不归档';actions.appendChild(reject);}
        }else if(['exclude-one','undo-exclude','confirm-one'].includes(action)){
          setDecision([b.dataset.task+':'+b.dataset.digest],action==='exclude-one'?'defer':'include');
        }else if(action==='apply'){
          const saved=proposal, decisions=selectedDecisions();
          const result=await api('organization/groups/decisions',{proposal:saved,decisions});
          decisionDrafts.delete(saved.revision);try{sessionStorage.removeItem('evolvmem.review.decisions.'+saved.group_id);}catch(_){}
          const target=EvolvProjects.scopeLabel(saved,projects);
          const message=`已保存：归入项目 ${result.included} 条；暂不归档 ${result.deferred} 条${result.guidance?'；已保存以后 AI 提供建议的依据':''}。`;
          // Close the saved editor before refreshing anything. A failed refresh
          // must never resurrect its stale prompt or invite a duplicate write.
          receipt('归档选择已保存',`归入「${target}」${result.included} 条；暂不归档 ${result.deferred} 条。`,saved.job_id);
          await refreshSaved(message);
        }else if(action==='reprocess'){
          if(pendingTasks.length){notice('上一批来源还在重新整理，完成后可继续处理下一批。');return;}
          const result=await api('organization/groups/reprocess',{proposal});detailCache.clear();pendingTasks=result.items.map(t=>t.id);recoveryGroup=selectedGroup;
          invalidate();$('[data-rg-reprocess-status]').textContent=`已提交 ${pendingTasks.length} 份来源重新整理，项目选择与条件不变。`;checkRecovery();
        }else if(action==='original'){
          const dest=b.nextElementSibling;
          if(!dest.hidden){dest.hidden=true;b.textContent='展开原文';return;}
          dest.hidden=false;await EvolvTaskContext.render({mount:dest,api,esc,query:'organization/context?'+new URLSearchParams({task_id:b.dataset.task,digest:b.dataset.digest})});b.textContent='收起原文';
        }else if(['file','defer','archive-current','defer-current'].includes(action)){
          const include=['file','archive-current'].includes(action);
          const u=detail.items.find(x=>x.digest===form.elements.digest.value&&x.task_id===Number(form.elements.task_id.value));
          const target=scopePicker.read();
          const result=await api('organization/archive',{task_id:u.task_id,digest:u.digest,expected_revision:u.revision,action:include?'include':'defer',...target});
          const message=include?`已归入「${EvolvProjects.scopeLabel(target,projects)}」${result.unit?.item_id?1:0} 条知识。`:'暂不归档，原文仍在，可在运行记录中返回待归档。';
          receipt(include?'归档已保存':'暂不归档',message);
          await refreshSaved(message);
        }else if(action==='resume-batch'){
          await resumeBatch();
        }else{
          if(action==='previous')page=Math.max(1,page-1);if(action==='next')page++;
          remember();if(action==='refresh'){detailCache.clear();prepared.clear();equivalents.invalidate();}await load();if(action==='refresh'){if(pendingTasks.length)await checkRecovery();else await refreshEditor();await onSaved();notice('资料已刷新。');}
        }
      }catch(error){
        if(['review_preview_changed','revision_conflict'].includes(error.message))detailCache.clear();
        notice(messages[error.message]||error.message);
        if(saving)feedback.textContent='保存未完成：'+(messages[error.message]||error.message);
        if(b.dataset.rg==='apply'&&!['review_preview_changed','revision_conflict'].includes(error.message)){
          // A failed write retains the user's choices for an explicit retry.
          remember();
        }else if(['apply','reprocess'].includes(b.dataset.rg)&&proposal?.job_id){
          try{const job=await api('organization/groups/analysis?'+new URLSearchParams({id:proposal.job_id}));proposal=job.proposal;drawPreview();remember();notice('已重新核对资料变化，未变化的建议仍可使用。请确认更新后的清单。');}
          catch(_){invalidate();}
        }else if(['apply','preview','reprocess'].includes(b.dataset.rg))invalidate();
        if(['preview','next-batch'].includes(b.dataset.rg))previewState('failed',{},messages[error.message]||error.message);
      }
      finally{finish();if(saving)form.inert=false;setBusy(false);}
    });
    await load();
    onReady();loadModels();
    async function restoreRecent(){
      const sequence=chooseSequence;
      try{
        const {job}=await api('organization/groups/recent-analysis');
        if(sequence!==chooseSequence||selectedGroup||!mount.isConnected)return;
        if(job){
          const resumeGroup=job.resume_group_id||job.request.group_id;
          modelReports.set(resumeGroup,job);drafts.set(resumeGroup,{values:job.request,proposal:job.proposal||null});
          if(job.status!=='completed')analyses.set(resumeGroup,{id:job.id});
          if(await choose(resumeGroup))notice(job.status==='completed'?'已恢复上次判断结果和项目选择，可继续核对并确认归类。':'已恢复上次判断任务，请等待处理结果。');
        }
      }catch(error){if(!selectedGroup&&sequence===chooseSequence&&mount.isConnected)notice(messages[error.message]||error.message);}
    }
    // Optional saved-work recovery must not hold the visible list inert.
    restoreRecent();
    const history=document.createElement('section');history.className='progress-history';historyMount.appendChild(history);
    history.innerHTML='<h2>进度已保存，无需审核</h2><form><label>查找进度原文<input name="query" maxlength="200"></label><button type="submit">查找</button></form><p data-history-status role="status"></p><div data-history-list></div><div class="actions"><button data-history-page="previous">上一页</button><span data-history-page-label></span><button data-history-page="next">下一页</button></div>';
    let historyPage=1, historyItems=[];
    async function historyLoad(){
      const result=await api('organization/progress?'+new URLSearchParams({query:history.querySelector('input').value,page:historyPage}));
      historyItems=result.items;
      history.querySelector('[data-history-list]').innerHTML=result.items.map((u,i)=>`<details class="organization-task"><summary>${esc(u.title)} · ${esc(u.project||'未归属')}</summary><p>${esc(u.disposition_reason)}</p><pre class="organization-text">${esc(u.text)}</pre><button data-progress-restore="${i}">重新整理</button></details>`).join('')||'<p class="hint">暂无匹配进度。</p>';
      history.querySelector('[data-history-page-label]').textContent=`共 ${result.total} 条 · 第 ${historyPage} 页`;
      history.querySelector('[data-history-page="previous"]').disabled=historyPage<=1;
      history.querySelector('[data-history-page="next"]').disabled=historyPage*result.page_size>=result.total;
    }
    history.querySelector('form').onsubmit=async e=>{e.preventDefault();historyPage=1;try{await historyLoad();}catch(error){history.querySelector('[data-history-status]').textContent=error.message;}};
    history.addEventListener('click',async e=>{
      const b=e.target.closest('[data-progress-restore],[data-history-page]');if(!b||busy)return;e.preventDefault();e.stopPropagation();setBusy(true);
      try{
        if(b.dataset.progressRestore!==undefined){const u=historyItems[Number(b.dataset.progressRestore)];await api('organization/disposition',{task_id:u.task_id,digest:u.digest,expected_revision:u.revision,disposition:'keep'});history.querySelector('[data-history-status]').textContent='已返回待归档，请选择项目后保存。';await onSaved();await load();}
        else historyPage+=b.dataset.historyPage==='next'?1:-1;
        await historyLoad();
      }catch(error){history.querySelector('[data-history-status]').textContent=error.message;}
      finally{setBusy(false);}
    });
    await historyLoad();
    return {refresh:async()=>{detailCache.clear();await load();await refreshEditor();},poll:async()=>{if(!busy&&!detailRequest)await load();},resume:()=>{checkRecovery();checkAnalysis();}};
  }};
})();
