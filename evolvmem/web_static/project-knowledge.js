/* Same reading projection as knowledge_recall. Browsing never writes ownership. */
window.EvolvProjectKnowledge={create({mount,project,api,esc,projects=[]}){
  const cache=new Map(), originals=new Map();let data=null,selected='',sequence=0,dirty=false,loadedKey='',applied=new URLSearchParams({project,q:'',at:''});
  mount.className='project-knowledge';
  mount.innerHTML=`<div class="section-head"><h3>项目知识目录</h3><button data-pk-refresh>刷新知识</button></div>
    <form class="pk-filters"><label>查找知识<input name="q" type="search" placeholder="对象、规则或做法"></label><label>查看日期<input name="at" type="date"></label><label>适用条件<input name="conditionKey" placeholder="例如：环境"></label><label>条件值<input name="conditionValue" placeholder="例如：生产"></label><button type="submit">查找</button></form>
    <p data-pk-status role="status"></p><div class="pk-layout"><nav data-pk-list aria-label="知识目录"></nav><article data-pk-detail></article></div>`;
  const $=s=>mount.querySelector(s), form=$('form');
  const targetName=id=>projects.find(p=>p.project===id)?.display_name||id;
  const condition=v=>Object.entries(v||{}).map(([k,v])=>`${k}：${v}`).join('、')||'未注明适用条件';
  function source(s){if(!s.quote)return `<p>${esc(({session:'原始会话',organization:'整理任务',user:'用户录入',manual:'用户录入',file:'来源文件'})[s.source_kind]||'来源资料')}${s.created_at?' · '+esc(s.created_at.slice(0,10)):''}</p>`;return `<blockquote>${esc(s.quote||s.source_ref||'来源资料')}<p>${esc(s.adapter||s.source_kind||'资料')}${s.role?' · '+esc(({user:'用户原话',assistant:'助手记录',tool:'工具结果'})[s.role]||s.role):''}${s.source_created_at?' · '+esc(s.source_created_at.slice(0,10)):''}</p></blockquote>`;}
  function version(v){return `<section class="pk-version"><h4>${esc(v.value)}</h4><p>${esc(condition(v.conditions))}</p><p>${v.state==='confirmed'?'已确认的结论':v.state==='previously_confirmed'?'以前确认的结论':'资料中的说法'}${v.valid_from?' · 生效日期 '+esc(v.valid_from):''}${v.valid_until?' · 截止 '+esc(v.valid_until):''} · 独立验证 ${v.verification_count} 次</p>${v.sources.map(source).join('')}</section>`;}
  function details(){
    const item=data?.items.find(i=>i.id===selected),box=$('[data-pk-detail]');
    if(!item){box.innerHTML='<p>选择左侧知识查看内容与来源。</p>';return;}
    mount.querySelectorAll('[data-pk-select]').forEach(b=>b.setAttribute('aria-current',b.dataset.pkSelect===selected?'true':'false'));
    const old=item.versions.filter(v=>!item.current.some(c=>c.id===v.id));
    box.innerHTML=`<h3>${esc(item.title)}</h3>${item.warnings.map(w=>`<p class="pk-warning">${esc(w.message)}${w.state==='deferred'?'（已暂缓处理）':''}</p>`).join('')}
      ${item.kind==='fact'?`${item.body?`<pre class="project-memory-text">${esc(item.body)}</pre>`:''}<h4>当前内容</h4>${item.current.map(version).join('')||'<p>所查日期没有适用结论。</p>'}`:`<pre class="project-memory-text">${esc(item.body)}</pre>${item.trigger?`<p>适用：${esc(item.trigger)}</p>`:''}<h4>来源</h4>${item.sources.filter((s,n,a)=>!s.archive_id||a.findIndex(x=>x.archive_id===s.archive_id)===n).map(source).join('')}`}
      ${item.expressions?.length?`<details><summary>已确认的同义表达 · ${item.expressions.length} 种</summary>${item.expressions.map(x=>`<h4>${esc(x.title)}</h4><pre class="project-memory-text">${esc(x.text)}</pre>`).join('')}</details>`:''}
      ${old.length?`<details><summary>其他版本与变化 · ${old.length} 条</summary>${old.map(version).join('')}</details>`:''}
      ${item.alternatives.length?`<details class="pk-warning" open><summary>分歧资料 · 未归入本项目</summary>${item.alternatives.map(version).join('')}</details>`:''}
      ${item.relations.some(r=>r.type!=='属于项目')?`<h4>相关知识</h4><ul>${item.relations.filter(r=>r.type!=='属于项目').map(r=>`<li>${esc(r.type)} · ${esc(targetName(r.target))}${r.state==='suggested'?'（关联建议）':''}<blockquote>${esc(r.quote)}</blockquote></li>`).join('')}</ul>`:''}
      <div class="actions">${item.item_ids.map(id=>`<button data-pk-source="${id}">查看来源全文 #${id}</button>`).join('')}${item.warnings.length?'<a href="#knowledge/organization?tab=facts">处理事实与分歧</a>':''}</div><div data-pk-original></div>`;
  }
  function draw(){
    $('[data-pk-status]').textContent=`已归档知识 ${data.total} 条`;
    if(!data.items.some(i=>i.id===selected))selected=data.items[0]?.id||'';
    $('[data-pk-list]').innerHTML=data.sections.map(s=>`<section><h4>${esc(s.name)} · ${s.count}</h4>${data.items.filter(i=>i.section===s.name).map(i=>`<button data-pk-select="${esc(i.id)}"><strong>${esc(i.title)}</strong>${i.warnings.length?'<span>有待处理的变化或分歧</span>':''}</button>`).join('')}</section>`).join('')||'<p>没有符合条件的已归档知识。</p>';
    details();
  }
  async function load(force=false){
    const params=applied;
    const key=params.toString(),token=++sequence;
    if(dirty||force){cache.clear();originals.clear();dirty=false;}
    if(cache.has(key)){if(key===loadedKey&&data===cache.get(key))return;data=cache.get(key);loadedKey=key;draw();return;}
    $('[data-pk-status]').textContent='正在读取项目知识…';
    try{
      const result=await api('directory?'+key);cache.set(key,result);
      if(token!==sequence)return;
      data=result;loadedKey=key;draw();
    }catch(error){if(token===sequence)$('[data-pk-status]').textContent='读取失败：'+error.message;}
  }
  form.onsubmit=e=>{e.preventDefault();applied=new URLSearchParams({project,q:form.elements.q.value,at:form.elements.at.value});if(form.elements.conditionKey.value.trim()&&form.elements.conditionValue.value.trim())applied.set('conditions',JSON.stringify({[form.elements.conditionKey.value.trim()]:form.elements.conditionValue.value.trim()}));load();};
  mount.addEventListener('click',async e=>{
    const b=e.target.closest('[data-pk-select],[data-pk-refresh],[data-pk-source]');if(!b)return;
    if(b.hasAttribute('data-pk-refresh')){await load(true);return;}
    if(b.dataset.pkSelect){selected=b.dataset.pkSelect;details();return;}
    const key=b.dataset.pkSource,box=$('[data-pk-original]');
    if(originals.has(key)){box.replaceChildren(originals.get(key));return;}
    const node=document.createElement('div');originals.set(key,node);box.replaceChildren(node);
    try{await EvolvTaskContext.render({mount:node,api,esc,query:'items/'+key+'/context'});}catch(error){node.textContent=error.message;originals.delete(key);}
  });
  return {node:mount,load,invalidate(){dirty=true;sequence++;}};
}};
