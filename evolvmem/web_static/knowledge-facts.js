/* Fact content decisions are separate from project archive decisions. */
(() => {
  window.EvolvKnowledgeFacts = {create({mount, api, esc, projects}) {
    let data=null, busy=false, sequence=0;
    const $=s=>mount.querySelector(s);
    const name=p=>projects.find(x=>x.project===p)?.display_name||p;
    const localTime=s=>s?new Date(s.replace(' ','T')+'Z').toLocaleString('zh-CN',{hour12:false}):'未注明时间';
    const condition=c=>Object.entries(c).map(([k,v])=>`${k}：${v}`).join('；')||'原文未限定条件';
    const errors={revision_conflict:'内容已更新，请刷新后再处理。',invalid_fact_conditions:'请为每个说法填写不同的适用情况。',invalid_fact_split:'这组已无法按该资料拆开，请刷新查看。',invalid_fact_date:'请选择有效日期。'};
    mount.innerHTML=`<div class="fact-heading"><h2>事实版本与知识关系</h2><button data-fact-action="refresh">刷新结果</button></div>
      <form class="fact-filters"><label>查找对象<input name="q" placeholder="对象名称或属性"></label><label>已归入项目<select name="project"><option value="">所有资料</option>${projects.filter(p=>p.status==='active').map(p=>`<option value="${esc(p.project)}">${esc(p.display_name||p.project)}</option>`).join('')}</select></label><label>查看当时的结论<input name="at" type="date"></label><label>条件名称<input name="conditionKey" placeholder="例如：环境"></label><label>条件内容<input name="conditionValue" placeholder="例如：Linux"></label><button>查找</button></form>
      <p data-fact-status role="status"></p><div data-fact-results></div>`;
    const status=message=>$('[data-fact-status]').textContent=message;
    function filters(){
      const f=new FormData($('.fact-filters')), out=new URLSearchParams();
      for(const k of ['q','project','at'])if(f.get(k))out.set(k,f.get(k));
      if(f.get('conditionKey')&&f.get('conditionValue'))out.set('conditions',JSON.stringify({[f.get('conditionKey')]:f.get('conditionValue')}));
      return out.toString();
    }
    function versionHtml(f,v){
      const pending=f.issues.find(i=>JSON.parse(i.members_json).includes(v.id));
      const sameSourceCount=new Set(v.sources.map(s=>`${s.source_key}:${s.start}:${s.end}`)).size;
      return `<article class="fact-version" data-fact-version="${v.id}"><div class="fact-heading"><h4>${esc(v.value)}</h4><span class="badge ${v.state==='confirmed'?'green':''}">${v.state==='confirmed'?'已确认内容':v.state==='previously_confirmed'?'以前确认的结论':'AI 整理'}</span></div>
        <p>${esc(condition(v.conditions))}</p><p>生效时间：${esc(v.valid_from||'未注明')}${v.valid_until?' 至 '+esc(v.valid_until):''} · 收录于 ${esc(localTime(v.recorded_at))}</p>
        <p>${sameSourceCount} 份原文 · ${v.verification_count} 次独立验证</p>
        <details><summary>查看原话与归属</summary>${v.sources.map(s=>`<blockquote><p><strong>${esc(s.adapter)} · ${esc(localTime(s.source_created_at))}</strong></p><p>${esc(s.quote)}</p><p>${esc(({user:'用户原话',tool:'工具原始结果',assistant:'助手陈述',unknown:'来源资料'})[s.role]||'来源资料')} · ${s.projects.length?'已归入 '+s.projects.map(p=>esc(name(p))).join('、'):'尚未归入项目'}</p></blockquote>`).join('')}</details>
        ${v.state!=='confirmed'?`<button class="write" data-fact-action="choose" data-group="${f.id}" data-version="${v.id}" ${pending?`data-issue="${pending.id}"`:''}>${pending?'采用这个结论':'确认此结论'}</button>`:''}</article>`;
    }
    function card(f){
      const sources=[...new Map(f.versions.flatMap(v=>v.sources).map(s=>[s.knowledge_id,s])).values()];
      const relationRows=[...new Map(f.relations.map(r=>[`${r.type}:${r.target}:${r.state}`,r])).values()];
      return `<section class="fact-card" data-fact-group="${f.id}"><div class="fact-heading"><h3>${esc(f.subject)} · ${esc(f.property)}</h3><span>${f.issues.length?'有分歧待处理':f.issue_history.some(i=>i.state==='deferred')?'分歧已暂缓':f.versions.length+' 个事实版本'}</span></div>
        ${f.current?`<p class="fact-current">${f.current.state!=='suggested'?'已确认结论':'当前整理结果'}：${esc(f.current.value)}</p>`:''}
        <div class="fact-versions">${f.versions.map(v=>versionHtml(f,v)).join('')}</div>
        ${f.issues.map(i=>`<section class="fact-issue" data-fact-issue="${i.id}"><h4>${i.kind==='change'?'发现新的生效版本':'同一条件下有不同说法'}</h4><div class="actions"><button class="write" data-fact-action="conditions" data-issue="${i.id}">补充各自适用情况</button><button class="write" data-fact-action="defer" data-issue="${i.id}">暂缓处理</button></div><form data-fact-conditions="${i.id}" hidden>${f.versions.filter(v=>JSON.parse(i.members_json).includes(v.id)).map(v=>`<label>${esc(v.value)}<input required data-condition-version="${v.id}" placeholder="这条结论适用于什么情况"></label>`).join('')}<button class="primary write">保存适用情况</button></form></section>`).join('')}
        ${relationRows.length?`<details><summary>知识关系 · ${relationRows.length} 项</summary><ul class="fact-relations">${relationRows.map(r=>`<li><strong>${esc(r.type)}</strong> → ${esc(name(r.target))} <span class="badge">${r.state==='confirmed'?'人工归属':'AI 建议'}</span></li>`).join('')}</ul></details>`:''}
        ${sources.length>1?`<details><summary>资料被放错组了？</summary>${sources.map((s,i)=>`<div class="fact-separate"><p>${i+1}. ${esc(s.quote)}</p><button class="write" data-fact-action="split" data-group="${f.id}" data-knowledge="${s.knowledge_id}">这份资料单独列出</button></div>`).join('')}</details>`:''}</section>`;
    }
    function draw(){
      $('[data-fact-results]').innerHTML=data.items.map(card).join('')||'<div class="empty">这个范围尚无整理出的事实。新增资料完成整理后会出现在这里。</div>';
      status(`${data.total} 组事实 · ${data.issues.length} 项分歧待处理`);
    }
    async function load(force=false){
      if(data&&!force)return;
      const request=++sequence;status('正在读取事实与关系…');
      try{const result=await api('facts?'+filters());if(request!==sequence)return;data=result;draw();}
      catch(e){if(request===sequence)status(errors[e.message]||e.message);}
    }
    async function save(route, body){
      if(busy)return;busy=true;
      mount.querySelectorAll('button').forEach(b=>b.disabled=true);
      try{await api('facts/'+route,body);await load(true);}
      catch(e){status(errors[e.message]||e.message);}
      finally{busy=false;mount.querySelectorAll('button').forEach(b=>b.disabled=false);}
    }
    mount.addEventListener('click',e=>{
      const b=e.target.closest('[data-fact-action]');if(!b||busy)return;
      const a=b.dataset.factAction,i=data?.issues.find(i=>i.id===b.dataset.issue),f=data?.items.find(f=>f.id===b.dataset.group);
      if(a==='refresh')return void load(true);
      if(a==='conditions'){$(`[data-fact-conditions="${i.id}"]`).hidden=false;return;}
      if(a==='defer')return void save('resolve',{id:i.id,expected_revision:i.revision,action:'defer'});
      if(a==='choose')return void save(i?'resolve':'confirm',i?{id:i.id,expected_revision:i.revision,action:'choose',version_id:b.dataset.version}:{id:f.id,expected_revision:f.revision,version_id:b.dataset.version});
      if(a==='split')return void save('split',{id:f.id,expected_revision:f.revision,knowledge_id:b.dataset.knowledge});
    });
    mount.addEventListener('submit',e=>{
      e.preventDefault();if(busy)return;
      if(e.target.matches('.fact-filters'))return void load(true);
      const iid=e.target.dataset.factConditions,i=data.issues.find(i=>i.id===iid),f=data.items.find(f=>f.id===i.group_id),conditions={};
      e.target.querySelectorAll('[data-condition-version]').forEach(input=>{const v=f.versions.find(v=>v.id===input.dataset.conditionVersion);conditions[v.id]={...v.conditions,'适用情况':input.value.trim()};});
      void save('resolve',{id:i.id,expected_revision:i.revision,action:'conditions',conditions});
    });
    return {load};
  }};
})();
