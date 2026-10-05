/* Stage-specific editing backed by the same policy consumed by ingestion. */
window.EvolvPipeline={async render({mount,api,esc,toast,onDirty,onBusy}) {
 const catalog=await api('skills');let active='extraction',dirty=false,busy=false;
 const allProjects=api('projects').then(r=>r.projects).catch(e=>{toast(e.message);return[];});
 const projectLabel=async id=>{if(!id)return'';const name=(await allProjects).find(p=>p.project===id)?.display_name;return name&&name!==id?`${name}（${id}）`:id;};
 const $=s=>mount.querySelector(s),names={project_alias_matching:'按正文中的项目名称与别名匹配',ambiguous_project_names:'不参与匹配的通用词 · 每行一个',cleaning_drop_lines:'额外去除的固定提示行 · 完全匹配，每行一个',cleaning_collapse_duplicates:'合并相邻且内容相同的同角色消息',related_memory_projects:'允许对照旧知识的项目标识 · 每行一个',auto_min_confidence:'自动入库最低置信度',require_source:'要求可核对来源',min_chars:'正文最少字数',max_chars:'正文最多字数',ignore_keywords:'不入库关键词 · 每行一个',auto_explicit_rules:'明确且无冲突的协作规则自动更新'};
 const trials={
  ownership:{sample:'material',projectEmpty:'自动判断',projectHint:' · 可留空判断',
   buttons:'<button data-pipeline-action="judge-preview">预览归属判断</button>',
   hint:'本地预览使用草稿条件，不写资料；验证使用已保存规则，把样例与判断原因放入待确认。'},
  cleaning:{sample:'dialogue',projectEmpty:'请选择项目',
   buttons:'<button data-pipeline-action="clean-preview">本地去噪预览</button><button class="write" data-pipeline-action="model-preview">对比需求与问答提炼</button>',
   hint:'本地去噪不调用模型。点击“对比”才发送此处样例给已配置模型（最多 2 次）；默认不发送旧记忆，不保存资料。验证把清洗结果放入待确认。'},
  extraction:{sample:'dialogue',projectEmpty:'请选择项目',
   buttons:'<button class="write" data-pipeline-action="model-preview">对比需求与问答提炼</button>',
   hint:'“对比”发送样例给已配置模型（最多 2 次），不写资料；验证用已保存规则提炼 1 次，并把提炼结果放入待确认。'},
  ingestion:{sample:'material',projectEmpty:'自动判断',projectHint:' · 可留空判断',
   buttons:'<button data-pipeline-action="judge-preview">预览入库判断</button>',
   hint:'本地预览使用草稿条件；验证按已保存阈值判断样例，连同判定原因放入待确认。'},
  collaboration:{sample:'',projectEmpty:'全局习惯',projectHint:' · 可留空分析全局习惯',
   buttons:'',
   hint:'保存框架后，验证分析所选范围已有记忆（调用已配置模型），建议规则进入协作学习成果待确认。'}};
 const setDirty=v=>{dirty=v;onDirty(v);if($('#stage-state'))$('#stage-state').textContent=v?'有未保存的修改':'已保存';};
 mount.innerHTML=`<p class="intro">项目整理 Skill 已移至 <a href="#knowledge/unassigned" data-view="unassigned">项目历史 → 待入库项目</a>。清洗 Skill 在 <a href="#knowledge/cleaning" data-view="cleaning">数据清洗</a> 中维护。这里保留提炼、入库与协作 3 个环节。</p><div class="pipeline-catalog">${catalog.skills.filter(s=>!['ownership','cleaning'].includes(s.id)).map((s,i)=>`<button data-pipeline-stage="${s.id}"><span>0${i+1}</span><strong>${esc(s.title)}</strong><small>${esc(s.description)}</small></button>`).join('')}</div><div id="stage-editor"></div><details class="panel"><summary>高级管理与旧版入口</summary><p>各环节共享同一份入库规则，协作框架沿用原有版本管理；任一环节保存后，skills/evolvmem-ownership、evolvmem-cleaning、evolvmem-extraction、evolvmem-ingestion、evolvmem-collaboration 五个独立文件同步刷新，可链接到客户端 skills 目录供 AI 分别发现。</p><a href="#knowledge/rules" data-view="rules">完整入库条件与模型对比</a> · <a href="#knowledge/learning" data-view="learning">协作成果与版本恢复</a><div id="stage-full-skill"></div></details>`;
 function controls(s){return Object.entries(s.settings).map(([key,value])=>`<div class="field">${typeof value==='boolean'?`<label class="extraction-choice"><input type="checkbox" data-stage-setting="${key}" ${value?'checked':''}>${esc(names[key])}</label>`:`<label for="stage-${key}">${esc(names[key])}</label>${Array.isArray(value)?`<textarea id="stage-${key}" data-stage-setting="${key}">${esc(value.join('\n'))}</textarea>`:`<input id="stage-${key}" type="number" step="${key==='auto_min_confidence'?'0.05':'1'}"${key==='auto_min_confidence'?' min="0" max="1"':''} data-stage-setting="${key}" value="${value}">${key==='auto_min_confidence'?'<small>0 到 1 之间</small>':''}`}`}</div>`).join('');}
 function stage(){return catalog.skills.find(s=>s.id===active);}
 function instructionsHint(s){if(s.id==='collaboration')return'协作说明供 AI 处理时参考；请保留 --- 之间的 name 与 description 文件头，只修改正文。';return `自然语言说明供 AI 处理时参考${Object.keys(s.settings).length?'，下方条件由程序执行':''}。原话依据、来源检查与疑难待确认仍然保留。`;}
 function payload(){const s=stage(),settings={};mount.querySelectorAll('[data-stage-setting]').forEach(e=>{const k=e.dataset.stageSetting,v=s.settings[k];settings[k]=typeof v==='boolean'?e.checked:Array.isArray(v)?e.value.split('\n').map(x=>x.trim()).filter(Boolean):Number(e.value);});return {expected_revision:s.revision,instructions:$('#stage-instructions').value,settings};}
 function messages(){const result=[];for(const line of $('#stage-sample').value.split('\n')){const m=line.match(/^(用户|助手|工具)[：:]\s*(.*)$/);if(m)result.push({role:({用户:'user',助手:'assistant',工具:'tool'})[m[1]],content:m[2]});else if(result.length)result.at(-1).content+='\n'+line;else if(line.trim())throw Error('请以“用户：”“助手：”或“工具：”标明角色。');}return result;}
 function draw(){const s=stage(),trial=trials[active];mount.querySelectorAll('[data-pipeline-stage]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.pipelineStage===active)));
  $('#stage-editor').innerHTML=`<section class="panel"><div class="section-head"><div><h2>${esc(s.title)} Skill</h2><p class="hint">${esc(s.execution)} · 版本 ${esc(String(s.revision).slice(0,12))}</p></div><div class="actions"><span id="stage-state" role="status">已保存</span><button data-pipeline-action="download">下载已保存 Skill</button><button class="primary write" data-pipeline-action="save">保存本环节</button></div></div><div class="field"><label for="stage-instructions">AI 执行说明</label><textarea id="stage-instructions" class="stage-instructions">${esc(s.instructions)}</textarea><small>${instructionsHint(s)}</small></div><div class="form-grid">${controls(s)}</div>${active==='cleaning'?'<p class="rule-note">原话正文只做去噪；需求表达是引用原话的派生知识。保存影响新对话与后续提炼，已入库历史不会静默改写。</p>':''}
  <details class="skill-trial" open><summary>用样例检查效果与验证</summary>${trial.sample?`<div class="field"><label for="stage-sample">样例${trial.sample==='material'?'资料':'对话'}</label><textarea id="stage-sample">${trial.sample==='material'?'Evo 项目需要在经验问答里新增项目。':'用户：我想在经验里直接加项目，不要让我到处找。\n工具：这段工具记录应移除。\n助手：会把新增项目入口放在问答编辑处。'}</textarea></div>`:''}<div class="field"><label for="stage-project">已确认项目${trial.projectHint||''}</label><select id="stage-project"><option value="">${trial.projectEmpty}</option></select></div><div class="actions">${trial.buttons}<button class="write" data-pipeline-action="verify">${active==='collaboration'?'保存框架并分析验证':'保存并放入待确认验证'}</button></div><p class="hint">${trial.hint}</p><div id="stage-result" role="status"></div></details></section>`;
  if($('#stage-project'))allProjects.then(rows=>{if($('#stage-project'))rows.filter(p=>p.status==='active').forEach(p=>{const label=p.display_name&&p.display_name!==p.project?`${p.display_name}（${p.project}）`:p.project;$('#stage-project').add(new Option(label,p.project));});});
 }
 async function fullSkill(){const r=await api('rules');$('#stage-full-skill').innerHTML=`<details><summary>完整管理 SKILL.md</summary><textarea id="skill-text" class="code skill-editor" aria-label="管理 Skill 内容">${esc(r.skill)}</textarea><div class="actions"><button data-pipeline-action="full-download">下载文件</button><button class="write" data-pipeline-action="full-save">保存完整 Skill</button></div></details>`;$('#stage-full-skill').dataset.revision=r.revision;}
 const download=(text,name)=>{const url=URL.createObjectURL(new Blob([text],{type:'text/markdown;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
 mount.oninput=e=>{if(['stage-instructions','skill-text'].includes(e.target.id)||e.target.hasAttribute('data-stage-setting'))setDirty(true);};mount.onchange=e=>{if(e.target.hasAttribute('data-stage-setting'))setDirty(true);};
 mount.onclick=async e=>{const tab=e.target.closest('[data-pipeline-stage]'),b=e.target.closest('[data-pipeline-action]');if((!tab&&!b)||busy)return;e.preventDefault();
  if(tab){if(tab.dataset.pipelineStage===active)return;if(dirty&&!confirm('有未保存的修改，放弃并切换 Skill？'))return;active=tab.dataset.pipelineStage;setDirty(false);draw();await fullSkill();return;}
  busy=true;b.disabled=true;onBusy(true);
  try{const action=b.dataset.pipelineAction;
   if(action==='save'){const saved=await api('skills/'+active,payload());catalog.skills=(await api('skills')).skills;setDirty(false);draw();await fullSkill();toast(saved.title+' Skill 已保存，后续处理读取新版本');}
   else if(action==='download')download(stage().skill,'SKILL.md');
   else if(action==='full-download')download($('#skill-text').value,'SKILL.md');
   else if(action==='full-save'){await api('rules',{expected_revision:$('#stage-full-skill').dataset.revision,skill:$('#skill-text').value});catalog.skills=(await api('skills')).skills;setDirty(false);draw();await fullSkill();toast('完整管理 Skill 已保存');}
   else if(action==='clean-preview'){const r=await api('skills/cleaning/preview',{...payload(),messages:messages()});$('#stage-result').innerHTML=`<h3>去噪结果</h3><p>${r.input_messages} 轮 → ${r.dialogue_messages} 轮；模型调用 0 次，入库 0 条。</p><pre class="project-memory-text">${esc(r.text)}</pre>`;}
   else if(action==='verify'){
    let saved=false;
    if(dirty){await api('skills/'+active,payload());catalog.skills=(await api('skills')).skills;setDirty(false);saved=true;}
    const kind=trials[active].sample;let body;
    if(kind==='material')body={body:$('#stage-sample').value,project:$('#stage-project').value};
    else if(kind==='dialogue'){body={messages:messages(),project:$('#stage-project').value};if(active==='extraction'&&!body.project)throw Error('请先选择样例所属项目。');}
    else body={project:$('#stage-project').value};
    if($('#stage-result'))$('#stage-result').textContent='正在按已保存规则处理…';
    const r=await api('skills/'+active+'/verify',body);
    if(saved){draw();await fullSkill();}
    const link=active==='collaboration'?'<a href="#knowledge/learning" data-view="learning">到「协作学习成果」确认</a>':'<a href="#knowledge/intake" data-view="intake">到「资料待确认」确认成果</a>';
    $('#stage-result').innerHTML=active==='collaboration'
      ?(r.created?`分析完成，新增 ${r.created} 条待确认规则。${link}`:esc(r.reason||(r.status==='complete'?'分析完成，没有新增待确认规则。':'该范围暂时没有可分析的记忆。')))
      :`已按保存的「${esc(stage().title)}」Skill 处理，放入待确认 ${r.created} 条。${link}`;
   }
   else {const current=await api('rules'),draft=payload();if(current.revision!==draft.expected_revision)throw Error('规则已变化，请重新打开本环节。');const field={ownership:'ownership_instructions',cleaning:'cleaning_instructions',extraction:'extraction_instructions'}[active];const rules={expected_revision:current.revision,settings:{...current.settings,...draft.settings,...(field?{[field]:draft.instructions}:{})}};
    if(action==='judge-preview'){const r=await api('preview',{body:$('#stage-sample').value,project:$('#stage-project').value,source:'Skill 样例',rules});$('#stage-result').innerHTML=`<h3>${esc(({auto:'明确，可入库',review:'待确认',ignore:'不入库'})[r.action])}</h3><p>项目：${esc(r.project?await projectLabel(r.project):'未确定')} · ${esc(r.reason)}</p>`;}
    else {if(!$('#stage-project').value)throw Error('请先选择样例所属项目。');$('#stage-result').textContent='正在比较提炼结果…';const r=await api('extraction/preview',{project:$('#stage-project').value,messages:messages(),rules});$('#stage-result').innerHTML=`<p>模型调用 ${r.model_calls} 次；入库 0 条。草稿只用于本次对比，保存后才正式生效。</p><div class="extraction-comparison">${[['已保存规则',r.current],['当前草稿',r.draft]].map(([title,v])=>`<section><h3>${title}</h3><details><summary>去噪后的对话</summary><pre class="project-memory-text">${esc(v.cleaned_messages.map(m=>(m.role==='user'?'用户：':'AI：')+m.content).join('\n\n'))}</pre></details>${v.candidates.map(c=>`<article class="extraction-candidate"><span class="badge">${c.status==='active'?'自动入库':'待确认'}</span><p>${esc(c.body)}</p><p class="hint">${esc(c.reason)}</p>${EvolvExtraction.process(c,esc)}</article>`).join('')||'<p>未提炼出需要保存的知识。</p>'}</section>`).join('')}</div>`;}
   }
  }catch(error){toast(error.message);if($('#stage-result'))$('#stage-result').textContent=error.message;}finally{busy=false;b.disabled=false;onBusy(false);}
 };draw();await fullSkill();
}};
