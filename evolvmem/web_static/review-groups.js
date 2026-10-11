/* Review the original in place; every write is bound to an explicit preview. */
(() => {
  window.EvolvReviewGroups = {async render({mount, api, esc, projects, onSaved, onDirty, onBusy, historyMount=mount}) {
    let page=1, proposal=null, busy=false, selectedGroup='', detail=null, pendingTasks=[], recoveryTimer=null, recoveryGroup='', listSnapshot='', loadSequence=0;
    const drafts=new Map(), sourceCache=new Map(), analyses=new Map();let analysisTimer=null;
    const $=s=>mount.querySelector(s);
    const projectName=p=>projects.find(x=>x.project===p)?.display_name||p;
    const messages={review_preview_changed:'资料或规则已变化，请刷新资料并重新预览。',review_condition_required:'请说明按什么业务依据判断这些资料的适用范围。',review_ai_invalid_response:'AI 返回的判断不完整，请重试。',review_ai_failed:'AI 判断未完成，资料尚未修改，请重试。',review_ai_not_ready:'AI 还在判断，请等待预览完成。',extraction_provider_unavailable:'整理模型暂不可用，资料尚未修改。',review_preview_required:'请先预览并核对本次归类范围。',invalid_project:'请先选择适用项目。',project_required:'请勾选至少一个项目，或选择“个人通用”。',invalid_projects:'请选择有效的项目。',revision_conflict:'资料已更新，请刷新原文后重试。',review_reprocess_not_needed:'当前没有需要按新规则重新整理的资料。'};
    mount.innerHTML=`<section class="review-groups"><div class="section-head"><h2>待你确认的资料</h2><button data-rg="refresh">刷新资料</button></div><p data-rg-status role="status"></p><div class="review-workspace"><div class="review-list-column"><h3>1. 选择要审核的资料</h3><p data-rg-total></p><div data-rg-list></div><div class="actions"><button data-rg="previous">上一页</button><span data-rg-page></span><button data-rg="next">下一页</button></div></div>
    <div class="review-editor"><div data-rg-empty><h3>点击左侧资料，在这里核对原文和归属</h3></div><form data-rg-form hidden>
      <h3>2. 核对原文</h3><h4 data-rg-title></h4><p data-rg-source-label></p>
      <label data-rg-unit-picker>这份会话中需要确认的资料<select name="unit"></select></label>
      <p class="review-question" data-rg-reason></p><pre class="review-original" data-rg-original></pre>
      <details data-rg-source><summary>查看所在会话全文</summary><div data-rg-source-text></div></details>
      <div class="actions" data-rg-value hidden><button type="button" data-rg="keep">保留这条资料</button><button type="button" data-rg="aside">暂存，稍后再判断</button></div>
      <h3>3. 选择这条知识适用于哪里</h3><div data-rg-scope></div><div class="organization-settings">

        <label>本次处理哪些资料<select name="selection"><option value="item">只处理正在看的这一条</option><option value="group">在左侧选中这组内交给 AI 判断</option><option value="all">在全部待确认资料中交给 AI 判断</option></select></label>
      </div><div data-rg-batch hidden><label>归类依据<textarea rows="4" name="condition" maxlength="1000" placeholder="例如：介绍技术概念、原理或通用使用方法的资料，归入知识问答；实际修改某个项目的需求不属于这一类。"></textarea></label>
        <label>不适用的情况（选填）<textarea name="exceptions" rows="2" maxlength="500" placeholder="例如：虽提到某项技术，但主要是在安排具体项目的开发任务。"></textarea></label>
        <label>以后遇到同样内容<select name="scope"><option value="batch">只处理本次预览中的资料</option><option value="future">让 AI 继续按这份依据判断</option></select></label>
      </div><details><summary>补充说明（选填）</summary><input name="guidance" maxlength="1000" aria-label="补充归类说明"></details>
      <input type="hidden" name="group_id"><input type="hidden" name="task_id"><input type="hidden" name="digest">
      <button type="button" data-rg="preview">预览归类结果</button><div data-rg-reprocess-status role="status"></div><div data-rg-preview></div>
      <div class="review-apply-bar"><p data-rg-save-status role="status">选择适用范围后，先预览再保存。</p><button type="button" class="primary write" data-rg="apply" disabled>确认归类</button></div>
    </form></div></div></section>`;
    const form=$('[data-rg-form]');
    const scopePicker=EvolvProjects.scopePicker({mount:$('[data-rg-scope]'),projects,esc});
    function notice(text){$('[data-rg-status]').textContent=text;}
    function values(){const data=Object.fromEntries(new FormData(form));data.task_id=Number(data.task_id);return {...data,...scopePicker.read(),matching_mode:data.selection==='item'?'phrase':'semantic'};}
    function remember(){if(selectedGroup)drafts.set(selectedGroup,{values:values(),proposal});}
    function invalidate(){$('[data-rg="preview"]').disabled=false;analyses.delete(selectedGroup);proposal=null;$('[data-rg="apply"]').disabled=true;$('[data-rg="apply"]').textContent='先预览，再确认归类';$('[data-rg-preview]').replaceChildren();$('[data-rg-save-status]').textContent='选择已修改，请预览本次归类结果。';remember();onDirty(true);}
    function highlight(){mount.querySelectorAll('[data-rg="choose"]').forEach(b=>{const yes=b.dataset.id===selectedGroup;b.setAttribute('aria-pressed',String(yes));b.closest('.review-source-card').classList.toggle('selected',yes);});}
    async function load(){
      const sequence=++loadSequence;
      const result=await api('organization/groups?'+new URLSearchParams({page}));
      if(sequence!==loadSequence)return;
      const snapshot=JSON.stringify(result);
      if(snapshot===listSnapshot)return;
      listSnapshot=snapshot;
      $('[data-rg-total]').textContent=`共 ${result.unit_count} 条待确认资料，分在 ${result.total} 组中`;
      $('[data-rg-page]').textContent=`第 ${page} / ${Math.max(1,Math.ceil(result.total/result.page_size))} 页`;
      $('[data-rg="previous"]').disabled=page<=1;$('[data-rg="next"]').disabled=page*result.page_size>=result.total;
      $('[data-rg-list]').innerHTML=result.items.map(g=>`<article class="review-source-card"><button type="button" data-rg="choose" data-id="${esc(g.id)}" aria-pressed="false"><strong>${esc(g.examples[0]?.title||g.title)}</strong><span>${g.count} 条 · ${g.reason==='价值待确认'?'需要确认是否保留':g.reason==='归属有冲突'?'需要解决归属冲突':'需要确认适用范围'}</span><span class="review-source-name">来源：${esc(g.title)}</span></button></article>`).join('')||'<div class="empty">没有需要你确认的资料</div>';
      highlight();
    }
    function showUnit(){
      const u=detail?.items.find(x=>`${x.task_id}:${x.digest}`===form.elements.unit.value);if(!u)return;
      form.elements.task_id.value=u.task_id;form.elements.digest.value=u.digest;
      $('[data-rg-title]').textContent=u.title;$('[data-rg-source-label]').textContent=`来源：${detail.title}`;
      $('[data-rg-original]').textContent=u.text;
      $('[data-rg-reason]').textContent=u.disposition==='review'?'需要你确认是否保留：'+(u.disposition_reason||u.reason):'需要你确认适用范围：'+(u.reason||'系统无法确定这条知识是个人通用还是用于哪些项目');
      $('[data-rg-value]').hidden=u.disposition!=='review';$('[data-rg-source]').open=false;$('[data-rg-source-text]').replaceChildren();
    }
    async function choose(id,{preserve=false,member=null}={}){
      if(!preserve)remember();
      const next=await api('organization/groups/detail?'+new URLSearchParams(member?{task_id:member.task_id,digest:member.digest}:{group_id:id}));
      if(member){const old=drafts.get(selectedGroup);drafts.delete(selectedGroup);if(old)drafts.set(next.id,old);id=next.id;}
      selectedGroup=id;detail=next;form.hidden=false;$('[data-rg-empty]').hidden=true;$('[data-rg-empty]').textContent='点击左侧资料，在这里核对原文和归属';
      const draft=drafts.get(id);form.reset();form.elements.group_id.value=id;
      form.elements.unit.innerHTML=detail.items.map((u,i)=>`<option value="${esc(u.task_id+':'+u.digest)}">${i+1}. ${esc(u.title)}</option>`).join('');
      if(draft)for(const [k,v]of Object.entries(draft.values))if(!['projects','knowledge_scope'].includes(k)&&form.elements[k])form.elements[k].value=v;
      scopePicker.set(draft?.values||{});
      form.elements.group_id.value=id;
      // A reprocessed task has a new task id. A retained digest identifies the
      // same fragment; otherwise show the first new fragment for fresh review.
      if(!form.elements.unit.value){const old=detail.items.find(u=>u.digest===draft?.values.digest);form.elements.unit.value=old?`${old.task_id}:${old.digest}`:form.elements.unit.options[0]?.value||'';}
      $('[data-rg-unit-picker]').hidden=detail.items.length===1;showUnit();
      $('[data-rg-batch]').hidden=form.elements.selection.value==='item';proposal=draft?.proposal||null;
      checkAnalysis();highlight();if(proposal)drawPreview();else{$('[data-rg-preview]').replaceChildren();$('[data-rg="apply"]').disabled=true;$('[data-rg="apply"]').textContent='先预览，再确认归类';$('[data-rg-save-status]').textContent='选择适用范围后，先预览再保存。';}
    }
    function drawPreview(){
      const p=proposal, target=EvolvProjects.scopeLabel(p,projects);
      const rows=(items,excluded)=>items.map(u=>`<li><strong>${esc(u.title)}</strong>${u.reason?`<p>${esc(u.reason)}</p>`:''}<blockquote>${esc(u.evidence_quote||u.quote)}</blockquote><button type="button" data-rg="original" data-task="${u.task_id}" data-digest="${esc(u.digest)}">展开原文</button><div data-rg-preview-original hidden></div></li>`).join('');
      const stale=[...new Set(p.excluded.filter(u=>u.reason_code==='rule_changed').map(u=>u.source_key))];
      $('[data-rg-preview]').innerHTML=`<h3>4. 核对本次归类结果</h3><p>${p.selection==='all'?'全部待确认资料':p.selection==='group'?'左侧选中这组资料':'正在看的这一条'}：检查 ${p.searched_count} 条${p.matching_mode==='semantic'?`，AI 已按业务依据判断 ${p.eligible.length+p.excluded.filter(u=>['ai_excluded','ai_uncertain'].includes(u.reason_code)).length} 条`:''}。</p>
        ${p.matching_mode==='semantic'?`<p>判断依据：${esc(p.condition)}</p><p>需要你判断：${p.excluded.filter(u=>u.reason_code==='ai_uncertain').length} 条</p>`:''}<div class="review-counts"><strong>归入「${esc(target)}」：${p.eligible.length} 条</strong><strong>本次不归入：${p.excluded.length} 条</strong></div>
        ${p.scope==='future'?`<p>保存后，AI 将按“${esc(p.condition)}”及不适用情况判断新资料，符合时归入「${esc(target)}」。</p>`:''}
        ${stale.length?`<div class="review-recovery"><p>${stale.length} 份来源使用了旧版整理规则。重新整理后，请核对新结果再保存。</p><button type="button" class="write" data-rg="reprocess">按最新规则重新整理 ${Math.min(20,stale.length)} 份来源</button>${stale.length>20?'<p>其余来源留待下一批处理。</p>':''}</div>`:''}
        <details open><summary>将归入的资料 · ${p.eligible.length} 条</summary>${p.eligible.length?`<ul>${rows(p.eligible,false)}</ul>`:'<p>没有可归入的资料。</p>'}</details>
        ${p.excluded.length?`<details open><summary>不建议归入或需要你判断的资料 · ${p.excluded.length} 条</summary><ul>${rows(p.excluded,true)}</ul></details>`:''}`;
      $('[data-rg="apply"]').disabled=!p.eligible.length;
      $('[data-rg="apply"]').textContent=`确认归入「${target}」${p.eligible.length} 条`;
      $('[data-rg-save-status]').textContent=p.eligible.length?'核对以上清单后即可保存。':stale.length?'当前没有可保存的资料。请先点击上方“按最新规则重新整理”。':'当前没有可保存的资料。请查看上方原因，调整查找范围或条件后重新预览。';
    }
    async function checkAnalysis(){
      clearTimeout(analysisTimer);if(!mount.isConnected)return;
      const group=selectedGroup, pending=analyses.get(group);
      if(!pending){$('[data-rg=preview]').disabled=false;return;}
      try{
        const job=await api('organization/groups/analysis?'+new URLSearchParams({id:pending.id}));
        if(selectedGroup!==group||analyses.get(group)?.id!==pending.id)return;
        if(job.status==='completed'){
          proposal=job.proposal;analyses.delete(group);drawPreview();remember();$('[data-rg=preview]').disabled=false;notice('AI 判断完成，请核对每条理由和原文后保存。');return;
        }
        if(job.status==='failed')throw Error(messages[job.error]||job.error||'AI 判断未完成，请重试。');
        $('[data-rg-save-status]').textContent=`AI ${job.status==='pending'?'等待开始':'正在阅读资料'}：已判断 ${job.processed} / ${job.total} 条`;
      }catch(error){analyses.delete(group);$('[data-rg=preview]').disabled=false;$('[data-rg-save-status]').textContent=messages[error.message]||error.message;return;}
      analysisTimer=setTimeout(checkAnalysis,1500);
    }
    async function checkRecovery(){
      clearTimeout(recoveryTimer);if(!pendingTasks.length||!mount.isConnected)return;
      try{
        const states=await Promise.all(pendingTasks.map(id=>api('organization/detail?'+new URLSearchParams({task_id:id}))));
        const waiting=states.filter(t=>['pending','running'].includes(t.status));
        const failed=states.filter(t=>t.status==='failed');
        $('[data-rg-reprocess-status]').textContent=waiting.length?`正在重新整理 ${waiting.length} 份来源，项目选择与条件已保留。`:failed.length?`有 ${failed.length} 份重新整理失败，请到“系统问题”查看原因并重试。`:'重新整理完成。请核对原文，再点击“预览归类结果”。';
        if(!waiting.length){pendingTasks=[];await load();if(selectedGroup===recoveryGroup)try{await choose(selectedGroup,{preserve:true});}catch(error){form.hidden=true;$('[data-rg-empty]').hidden=false;$('[data-rg-empty]').textContent='重新整理后，这组资料已退出待确认列表；可在“已完成”查看处理结果。';}return;}
      }catch(error){$('[data-rg-reprocess-status]').textContent='暂时无法读取重新整理进度，可点击“刷新资料”重试。';}
      recoveryTimer=setTimeout(checkRecovery,2000);
    }
    mount.addEventListener('input',e=>{if(e.target.closest('[data-rg-form]')&&!e.target.matches('select,[data-scope-search]'))invalidate();});
    mount.addEventListener('change',e=>{if(!e.target.closest('[data-rg-form]'))return;if(e.target.name==='unit')showUnit();$('[data-rg-batch]').hidden=form.elements.selection.value==='item';invalidate();});
    form.onsubmit=e=>e.preventDefault();
    $('[data-rg-source]').addEventListener('toggle',async()=>{
      const box=$('[data-rg-source]');if(!box.open)return;const id=Number(form.elements.task_id.value), dest=$('[data-rg-source-text]');
      dest.textContent='正在读取会话全文…';
      try{const task=sourceCache.get(id)||await api('organization/detail?'+new URLSearchParams({task_id:id}));sourceCache.set(id,task);if(Number(form.elements.task_id.value)!==id)return;const pre=document.createElement('pre');pre.className='review-original';pre.textContent=task.source_text||'没有可读取的会话原文。';dest.replaceChildren(pre);}catch(error){dest.textContent=messages[error.message]||error.message;}
    });
    mount.addEventListener('click',async e=>{
      const b=e.target.closest('[data-rg]');if(!b)return;e.preventDefault();e.stopPropagation();if(busy)return;
      busy=true;onBusy(true);
      try{
        const action=b.dataset.rg;
        if(action==='choose'){await choose(b.dataset.id);}
        else if(action==='preview'){
          if(pendingTasks.length&&selectedGroup===recoveryGroup){notice('这份资料正在重新整理，请完成后再预览。');return;}
          const request=values(), requestGroup=selectedGroup;
          const unchanged=()=>selectedGroup===requestGroup&&JSON.stringify(values())===JSON.stringify(request);
          if(request.selection==='item'){
            const result=await api('organization/groups/preview',request);if(!unchanged())return;proposal=result;drawPreview();remember();notice(proposal.eligible.length?'预览已更新，请核对归类清单。':'预览已更新，暂时没有可保存的资料。');
          }else{
            const job=await api('organization/groups/analyze',request);if(!unchanged())return;proposal=null;analyses.set(selectedGroup,{id:job.id});$('[data-rg=apply]').disabled=true;$('[data-rg-preview]').replaceChildren();$('[data-rg-save-status]').textContent=`AI 等待判断 ${job.total} 条资料…`;$('[data-rg=preview]').disabled=true;remember();checkAnalysis();
          }
        }else if(action==='apply'){
          const saved=proposal, result=await api('organization/groups/apply',{proposal:saved});
          notice(`已归入「${EvolvProjects.scopeLabel(saved,projects)}」${result.succeeded} 条${result.failed?`，${result.failed} 条未保存，请刷新重试`:''}${result.guidance?'；已保存以后同类资料的归类条件':''}。`);
          proposal=null;drafts.delete(selectedGroup);selectedGroup='';form.hidden=true;$('[data-rg-empty]').hidden=false;onDirty(drafts.size>0);await load();await onSaved();
        }else if(action==='reprocess'){
          if(pendingTasks.length){notice('上一批来源还在重新整理，完成后可继续处理下一批。');return;}
          const result=await api('organization/groups/reprocess',{proposal});pendingTasks=result.items.map(t=>t.id);recoveryGroup=selectedGroup;
          invalidate();$('[data-rg-reprocess-status]').textContent=`已提交 ${pendingTasks.length} 份来源重新整理，项目选择与条件已保留。`;checkRecovery();
        }else if(action==='original'){
          const dest=b.nextElementSibling;
          if(!dest.hidden){dest.hidden=true;b.textContent='展开原文';return;}
          const task=await api('organization/detail?'+new URLSearchParams({task_id:b.dataset.task}));
          const u=task.units.find(u=>u.digest===b.dataset.digest);const pre=document.createElement('pre');pre.className='review-original';pre.textContent=u?.text||'这条资料已更新，请重新预览。';dest.replaceChildren(pre);dest.hidden=false;b.textContent='收起原文';
        }else if(action==='keep'||action==='aside'){
          const u=detail.items.find(x=>x.digest===form.elements.digest.value&&x.task_id===Number(form.elements.task_id.value));
          await api('organization/disposition',{task_id:u.task_id,digest:u.digest,expected_revision:u.revision,disposition:action==='keep'?'keep':'set_aside',reason:action==='aside'?'用户选择暂存，稍后判断':''});
          invalidate();await load();if(action==='keep')try{await choose(null,{preserve:true,member:u});}catch(error){if(error.message!=='review_preview_changed')throw error;form.hidden=true;$('[data-rg-empty]').hidden=false;}else{form.hidden=true;$('[data-rg-empty]').hidden=false;}await onSaved();notice(action==='keep'?'已确认保留；未确定项目的资料可继续预览归类，已确定项目的会继续整理。':'已暂存，可在运行记录中恢复。');
        }else{
          if(action==='previous')page=Math.max(1,page-1);if(action==='next')page++;
          remember();await load();if(action==='refresh'){if(pendingTasks.length)await checkRecovery();else if(selectedGroup)try{await choose(selectedGroup,{preserve:true});}catch(error){notice(messages[error.message]||error.message);}}
        }
      }catch(error){notice(messages[error.message]||error.message);if(['apply','preview','reprocess'].includes(b.dataset.rg))invalidate();}
      finally{busy=false;onBusy(false);}
    });
    await load();
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
      const b=e.target.closest('[data-progress-restore],[data-history-page]');if(!b||busy)return;e.preventDefault();e.stopPropagation();busy=true;onBusy(true);
      try{
        if(b.dataset.progressRestore!==undefined){const u=historyItems[Number(b.dataset.progressRestore)];await api('organization/disposition',{task_id:u.task_id,digest:u.digest,expected_revision:u.revision,disposition:'keep'});history.querySelector('[data-history-status]').textContent='已恢复，归属或价值尚未确认的条目会回到待审核。';await onSaved();await load();}
        else historyPage+=b.dataset.historyPage==='next'?1:-1;
        await historyLoad();
      }catch(error){history.querySelector('[data-history-status]').textContent=error.message;}
      finally{busy=false;onBusy(false);}
    });
    await historyLoad();
    return {refresh:load,poll:async()=>{if(!busy)await load();},resume:()=>{checkRecovery();checkAnalysis();}};
  }};
})();
