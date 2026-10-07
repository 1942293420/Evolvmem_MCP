/* The rule editor and the ingestion adapters use the same extraction contract. */
window.EvolvExtraction = {
  settings(root, previous) {
    return {...previous,
      extraction_instructions:root.querySelector('#extraction-instructions').value,
      related_memory_projects:[...root.querySelectorAll('[data-related-project]:checked')].map(e=>e.value)};
  },
  process(learning, esc) {
    const names={goal:'用户目标',understanding:'AI 的理解',correction:'用户纠正',decision:'明确决定',verification:'验证陈述'};
    const entries=Object.entries(learning?.process||{});
    const n=learning?.normalization;const normalized=n?`<section class="requirement-expression"><h4>整理后的需求表达</h4><p>${esc(n.requirement)}</p>${(learning.evidence||[]).map(e=>`<blockquote>${esc(e.quote)}</blockquote>`).join('')}${n.acceptance?.length?`<p>用户明确的验收要求：${n.acceptance.map(esc).join('；')}</p>`:''}${n.questions?.length?`<p class="reason">待确认：${n.questions.map(esc).join('；')}</p>`:''}${learning.normalization_errors?.length?`<p class="reason">${learning.normalization_errors.map(esc).join('；')}</p>`:''}<small>这是引用原话整理的需求，不改写历史对话。</small></section>`:'';
    if(!entries.length)return normalized;
    const relation=learning.relation;
    return normalized+`<details class="extraction-process"><summary>查看交流与验证来源${relation?.target_id?' · 关联资料 #'+Number(relation.target_id):''}</summary>${entries.map(([stage,e])=>`<div><strong>${esc(names[stage]||stage)}</strong><blockquote>${esc(e.quote)}</blockquote><small>${esc(({user:'用户',assistant:'助手',tool:'工具'})[e.role]||e.role)} · 消息 ${Number(e.message_index)+1}${stage==='verification'?' · 原始陈述，不直接计作成功':''}</small></div>`).join('')}${learning.process_errors?.length?'<p class="hint">部分过程无法核对，已留待确认。</p>':''}</details>`;
  },
  support(learning, esc) {
    const s = learning?.answer_support;
    if (!s) return '';
    const verdicts = {supported: '独立核对通过', narrow: '已按原话收窄', review: '核对未通过，待确认'};
    const answer = s.corrected ? s.quote : s.asked;
    const marks = [];
    if (s.original_quote && s.corrected && s.original_quote !== s.quote) marks.push(`<p>原引用：<blockquote>${esc(s.original_quote)}</blockquote></p>`);
    if (s.original_answer && s.corrected && s.original_answer !== answer) marks.push(`<p>修正前答案：<span class="struck">${esc(s.original_answer)}</span></p>`);
    return `<details class="extraction-process answer-support"><summary>独立核对答案范围 · ${esc(verdicts[s.verdict]||s.verdict)}</summary><p class="reason">${esc(s.reason)}</p><div><strong>核对依据（用户原话）</strong><blockquote>${esc(s.quote)}</blockquote></div><div><strong>核对答案</strong><blockquote>${esc(answer)}</blockquote></div>${marks.join('')}<small>由独立模型核对答案与用户原话，引用只证明原话存在；仍可能留待人工确认。</small></details>`;
  },
  mount({editor,preview,output,api,esc,projects,policy,getDraft,onDirty,onBusy}) {
    const active=projects.filter(p=>p.status==='active');
    editor.innerHTML=`      <details class="rules-disclosure"><summary>对照项目旧知识<span>选择后续提炼可参考的项目</span></summary><div class="rules-disclosure-body"><p class="hint">勾选并保存后，该项目提炼会将最多 20 条旧知识（共 8,000 字）与对话一起发送给当前配置模型。未勾选时仅处理新对话。</p><div class="extraction-projects">${active.map(p=>`<label class="extraction-choice"><input type="checkbox" data-related-project value="${esc(p.project)}" ${policy.settings.related_memory_projects.includes(p.project)?'checked':''}>${esc(p.display_name||p.project)}</label>`).join('')||'<p class="hint">还没有已登记项目，请先在项目资料中添加。</p>'}</div></div></details>
      <details class="rules-disclosure"><summary>对话提炼说明<span>如何记录目标、纠正与决定</span></summary><div class="rules-disclosure-body"><div class="field"><label for="extraction-instructions">提炼说明</label><textarea id="extraction-instructions" class="code rules-long-text">${esc(policy.settings.extraction_instructions)}</textarea><small>可先在试运行中比较效果，再保存修改。</small></div></div></details>`;
    preview.innerHTML=`<div class="field"><label for="extraction-project">样例所属项目</label><select id="extraction-project"><option value="">请选择项目</option>${active.map(p=>`<option value="${esc(p.project)}">${esc(p.display_name||p.project)}</option>`).join('')}</select></div>
      <div class="field"><label for="extraction-sample">样例对话</label><textarea id="extraction-sample">用户：以后，修改界面时先明确操作目标和验收条件。
助手：我会新建一个独立管理页面。
用户：管理保留在原知识库，不另建页面。</textarea><small>每轮以“用户：”“助手：”或“工具：”开头。</small></div>
      <label class="extraction-choice"><input type="checkbox" id="extraction-related">本次同时参考该项目旧知识</label>
      <button class="write" id="extraction-preview-button">比较提炼结果</button><p class="hint rules-preview-note">样例及选中的旧知识会发送给当前配置模型：每个版本一次提炼，另加一次独立核对答案范围。比较已保存规则与当前草稿，不写入资料。</p>`;
    editor.addEventListener('input',e=>{if(e.target.id==='extraction-instructions'||e.target.id==='extraction-auto'||e.target.hasAttribute('data-related-project'))onDirty();});
    const button=preview.querySelector('#extraction-preview-button');
    button.onclick=async()=>{
      if(button.disabled)return;
      try{
        const project=preview.querySelector('#extraction-project').value;
        if(!project)throw Error('请先选择样例所属项目。');
        const messages=[];
        for(const line of preview.querySelector('#extraction-sample').value.split('\n')){
          const match=line.match(/^(用户|助手|工具)[：:]\s*(.*)$/);
          if(match)messages.push({role:({用户:'user',助手:'assistant',工具:'tool'})[match[1]],content:match[2]});
          else if(messages.length)messages.at(-1).content+='\n'+line;
          else if(line.trim())throw Error('请以“用户：”“助手：”或“工具：”标明每轮对话。');
        }
        const rules=getDraft();
        button.disabled=true;onBusy(true);output.textContent='正在按已保存规则和草稿分别提炼…';
        const result=await api('extraction/preview',{project,messages,rules,include_related:preview.querySelector('#extraction-related').checked});
        const card=(label,r)=>`<section><h4>${label}</h4><p class="version">版本 ${esc(r.rule_revision.slice(0,12))} · 对照 ${r.related_ids.length} 条旧知识</p>${r.candidates.map(c=>`<article class="extraction-candidate"><span class="badge ${c.status==='active'?'green':'amber'}">${esc(c.action==='skip'?'跳过':({active:'自动入库',candidate:'待确认',archived:'不入库',ignored:'跳过'})[c.status]||c.status)}</span> <strong>${esc(({add:'新增',supplement:'补充',replace:'替代',skip:'重复'})[c.action]||c.action)}</strong><p>${c.question?`问：${esc(c.question)}<br>答：${esc(c.answer||c.body)}`:esc(c.body)}</p><small>${esc(c.reason)}</small>${window.EvolvExtraction.process(c,esc)}${window.EvolvExtraction.support(c,esc)}</article>`).join('')||'<p>未提炼出需要保存的原子知识。</p>'}<details><summary>查看本次完整提炼提示</summary><pre>${esc(r.prompt)}</pre></details></section>`;
        output.innerHTML=`<div class="rules-result-heading"><h3>提炼结果对比</h3><span class="hint">修改规则或样例后，请重新试运行</span></div><p class="hint">清洗后保留 ${result.cleaning?.dialogue_messages ?? "—"} 轮对话；已完成 ${result.model_calls} 次模型调用，写入 0 条资料。保存规则后用于后续提炼；来源有变化时仍需重新核对。</p><div class="extraction-comparison">${card('已保存规则',result.current)}${card('当前草稿',result.draft)}</div>`;
      }catch(error){output.textContent=error.message;}finally{button.disabled=false;onBusy(false);}
    };
  }
};
