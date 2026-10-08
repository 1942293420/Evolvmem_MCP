/* One instruction, an explicit preview, then current matching units only. */
(() => {
  window.EvolvReviewGroups = {async render({mount, api, esc, projects, onSaved, onDirty, onBusy, historyMount=mount, onTask=()=>{}}) {
    let page=1, proposal=null, busy=false;
    const $=s=>mount.querySelector(s);
    const messages={review_preview_changed:'资料或规则已变化，请重新预览。',review_condition_required:'请填写具体、连续的适用短语，例如“达人建联 SOP”。',review_preview_required:'请先预览并核对本次范围。',invalid_project:'请选择项目。'};
    mount.innerHTML=`<section class="review-groups"><div class="section-head"><h2>集中审核</h2><button data-rg="refresh">刷新分组</button></div><p data-rg-status role="status"></p><div class="review-workspace"><div class="review-list-column"><div data-rg-list></div><div class="actions"><button data-rg="previous">上一页</button><span data-rg-page></span><button data-rg="next">下一页</button></div></div>
      <div class="review-editor"><div data-rg-empty><h3>选择一组需要判断的资料</h3></div><form data-rg-form hidden><h3 data-rg-title></h3><div class="organization-settings"><label>所属项目<select name="project"><option value="">选择项目</option>${projects.filter(p=>p.status==='active').map(p=>`<option value="${esc(p.project)}">${esc(p.display_name||p.project)}</option>`).join('')}</select></label><label>资料包含<input name="condition" maxlength="500" placeholder="例如：达人建联 SOP"></label><label>排除包含<input name="exceptions" maxlength="500" placeholder="多个词用 | 分开"></label><label>应用范围<select name="scope"><option value="batch">仅处理本次</option><option value="future">以后同类也这样处理</option></select></label></div><details><summary>补充指导</summary><input name="guidance" maxlength="1000" aria-label="补充指导" placeholder="可选"></details><input type="hidden" name="group_id"><button type="button" data-rg="preview">预览将处理的资料</button><div data-rg-preview></div><div class="review-apply-bar"><button type="button" class="primary write" data-rg="apply" disabled>保存符合条件的资料</button></div></form></div></div></section>`;
    function notice(text){$('[data-rg-status]').textContent=text;}
    function invalidate(){proposal=null;$('[data-rg="apply"]').disabled=true;$('[data-rg-preview]').textContent='条件已修改，请重新预览。';onDirty(true);}
    async function load(){
      const result=await api('organization/groups?'+new URLSearchParams({page}));
      $('[data-rg-page]').textContent=`${result.total} 组 · ${result.unit_count} 条 · 第 ${page} 页`;
      $('[data-rg="previous"]').disabled=page<=1;
      $('[data-rg="next"]').disabled=page*result.page_size>=result.total;
      $('[data-rg-list]').innerHTML=result.items.map(g=>`<details class="organization-task"><summary><strong>${esc(g.reason)}</strong> · ${g.count} 条<span class="review-group-title">${esc(g.examples[0]?.title||g.title)}</span></summary><div class="review-question"><span class="review-question-label">为什么需要你</span><strong>${g.reason==='价值待确认'?'需要决定是否保留为知识':g.reason==='归属有冲突'?'项目线索有冲突':'还不能确定所属项目'}</strong></div><details><summary>资料来源</summary><p>${esc(g.title)}</p></details><span class="review-example-label">关键原话</span>${g.examples.map(u=>`<blockquote>${esc(u.quote)}</blockquote><button data-rg="task" data-task="${u.task_id}">查看此条</button>`).join('')}<div class="review-suggestion"><span>当前处理</span><p>${g.reason==='价值待确认'?'暂不提炼，等待保留或暂存决定':'暂不归类，等待明确项目'}</p></div><button ${g.reason==='价值待确认'?'hidden':''} data-rg="choose" data-id="${esc(g.id)}" data-title="${esc(g.reason+' · '+g.count+' 条')}">为同类资料设置项目</button></details>`).join('')||'<div class="empty">没有需要你判断的资料</div>';

    }
    mount.addEventListener('input',e=>{if(e.target.closest('[data-rg-form]'))invalidate();});
    mount.addEventListener('change',e=>{if(e.target.closest('[data-rg-form]'))invalidate();});
    $('[data-rg-form]').onsubmit=e=>e.preventDefault();
    mount.addEventListener('click',async e=>{
      const b=e.target.closest('[data-rg]');if(!b)return;e.preventDefault();e.stopPropagation();if(busy)return;
      busy=true;onBusy(true);
      try{
        const action=b.dataset.rg, form=$('[data-rg-form]');
        if(action==='task'){await onTask(Number(b.dataset.task));}
        else if(action==='choose'){
          form.reset();form.hidden=false;$('[data-rg-empty]').hidden=true;form.elements.group_id.value=b.dataset.id;
          $('[data-rg-title]').textContent=b.dataset.title;invalidate();form.elements.project.focus();form.scrollIntoView({block:'nearest',behavior:'smooth'});
        }else if(action==='preview'){
          proposal=await api('organization/groups/preview',Object.fromEntries(new FormData(form)));
          const rows=(items,excluded)=>items.map(u=>`<li><strong>${esc(u.title)}</strong>${excluded?`<p>${esc(u.reason)}</p>`:''}<blockquote>${esc(u.quote)}</blockquote></li>`).join('');
          $('[data-rg-preview]').innerHTML=`<h4>将处理 ${proposal.eligible.length} 条；排除 ${proposal.excluded.length} 条</h4><p>保存后：符合项归入所选项目${proposal.scope==='future'?'，同类新资料沿用本次指导':''}。${proposal.remaining?`本次超出上限 ${proposal.remaining} 条，留待下一批。`:''}</p><details open><summary>符合条件</summary><ul>${rows(proposal.eligible,false)}</ul></details><details ${proposal.excluded.length?'open':''}><summary>例外与不能处理项</summary><ul>${rows(proposal.excluded,true)}</ul></details>`;
          $('[data-rg="apply"]').disabled=!proposal.eligible.length;notice('预览已就绪，核对后点击保存。');
        }else if(action==='apply'){
          const result=await api('organization/groups/apply',{proposal});
          notice(`已保存 ${result.succeeded} 条，失败 ${result.failed} 条${result.guidance?'；已保存一条后续指导':''}。`);
          proposal=null;form.hidden=true;$('[data-rg-empty]').hidden=false;onDirty(false);await load();await onSaved();
        }else{
          if(action==='previous')page=Math.max(1,page-1);if(action==='next')page++;
          await load();
        }
      }catch(error){notice(messages[error.message]||error.message);if(b.dataset.rg==='apply')invalidate();}
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
    return {refresh:load};
  }};
})();
