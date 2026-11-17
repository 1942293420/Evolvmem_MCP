/* Learning stays inside the knowledge workbench. */
window.EvolvLearning = {
  async render({mount,api,esc,toast,detail,projects,onDirty,onBusy,selectedProject='__all__'}) {
    const overview=await api('learning');
    let project=selectedProject, family=overview.families.find(f=>f.project===project)?.family||'', dirty=false, busy=false;
    const categories=overview.categories, edited=new Set();let filterState='';
    const options=projects.filter(p=>p.status==='active').map(p=>{const name=p.display_name||p.project;return `<option value="${esc(p.project)}">${name===p.project?esc(name):`${esc(name)}（${esc(p.project)}）`}</option>`;}).join('');
    mount.innerHTML=`<p class="intro">从已入库记忆学习协作规则。这里确认的是 AI 今后怎样与你协作；资料入库在“待确认”中处理。</p>
      <div class="stats"><div class="stat"><span>当前可用规则</span><strong>${overview.counts.active}<small>按项目与类型调用</small></strong></div><div class="stat"><span>规则待确认</span><strong>${overview.counts.candidate}<small>推断、归纳或冲突</small></strong></div><div class="stat"><span>来源发生变化</span><strong>${overview.counts.stale}<small>已停止调用，待修订</small></strong></div></div>
      <section class="panel learning-controls"><h2>从项目记忆完善体系</h2><div class="toolbar"><select id="learning-project" aria-label="分析项目"><option value="__all__">所有学习成果</option><option value="">全局习惯</option>${options}</select><input id="learning-family" aria-label="项目类型" placeholder="项目类型，例如：内部工具" value="${esc(family)}"><button class="write" data-learning-action="family">保存项目类型</button><button class="primary write" data-learning-action="analyze">分析当前项目记忆</button><button class="write" data-learning-action="analyze-family">分析同类项目</button></div><p class="hint">同类项目由你设置类型。新对话会继续提炼；累计三份新记忆后自动综合分析。历史记忆可以在这里主动分析。</p><div id="learning-result" role="status"></div></section>
      <div class="section-head"><h2>学习成果与依据</h2><select id="learning-state" aria-label="规则状态"><option value="">全部成果</option><option value="active">当前生效</option><option value="candidate">规则待确认</option><option value="stale">来源已变化</option><option value="rejected">已否决</option></select></div><div id="learning-rules"></div>
      <section class="panel learning-framework"><div class="section-head"><div><h2>我的协作 Skill</h2><p class="hint">框架由你编辑，下面的已学习规则按适用范围加入。当前版本 ${overview.revision}。</p></div><button data-learning-action="preview">查看当前项目完整 Skill</button></div><label for="learning-framework">协作框架</label><textarea id="learning-framework" class="code" spellcheck="false">${esc(overview.framework)}</textarea><div class="actions"><button class="primary write" data-learning-action="save">保存协作框架</button><button data-learning-action="download">下载适用 Skill</button></div><div class="toolbar"><select id="learning-version" aria-label="历史版本">${overview.versions.map(v=>`<option value="${v.id}">版本 ${v.id} · ${esc(v.reason)} · ${esc(v.created_at)}</option>`).join('')}</select><button data-learning-action="compare">查看版本变化</button><button class="write" data-learning-action="restore" ${overview.versions.length?'':'disabled'}>恢复该版本</button></div><p class="hint">恢复会产生新版本；已被纠正或归档的来源不会因恢复旧版本重新生效。</p><pre id="learning-preview" hidden></pre></section>
      <section class="panel"><h2>最近分析</h2>${overview.runs.length?overview.runs.map(r=>`<p>${esc(r.created_at)} · ${esc(r.target||'全局')} · ${({complete:'已完成',failed:'待重试'})[r.status]||esc(r.status)} ${esc(r.reason)}</p>`).join(''):'<p class="hint">尚无综合分析记录。新对话提炼和规则确认会在上方留下成果。</p>'}</section>`;
    const $=s=>mount.querySelector(s);
    const setDirty=value=>{dirty=value;onDirty(value);};
    const refresh=()=>window.EvolvLearning.render({mount,api,esc,toast,detail,projects,onDirty,onBusy,selectedProject:project});
    function draw(){
      const state=$('#learning-state').value;
      const rows=overview.rules.filter(r=>(project==='__all__'||r.scope==='global'||(project&&r.target===project)||(family&&r.scope==='family'&&r.target===family))&&(!state||(state==='active'?r.effective:state==='stale'?r.status==='active'&&!r.effective:r.status===state)));
      $('#learning-rules').innerHTML=rows.length?rows.map(r=>`<article class="panel learning-rule" data-rule="${r.id}"><div class="section-head"><h3>#${r.id} · ${esc(r.topic)}</h3><span class="badge ${r.effective?'green':'amber'}">${r.effective?'已生效':({candidate:'规则待确认',rejected:'已否决',superseded:'已替代'})[r.status]||'来源已变化'}</span></div><p class="hint">范围：${esc(({global:'全局',project:'项目',family:'项目类型'})[r.scope])} ${esc(r.target)} · ${esc(r.effective_reason)}</p><textarea data-rule-text="${r.id}" aria-label="规则 ${r.id} 内容">${esc(r.instruction)}</textarea><p>适用：${esc(r.trigger||'遵循来源范围')}<br>理由：${esc(r.rationale||'见来源')}<br>例外：${esc(r.exceptions||'无额外声明')}</p><details><summary>为什么这样理解 · ${r.source_details.length} 份来源</summary>${r.source_details.map(s=>`<div class="learning-source"><button data-learning-action="source" data-id="${s.id}">#${s.id} ${esc(s.title)}</button><p class="hint">${esc(categories[s.learning.category]||s.learning.category)} · ${esc(s.learning.basis==='explicit'?'用户明确表达':s.learning.basis==='manual'?'用户已纠正':'待核对的提炼')}</p>${s.learning.evidence.map(e=>`<blockquote>${esc(e.quote)}</blockquote><small>${esc(e.session||'')} · 消息 ${e.message_index+1}${e.archive_id?' · 归档 #'+e.archive_id:''}</small>`).join('')||'<p class="hint">旧资料没有逐句引用，请打开来源核对。</p>'}${EvolvExtraction.process(s.learning,esc)}</div>`).join('')}</details><div class="actions"><button class="primary write" data-learning-action="accept" data-id="${r.id}">${r.status==='active'?'保存纠正并应用':'确认并应用'}</button><button class="write" data-learning-action="reject" data-id="${r.id}">不采用</button></div></article>`).join(''):'<div class="empty">这个范围还没有学习成果。可以先分析已有记忆，也可以在日常对话中逐步积累。</div>';
    }
    $('#learning-project').value=project;draw();
    mount.oninput=e=>{if(e.target.id==='learning-framework'||e.target.dataset.ruleText){edited.add(e.target.id||e.target.dataset.ruleText);setDirty(true);}};
    mount.onchange=e=>{
      if(!['learning-project','learning-state'].includes(e.target.id))return;
      if([...edited].some(key=>key!=='learning-framework')){
        if(!confirm('有未保存的规则编辑，切换会丢弃这些编辑。是否继续？')){e.target.value=e.target.id==='learning-project'?project:filterState;return;}
        for(const key of edited)if(key!=='learning-framework')edited.delete(key);setDirty(edited.size>0);
      }
      if(e.target.id==='learning-project'){project=e.target.value;family=overview.families.find(f=>f.project===project)?.family||'';$('#learning-family').value=family;}
      filterState=$('#learning-state').value;draw();
    };
    mount.onclick=async e=>{
      const button=e.target.closest('[data-learning-action]');if(!button||busy)return;
      e.preventDefault();e.stopPropagation();const action=button.dataset.learningAction;
      busy=true;onBusy(true);button.disabled=true;
      try{
        if(action==='compare'){
          const old=await api('learning/versions/'+$('#learning-version').value);
          const describe=r=>`${r.scope}:${r.target||'通用'} · ${r.instruction}`;
          const current=new Set(overview.rules.filter(r=>r.status==='active').map(describe));const previous=new Set(old.rules.filter(r=>r.status==='active').map(describe));
          $('#learning-preview').hidden=false;$('#learning-preview').textContent=`与版本 ${old.id} 比较\n新增或修改：\n${[...current].filter(r=>!previous.has(r)).join('\n')||'无'}\n移除或替代：\n${[...previous].filter(r=>!current.has(r)).join('\n')||'无'}\n框架：${old.framework===overview.framework?'一致':'已修改，以下为历史框架'}\n${old.framework===overview.framework?'':old.framework}`;return;
        }
        if(action==='source'){await detail(Number(button.dataset.id));return;}
        if(action==='preview'||action==='download'){
          if(project==='__all__')throw Error('请选择要查看的项目或全局习惯。');
          const r=await api('learning/skill?'+new URLSearchParams({project}));
          if(action==='preview'){$('#learning-preview').hidden=false;$('#learning-preview').textContent=r.skill;}
          else{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([r.skill],{type:'text/markdown;charset=utf-8'}));a.download='SKILL.md';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}return;
        }
        if(['analyze','analyze-family','restore','family'].includes(action)&&dirty&&!confirm('有未保存的编辑，继续将重新加载内容。是否继续？'))return;
        if(['save','accept','reject'].includes(action)){
          const saving=action==='save'?'learning-framework':button.dataset.id;
          if([...edited].some(key=>key!==saving)&&!confirm('还有其他未保存的编辑，本次保存后会重新加载。是否继续？'))return;
        }
        if(action==='family'){if(!project||project==='__all__')throw Error('先选择一个项目。');await api('learning/family',{project,family:$('#learning-family').value.trim()});}
        if(action==='analyze'||action==='analyze-family'){
          if(project==='__all__')throw Error('请选择要分析的项目或全局习惯。');const group=$('#learning-family').value.trim();if(action==='analyze-family'&&!group)throw Error('先为项目设置类型。');
          $('#learning-result').textContent='正在读取已有记忆并分析，请稍候…';
          const r=await api('learning/analyze',action==='analyze-family'?{family:group}:{project});
          if(r.status==='failed')throw Error(r.reason);
          toast(r.status==='empty'?r.reason:`分析完成，得到 ${r.rules.length} 条建议，可查看来源后确认。`);
        }
        if(action==='save')await api('learning/framework',{expected_revision:overview.revision,framework:$('#learning-framework').value});
        if(action==='restore'){if(!confirm('恢复所选版本的框架和规则？当前内容仍保留在版本记录中。'))return;await api('learning/restore',{expected_revision:overview.revision,version_id:Number($('#learning-version').value)});}
        if(action==='accept'||action==='reject'){
          const r=overview.rules.find(x=>x.id===Number(button.dataset.id));
          if(action==='accept'&&r.conflicting_ids.length&&!confirm(`将替换同主题规则 ${r.conflicting_ids.map(id=>'#'+id).join('、')}，确认采用当前规则？`))return;
          await api('learning/rules/'+r.id,{replace_conflicts:action==='accept'&&r.conflicting_ids.length>0,expected_revision:r.revision,action,instruction:mount.querySelector(`[data-rule-text="${r.id}"]`).value});
        }
        setDirty(false);await refresh();if(!action.startsWith('analyze'))toast('已保存，后续调用读取当前生效规则。');
      }catch(error){toast(error.message);if($('#learning-result'))$('#learning-result').textContent=error.message;}finally{busy=false;onBusy(false);button.disabled=false;}
    };
  }
};
