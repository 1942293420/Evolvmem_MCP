(function(){
  const PREFIX='/api/knowledge-console';
  let activeTrack='live';
  let selected=new Set();
  let inboxState={items:[],counts:{live:0,backfill:0},reason_codes:[],shadow_mode:true};

  function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  function getCsrf(){return document.cookie.match(/(?:^|; )evolvmem_csrf=([^;]+)/)?.[1]||window.__EVOLVMEM_CSRF__||'';}
  async function apiGet(path){
    const r=await fetch(PREFIX+path,{credentials:'same-origin'});
    const d=await r.json();
    if(!r.ok)throw new Error(d.error||'request_failed');
    return d;
  }
  async function apiPost(path,body){
    const r=await fetch(PREFIX+path,{
      method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(getCsrf())},
      body:JSON.stringify(body),
    });
    const d=await r.json();
    if(!r.ok)throw new Error(d.error||'request_failed');
    return d;
  }

  function viewEl(){return document.getElementById('review-inbox-view');}

  function render(){
    const root=viewEl();
    if(!root)return;
    const reasons=(inboxState.reason_codes||[]).map(r=>`<option value="${esc(r.code)}">${esc(r.label)} (${esc(r.code)})</option>`).join('');
    const shadowNote=inboxState.shadow_mode
      ?'<div class="review-shadow-banner">影子模式开启：提交决定前隐藏闸门判断</div>'
      :'';
    const cards=(inboxState.items||[]).map(item=>{
      const evHtml=(item.evidence_preview||[]).map(e=>`<div><b>[${esc(e.kind)}]</b> ${esc(e.ref)}: ${esc(e.quote)}</div>`).join('')||'<div>无预览证据</div>';
      const rescueBtn=(item.state==='rejected'||item.state==='quarantined')
        ?`<button type="button" class="btn btn-sm" data-act="rescue" data-uid="${esc(item.uid)}">救回 (rescue)</button>`:'';
      return `<article class="review-card" data-uid="${esc(item.uid)}">
        <div class="review-card-head">
          <label><input type="checkbox" class="rv-chk" data-uid="${esc(item.uid)}" ${selected.has(item.uid)?'checked':''}>
          <strong>${esc(item.category)}</strong> · <code>${esc(item.kind||'')}</code> · <span class="badge">${esc(item.state)}</span></label>
          <span class="mono">${esc(item.agent_client)} / ${esc(item.model)} · P${esc(item.importance)}</span>
        </div>
        <div><b>问题：</b>${esc(item.problem)}</div>
        <div><b>方案：</b>${esc(item.solution)}</div>
        <div><b>复用经验：</b>${esc(item.reusable_experience)}</div>
        ${item.misbelief?`<div><b>误区：</b>${esc(item.misbelief)}</div>`:''}
        <div class="review-evidence">${evHtml}</div>
        <div class="review-actions">
          <button type="button" class="btn btn-sm primary" data-act="accept" data-uid="${esc(item.uid)}">通过 (accept)</button>
          <button type="button" class="btn btn-sm" data-act="toggle-edit" data-uid="${esc(item.uid)}">编辑后通过 (edit_accept)</button>
          ${rescueBtn}
          <select class="review-reason-select" data-reason-for="${esc(item.uid)}">${reasons}</select>
          <button type="button" class="btn btn-sm danger" data-act="reject" data-uid="${esc(item.uid)}">拒绝 (reject)</button>
        </div>
        <div class="review-edit-box" data-edit-for="${esc(item.uid)}" hidden>
          <select data-field="category">
            <option ${item.category==='前端经验'?'selected':''}>前端经验</option>
            <option ${item.category==='业务经验'?'selected':''}>业务经验</option>
            <option ${item.category==='系统环境经验'?'selected':''}>系统环境经验</option>
          </select>
          <input data-field="problem" value="${esc(item.problem)}">
          <textarea data-field="solution" rows="2">${esc(item.solution)}</textarea>
          <textarea data-field="reusable_experience" rows="2">${esc(item.reusable_experience)}</textarea>
          <button type="button" class="btn btn-sm primary" data-act="submit-edit" data-uid="${esc(item.uid)}">保存并通过</button>
        </div>
      </article>`;
    }).join('')||'<p class="empty">当前页签暂无待审阅提议。</p>';

    root.innerHTML=`<div class="review-inbox-wrap">
      <div class="review-toolbar">
        <div class="review-tracks" role="tablist">
          <button type="button" class="review-track-btn" data-track="live" aria-selected="${activeTrack==='live'}">新会话 (${inboxState.counts?.live??0})</button>
          <button type="button" class="review-track-btn" data-track="backfill" aria-selected="${activeTrack==='backfill'}">旧聊天 (${inboxState.counts?.backfill??0})</button>
        </div>
        <div class="review-actions">
          <button type="button" class="btn btn-sm" id="rv-batch-accept">批量通过 (${selected.size})</button>
          <select id="rv-batch-reason" class="review-reason-select">${reasons}</select>
          <button type="button" class="btn btn-sm danger" id="rv-batch-reject">批量拒绝</button>
        </div>
      </div>
      ${shadowNote}
      <div id="rv-msg" class="muted"></div>
      ${cards}
    </div>`;
    bindEvents(root);
  }

  function bindEvents(root){
    root.querySelectorAll('[data-track]').forEach(btn=>btn.onclick=()=>{activeTrack=btn.dataset.track;selected.clear();loadInbox();});
    root.querySelectorAll('.rv-chk').forEach(chk=>chk.onchange=()=>{
      if(chk.checked)selected.add(chk.dataset.uid);else selected.delete(chk.dataset.uid);
      const b=document.getElementById('rv-batch-accept');if(b)b.textContent=`批量通过 (${selected.size})`;
    });
    root.querySelectorAll('[data-act]').forEach(btn=>btn.onclick=async()=>{
      const uid=btn.dataset.uid,act=btn.dataset.act;
      if(act==='toggle-edit'){
        const box=root.querySelector(`[data-edit-for="${uid}"]`);if(box)box.hidden=!box.hidden;return;
      }
      try{
        if(act==='accept'||act==='rescue'){
          await apiPost('/review-inbox/decide',{proposal_uid:uid,user_verdict:act});
        }else if(act==='reject'){
          const reason=root.querySelector(`[data-reason-for="${uid}"]`)?.value||'other';
          await apiPost('/review-inbox/decide',{proposal_uid:uid,user_verdict:'reject',user_reason:reason});
        }else if(act==='submit-edit'){
          const box=root.querySelector(`[data-edit-for="${uid}"]`);
          const edited_payload={
            category:box.querySelector('[data-field="category"]').value,
            problem:box.querySelector('[data-field="problem"]').value,
            solution:box.querySelector('[data-field="solution"]').value,
            reusable_experience:box.querySelector('[data-field="reusable_experience"]').value,
          };
          await apiPost('/review-inbox/decide',{proposal_uid:uid,user_verdict:'edit_accept',edited_payload});
        }
        selected.delete(uid);
        await loadInbox();
      }catch(e){const m=document.getElementById('rv-msg');if(m)m.textContent='操作失败: '+e.message;}
    });
    const bAcc=document.getElementById('rv-batch-accept');
    if(bAcc)bAcc.onclick=async()=>{
      if(!selected.size)return;
      try{
        await apiPost('/review-inbox/batch',{proposal_uids:[...selected],user_verdict:'accept'});
        selected.clear();await loadInbox();
      }catch(e){const m=document.getElementById('rv-msg');if(m)m.textContent='操作失败: '+e.message;}
    };
    const bRej=document.getElementById('rv-batch-reject');
    if(bRej)bRej.onclick=async()=>{
      if(!selected.size)return;
      try{
        const reason=document.getElementById('rv-batch-reason')?.value||'other';
        await apiPost('/review-inbox/batch',{proposal_uids:[...selected],user_verdict:'reject',user_reason:reason});
        selected.clear();await loadInbox();
      }catch(e){const m=document.getElementById('rv-msg');if(m)m.textContent='操作失败: '+e.message;}
    };
  }

  async function loadInbox(){
    try{
      inboxState=await apiGet('/review-inbox?source_track='+encodeURIComponent(activeTrack));
      const navCount=document.getElementById('nav-count-review');
      const total=(inboxState.counts?.live||0)+(inboxState.counts?.backfill||0);
      if(navCount){navCount.textContent=String(total);navCount.hidden=total===0;}
      render();
    }catch(e){
      const root=viewEl();
      if(root)root.innerHTML=`<p class="error">审阅箱加载失败：${esc(e.message)}</p>`;
    }
  }

  function syncRoute(){
    const root=viewEl(),consoleView=document.getElementById('console-view'),historyView=document.getElementById('history-view');
    const isInbox=location.hash==='#review-inbox';
    if(root)root.hidden=!isInbox;
    if(isInbox){
      if(consoleView)consoleView.hidden=true;
      if(historyView)historyView.hidden=true;
      document.querySelectorAll('[data-nav]').forEach(a=>{
        if(a.dataset.nav==='review-inbox')a.setAttribute('aria-current','page');
        else a.removeAttribute('aria-current');
      });
      loadInbox();
    }else if(consoleView&&(!historyView||historyView.hidden)){
      consoleView.hidden=false;
    }
  }

  window.addEventListener('hashchange',syncRoute);
  window.addEventListener('popstate',syncRoute);
  document.addEventListener('DOMContentLoaded',syncRoute);
})();