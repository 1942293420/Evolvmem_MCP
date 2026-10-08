/* One instruction, an explicit preview, then current matching units only. */
(() => {
  window.EvolvReviewGroups = {async render({mount, api, esc, projects, onSaved, onDirty, onBusy}) {
    let page=1, proposal=null, busy=false;
    const $=s=>mount.querySelector(s);
    const messages={review_preview_changed:'资料或规则已变化，请重新预览。',review_condition_required:'请填写具体、连续的适用短语，例如“达人建联 SOP”。',review_preview_required:'请先预览并核对本次范围。',invalid_project:'请选择项目。'};
    mount.innerHTML=`<section class="panel"><div class="section-head"><div><h2>集中审核</h2><p class="hint">按同一会话、疑问类型与资料类别汇总。填写一次条件，先核对匹配与例外，再处理符合条件的条目。</p></div><button data-rg="refresh">刷新分组</button></div><p data-rg-status role="status"></p><div data-rg-list></div><div class="actions"><button data-rg="previous">上一组页</button><span data-rg-page></span><button data-rg="next">下一组页</button></div>
      <form data-rg-form hidden><h3 data-rg-title></h3><div class="organization-settings"><label>归属项目<select name="project"><option value="">选择项目</option>${projects.filter(p=>p.status==='active').map(p=>`<option value="${esc(p.project)}">${esc(p.display_name||p.project)}</option>`).join('')}</select></label><label>适用短语<input name="condition" maxlength="500" placeholder="正文中实际出现的连续短语"></label><label>例外（多项用 | 分隔）<input name="exceptions" maxlength="500"></label><label>指导说明<input name="guidance" maxlength="1000" placeholder="可选；说明这次如何归类"></label><label>范围<select name="scope"><option value="batch">仅本次</option><option value="future">以后同类适用</option></select></label></div><input type="hidden" name="group_id"><button type="button" data-rg="preview">预览匹配范围</button><div data-rg-preview></div><button type="button" class="primary write" data-rg="apply" disabled>保存预览中的符合项</button></form></section>`;
    function notice(text){$('[data-rg-status]').textContent=text;}
    function invalidate(){proposal=null;$('[data-rg="apply"]').disabled=true;$('[data-rg-preview]').textContent='条件已修改，请重新预览。';onDirty(true);}
    async function load(){
      const result=await api('organization/groups?'+new URLSearchParams({page}));
      $('[data-rg-page]').textContent=`${result.total} 组 · ${result.unit_count} 条 · 第 ${page} 页`;
      $('[data-rg="previous"]').disabled=page<=1;
      $('[data-rg="next"]').disabled=page*result.page_size>=result.total;
      $('[data-rg-list]').innerHTML=result.items.map(g=>`<details class="organization-task"><summary>${esc(g.title)} · ${esc(g.reason)} · ${g.count} 条</summary><p class="hint">这里只按来源和疑问归组，项目仍以你预览的条件为准。</p>${g.examples.map(u=>`<blockquote>${esc(u.quote)}<small>${esc(u.source_key)} · ${esc(u.title)}</small></blockquote>`).join('')}<button data-rg="choose" data-id="${esc(g.id)}" data-title="${esc(g.title)}">填写这一组的条件</button></details>`).join('')||'<p class="hint">当前没有需要集中审核的条目。</p>';
    }
    mount.addEventListener('input',e=>{if(e.target.closest('[data-rg-form]'))invalidate();});
    mount.addEventListener('change',e=>{if(e.target.closest('[data-rg-form]'))invalidate();});
    $('[data-rg-form]').onsubmit=e=>e.preventDefault();
    mount.addEventListener('click',async e=>{
      const b=e.target.closest('[data-rg]');if(!b)return;e.preventDefault();e.stopPropagation();if(busy)return;
      busy=true;onBusy(true);
      try{
        const action=b.dataset.rg, form=$('[data-rg-form]');
        if(action==='choose'){
          form.reset();form.hidden=false;form.elements.group_id.value=b.dataset.id;
          $('[data-rg-title]').textContent=b.dataset.title;invalidate();form.elements.project.focus();
        }else if(action==='preview'){
          proposal=await api('organization/groups/preview',Object.fromEntries(new FormData(form)));
          const rows=(items,excluded)=>items.map(u=>`<li><strong>${esc(u.title)}</strong> · ${esc(u.source_key)}${excluded?`<p>${esc(u.reason)}</p>`:''}<blockquote>${esc(u.quote)}</blockquote></li>`).join('');
          $('[data-rg-preview]').innerHTML=`<h4>将处理 ${proposal.eligible.length} 条；排除 ${proposal.excluded.length} 条</h4><p class="hint">逐条来源与匹配范围如下，每次最多处理 100 条。</p><details open><summary>符合条件</summary><ul>${rows(proposal.eligible,false)}</ul></details><details ${proposal.excluded.length?'open':''}><summary>例外与不能处理项</summary><ul>${rows(proposal.excluded,true)}</ul></details>`;
          $('[data-rg="apply"]').disabled=!proposal.eligible.length;notice('预览已就绪，核对后点击保存。');
        }else if(action==='apply'){
          const result=await api('organization/groups/apply',{proposal});
          notice(`已保存 ${result.succeeded} 条，失败 ${result.failed} 条${result.guidance?'；已保存一条后续指导':''}。`);
          proposal=null;form.hidden=true;onDirty(false);await load();await onSaved();
        }else{
          if(action==='previous')page=Math.max(1,page-1);if(action==='next')page++;
          await load();
        }
      }catch(error){notice(messages[error.message]||error.message);if(b.dataset.rg==='apply')invalidate();}
      finally{busy=false;onBusy(false);}
    });
    await load();
    const history=document.createElement('section');history.className='panel';mount.appendChild(history);
    history.innerHTML='<h2>仅留历史的进度</h2><p class="hint">纯助手进度保留原文，可在这里查找；已确认项目的资料也进入项目历史。未归属的资料保持未归属，可恢复处理。</p><form><label>查找进度原文<input name="query" maxlength="200"></label><button type="submit">查找</button></form><p data-history-status role="status"></p><div data-history-list></div><div class="actions"><button data-history-page="previous">上一页</button><span data-history-page-label></span><button data-history-page="next">下一页</button></div>';
    let historyPage=1, historyItems=[];
    async function historyLoad(){
      const result=await api('organization/progress?'+new URLSearchParams({query:history.querySelector('input').value,page:historyPage}));
      historyItems=result.items;
      history.querySelector('[data-history-list]').innerHTML=result.items.map((u,i)=>`<details class="organization-task"><summary>${esc(u.title)} · ${esc(u.project||'未归属')}</summary><p>${esc(u.disposition_reason)}</p><pre class="organization-text">${esc(u.text)}</pre><small>${esc(u.source_key)}</small> <button data-progress-restore="${i}">恢复处理</button></details>`).join('')||'<p class="hint">暂无匹配进度。</p>';
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
  }};
})();
