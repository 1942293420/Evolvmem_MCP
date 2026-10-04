/* The rule editor and the ingestion adapters use the same extraction contract. */
window.EvolvExtraction = {
  settings(root, previous) {
    return {...previous,
      extraction_instructions:root.querySelector('#extraction-instructions').value,
      auto_explicit_rules:root.querySelector('#extraction-auto').checked,
      related_memory_projects:[...root.querySelectorAll('[data-related-project]:checked')].map(e=>e.value)};
  },
  process(learning, esc) {
    const names={goal:'用户目标',understanding:'AI 的理解',correction:'用户纠正',decision:'明确决定',verification:'验证陈述'};
    const entries=Object.entries(learning?.process||{});
    if(!entries.length)return '';
    const relation=learning.relation;
    return `<details class="extraction-process"><summary>查看协作过程${relation?.target_id?' · 关联资料 #'+Number(relation.target_id):''}</summary>${entries.map(([stage,e])=>`<div><strong>${esc(names[stage]||stage)}</strong><blockquote>${esc(e.quote)}</blockquote><small>${esc(({user:'用户',assistant:'助手',tool:'工具'})[e.role]||e.role)} · 消息 ${Number(e.message_index)+1}${stage==='verification'?' · 原始陈述，不直接计作成功':''}</small></div>`).join('')}${learning.process_errors?.length?'<p class="hint">部分过程无法核对，已留待确认。</p>':''}</details>`;
  },
  mount({root,api,esc,projects,policy,getDraft,onDirty,onBusy}) {
    const section=document.createElement('section');section.className='panel extraction-panel';root.append(section);
    const active=projects.filter(p=>p.status==='active');
    section.innerHTML=`<h2>对话提炼与旧知识对照</h2><p>编辑如何保留目标、理解、纠正、决定和验证。明确规则自动更新，疑难变化留待确认。</p>
      <label for="extraction-instructions">提炼说明</label><textarea id="extraction-instructions" class="code">${esc(policy.settings.extraction_instructions)}</textarea>
      <label class="extraction-choice"><input type="checkbox" id="extraction-auto" ${policy.settings.auto_explicit_rules?'checked':''}>明确、有原话且无冲突的协作规则自动生效</label>
      <details><summary>后续提炼时对照哪些项目的旧知识</summary><p class="hint">勾选并保存后，该项目后续提炼会将最多 20 条相关旧知识（总计最多 8,000 字）与对话一起交给当前配置模型。未勾选时仅处理新对话。</p><div class="extraction-projects">${active.map(p=>`<label class="extraction-choice"><input type="checkbox" data-related-project value="${esc(p.project)}" ${policy.settings.related_memory_projects.includes(p.project)?'checked':''}>${esc(p.display_name||p.project)}</label>`).join('')}</div></details>
      <div class="actions"><button class="primary write" data-action="rules-save">保存提炼与入库规则</button></div>
      <div class="divider"></div><h3>用一段对话比较修改前后</h3><label for="extraction-project">样例所属项目</label><select id="extraction-project"><option value="">请选择项目</option>${active.map(p=>`<option value="${esc(p.project)}">${esc(p.display_name||p.project)}</option>`).join('')}</select>
      <label for="extraction-sample">样例对话（每轮以“用户：”“助手：”或“工具：”开头）</label><textarea id="extraction-sample">用户：以后，修改界面时先明确操作目标和验收条件。
助手：我会新建一个独立管理页面。
用户：管理保留在原知识库，不另建页面。</textarea>
      <label class="extraction-choice"><input type="checkbox" id="extraction-related">本次预览同时对照所选项目旧知识</label>
      <p class="hint">预览会将样例及勾选的相关旧知识发给当前配置模型，最多调用两次。比较已保存规则与当前草稿；不保存规则，不写入资料。</p>
      <button class="write" id="extraction-preview-button">调用模型，比较提炼结果</button><div id="extraction-result" role="status"></div>`;
    section.addEventListener('input',e=>{if(e.target.id==='extraction-instructions'||e.target.id==='extraction-auto'||e.target.hasAttribute('data-related-project'))onDirty();});
    const button=section.querySelector('#extraction-preview-button');
    button.onclick=async()=>{
      if(button.disabled)return;
      const output=section.querySelector('#extraction-result');
      try{
        const project=section.querySelector('#extraction-project').value;
        if(!project)throw Error('请先选择样例所属项目。');
        const messages=[];
        for(const line of section.querySelector('#extraction-sample').value.split('\n')){
          const match=line.match(/^(用户|助手|工具)[：:]\s*(.*)$/);
          if(match)messages.push({role:({用户:'user',助手:'assistant',工具:'tool'})[match[1]],content:match[2]});
          else if(messages.length)messages.at(-1).content+='\n'+line;
          else if(line.trim())throw Error('请以“用户：”“助手：”或“工具：”标明每轮对话。');
        }
        const rules=getDraft();
        button.disabled=true;onBusy(true);output.textContent='正在按已保存规则和草稿分别提炼…';
        const result=await api('extraction/preview',{project,messages,rules,include_related:section.querySelector('#extraction-related').checked});
        const card=(label,r)=>`<section><h4>${label}</h4><p class="version">版本 ${esc(r.rule_revision.slice(0,12))} · 对照 ${r.related_ids.length} 条旧知识</p>${r.candidates.map(c=>`<article class="extraction-candidate"><span class="badge ${c.status==='active'?'green':'amber'}">${esc(c.action==='skip'?'跳过':({active:'自动入库',candidate:'待确认',archived:'不入库',ignored:'跳过'})[c.status]||c.status)}</span> <strong>${esc(({add:'新增',supplement:'补充',replace:'替代',skip:'重复'})[c.action]||c.action)}</strong><p>${esc(c.body)}</p><small>${esc(c.reason)}</small>${window.EvolvExtraction.process(c,esc)}</article>`).join('')||'<p>未提炼出需要保存的原子知识。</p>'}<details><summary>查看本次完整提炼提示</summary><pre>${esc(r.prompt)}</pre></details></section>`;
        output.innerHTML=`<p class="hint">已完成 ${result.model_calls} 次模型调用，写入 0 条资料。保存规则后用于后续提炼；来源有变化时仍需重新核对。</p><div class="extraction-comparison">${card('已保存规则',result.current)}${card('当前草稿',result.draft)}</div>`;
      }catch(error){output.textContent=error.message;}finally{button.disabled=false;onBusy(false);}
    };
  }
};
