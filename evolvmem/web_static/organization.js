/* Persistent background organization: launch a task, leave the page, review later. */
(() => {
  // One live instance at a time. A detach invalidates the old polling chain, so
  // navigating between views cannot leave detached timers or stale drafts.
  let active = null;
  window.EvolvOrganization = {async render({mount, api, esc, toast, projects, onDirty, onBusy, initialTab="review", onTab=()=>{}}) {
  const token = {};
  active = {mount, token};
  const [queue, guidance, settings] = await Promise.all([
    api('organization'), api('organization/guidance'), api('organization/settings')]);
  // Linux 本机 Codex 采集状态：只读，失败不影响自动整理本身。
  let localCapture = null;
  async function loadCapture(){
    try { localCapture = await (await EvolvAuth.fetch('/api/local-capture', {})).json(); }
    catch(error) { localCapture = null; }
    drawCapture();
  }
  // 数据库时间是 UTC；界面按访问者本地时区显示，避免误判“刚刚/昨天”。
  function localTime(value){
    const parsed = new Date(String(value).replace(' ', 'T') + 'Z');
    if(isNaN(parsed)) return value;
    const pad = n => String(n).padStart(2, '0');
    return `${pad(parsed.getMonth()+1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
  }
  function captureText(){
    const c = localCapture;
    if(!c || c.ok === false) return '本机采集状态暂不可用。';
    if(!c.configured) return '本机采集：未配置（未开启则不采集本机 Codex 会话）。';
    if(c.error) return '本机采集：配置有误，已停止采集。';
    if(!c.enabled) return '本机采集：已停用。';
    // auto_new 关闭时后台不再采集本机新对话，必须与“没有新内容”区分开。
    if(c.auto_new === false) return '本机采集：已暂停（“自动处理新资料”关闭时不会采集本机新对话）。';
    const wait = c.pending_bytes > 0 ? ` · 待处理 ${c.pending_bytes} 字节` : '';
    if(c.needs_review > 0) return `本机采集：${c.needs_review} 个文件需要人工处理，已停止在该文件继续采集${wait}`;
    const archived = c.archived_batches > 0 ? `已归档 ${c.archived_batches} 批对话` : '';
    // 只消费了旧字节而没有归档时，绝不能显示成“成功归档 0 批”。
    const scanned = archived ? archived : '已扫描，暂无新增可见对话';
    if(!c.last_success_at) return c.sessions.length ? '本机采集：已启用，尚无新增对话。' : '本机采集：已启用，尚未发现会话文件。';
    const line = `本机采集：${localTime(c.last_success_at)} ${scanned}${wait}`;
    return c.last_error ? `${line}；最近一次失败：${c.last_error}` : line;
  }
  function drawCapture(){
    // The message and its badge are two separate spans: writing the message into
    // the paragraph itself would wipe the badge node.
    const message = $('#org-local-capture-message'), badge = $('#org-local-capture-state');
    if(!message || !badge) return;
    message.textContent = captureText();
    const c = localCapture;
    badge.innerHTML = (!c || c.ok === false) ? ''
      : (c.error || c.needs_review > 0 || c.last_error) ? '<span class="badge red">需处理</span>'
      : !c.enabled ? (c.configured ? '<span class="badge">已停用</span>' : '')
      : c.auto_new === false ? '<span class="badge amber">已暂停</span>'
      : c.archived_batches === 0 ? '<span class="badge">暂无新对话</span>'
      : '<span class="badge green">已归档</span>';
  }
  let tasks = queue.items, current = new Set(queue.current_task_ids || []), busy = false;
  let guidanceRows = guidance.items, arrival = settings, total = queue.total || 0, page = 1, filter = 'review';
  let tab='review', loadSequence=0;
  const tabs={review:'待你判断',failed:'系统问题',completed:'已完成',history:'进度历史',guidance:'我的指导',all:'运行记录'};
  const counts = queue.counts || {};
  const open = new Map(), drafts = new Map(), errors = new Map(), selected = new Set();
  const $ = s => mount.querySelector(s);
  const name = p => projects.find(x => x.project === p)?.display_name || p || '未归属';
  const statusText = {pending:'等待处理',running:'处理中',completed:'已完成',review:'待确认',failed:'失败',superseded:'已被新版本替代'};
  const stageText = {queued:'排队',cleaning:'清洗',segmentation:'分段',assignment:'归属',extraction:'提取问答与经验',done:'完成',stale:'过期',retired:'已退出'};
  const errorText = e => ({revision_conflict:'任务或来源已变化，请刷新后重试。',cleaning_source_deleted:'来源已被永久删除。',cleaning_source_unavailable:'原文暂不可读取。',source_no_dialogue:'这次会话没有对话内容，无需审核，也不进入整理。',source_superseded:'旧快照已退出当前队列，原始归档仍保留；最新版本会正常整理。',source_excluded:'系统子会话已自动排除，不进入整理。',extraction_provider_unavailable:'尚未配置整理模型。',coverage_coverage_gap:'有段落未被处理，已停止；请核对原文后重试。',coverage_coverage_overlap:'分段位置重叠，已停止；请核对原文后重试。',coverage_quote_not_found:'模型引用的原话在原文中找不到，已停止。',units_need_review:'部分单元需要人工确认。',organization_failed:'整理失败，可重试。',organization_needs_context_mode:'当前服务模式不写入知识库，无法自动整理。',task_superseded:'任务已被新版本替代。',resegment_manual_review_required:'这份资料已有人工确认，不能重新分段覆盖；请在现有单元中修改。',guidance_not_found:'这条指导已不存在。',invalid_guidance:'请填写 1–1000 字的指导。',invalid_guidance_scope:'适用范围不正确。'})[e]||e;
  let groupDirty = false;
  const dirty = () => onDirty(drafts.size > 0 || groupDirty);
  function notice(t){$('#org-notice').textContent = t;}
  mount.innerHTML = `<section class="panel organization-panel">
  <div class="organization-topbar"><span id="org-arrival-state">${arrival.auto_new?'自动处理已开启':'自动处理已暂停'}</span><div class="actions"><button data-org-action="refresh">刷新状态</button><details class="organization-options"><summary>处理设置</summary><div class="organization-settings"><label class="extraction-choice"><input type="checkbox" data-org-arrival ${arrival.auto_new?'checked':''}>自动处理新资料</label><button class="write" data-org-action="backlog">补跑存量资料（每次 20 份）</button></div></details></div></div>
  <nav class="organization-tabs" role="tablist" aria-label="整理工作区">${Object.entries(tabs).map(([key,label])=>`<button role="tab" id="org-tab-${key}" aria-controls="org-pane-${key}" aria-selected="${key==='review'}" data-org-tab="${key}">${label}</button>`).join('')}</nav>
  <p id="org-notice" role="status"></p>
  <section data-org-pane="review" id="org-pane-review" role="tabpanel" aria-labelledby="org-tab-review"><div class="review-decision-notice"><strong>只处理系统无法确定的资料</strong><div class="actions"><a href="#knowledge/qa">问答内容核对 →</a><a href="#knowledge/intake">存量资料待确认 →</a></div></div><div id="org-review-groups"></div></section>
  <section data-org-pane="failed" id="org-pane-failed" role="tabpanel" aria-labelledby="org-tab-failed" hidden><div class="review-decision-notice"><strong>处理失败，无需重新判断项目</strong><button class="write" data-org-action="retry-failed">重试本页失败任务</button></div></section>
  <section data-org-pane="completed" id="org-pane-completed" role="tabpanel" aria-labelledby="org-tab-completed" hidden><div class="actions"><strong>结果已保存，无需再次审核</strong><a href="#knowledge/projects">查看项目历史 →</a><a href="#knowledge/qa">查看可用知识 →</a></div></section>
  <section data-org-pane="history" id="org-pane-history" role="tabpanel" aria-labelledby="org-tab-history" hidden><div id="org-history-mount"></div></section>
  <section data-org-pane="guidance" id="org-pane-guidance" role="tabpanel" aria-labelledby="org-tab-guidance" hidden><section class="organization-guidance"><h2>我的整理指导</h2><div id="org-guidance"></div></section></section>
  <section data-org-pane="all" id="org-pane-all" role="tabpanel" aria-labelledby="org-tab-all" hidden><p id="org-local-capture" role="status"><span id="org-local-capture-message">读取采集状态…</span> <span id="org-local-capture-state"></span></p><details><summary>效果与抽检</summary><div id="org-metrics"></div></details></section>
  <details class="organization-detail-tools" id="org-task-region"><summary id="org-task-summary">逐条判断</summary><div id="org-tasks"></div><div class="organization-pager"><label for="org-filter">状态</label><select id="org-filter"><option value="current">当前记录</option><option value="pending">排队中</option><option value="running">处理中</option><option value="completed">已完成</option><option value="review">待判断</option><option value="failed">处理失败</option><option value="superseded">已替代</option><option value="all">全部记录</option></select><span id="org-page"></span><button data-org-action="previous">上一页</button><button data-org-action="next">下一页</button></div></details></section>`;
  async function selectTab(value){
    const next=Object.hasOwn(tabs,value)?value:'review';tab=next;
    mount.querySelectorAll('[data-org-tab]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.orgTab===tab)));
    mount.querySelectorAll('[data-org-pane]').forEach(p=>p.hidden=p.dataset.orgPane!==tab);
    $('#org-task-region').hidden=['history','guidance'].includes(tab);
    $('#org-task-region').open=tab!=='review'||open.size>0;
    $('#org-task-summary').textContent=tab==='review'?'逐条判断与修改':'处理记录';
    filter=({review:'review',failed:'failed',completed:'completed'})[tab]||'current';page=1;
    onTab(tab);await load();
  }
  mount.addEventListener('click',e=>{const b=e.target.closest('[data-org-tab]');if(!b||busy)return;e.preventDefault();selectTab(b.dataset.orgTab).catch(error=>notice(errorText(error.message)));});
  const groupHandle=await EvolvReviewGroups.render({mount:$('#org-review-groups'),historyMount:$('#org-history-mount'),api,esc,projects,onBusy,
    onTask:async id=>{await selectTab('review');const detail=await api('organization/detail?'+new URLSearchParams({task_id:id}));if(!tasks.some(t=>t.id===id))tasks.unshift(detail);if(detail.source_current)current.add(id);else current.delete(id);open.set(id,detail.units||[]);draw();$('#org-task-region').open=true;$(`[data-org-task="${id}"]`)?.scrollIntoView({block:'start',behavior:'smooth'});},
    onDirty:value=>{groupDirty=value;dirty();},onSaved:async()=>{guidanceRows=(await api('organization/guidance')).items;await load();drawGuidance();}});
  async function loadMetrics(){try{$('#org-metrics').innerHTML=EvolvOrganizationMetrics.html(await api('organization/metrics'),esc);}catch(error){$('#org-metrics').textContent='整理效果暂不可用，可刷新重试。';}}
  await loadMetrics();
  function taskCard(task){
    const stale = !current.has(task.id), rows = open.get(task.id) || [];
    const failed = task.status === 'failed';
    const pendingDrafts = rows.filter(u => drafts.has(`${task.id}:${u.digest}`) && selected.has(`${task.id}:${u.digest}`)).length;
    return `<article class="organization-task ${stale?'is-stale':''}" data-org-task="${task.id}" data-org-review="${task.review_count}">
   <div class="section-head"><div><strong>${esc(task.source_title || '未命名资料')}</strong> <span class="badge ${task.status==='completed'?'green':task.status==='review'?'amber':task.status==='failed'?'red':''}">${statusText[task.status]||esc(task.status)}</span> <span class="task-stage">${stageText[task.stage]||esc(task.stage)} · ${task.unit_count} 个片段${task.review_count?` · ${task.review_count} 个待判断`:''}</span></div><div class="actions">${open.has(task.id)&&pendingDrafts?`<button class="primary write" data-org-action="correct-selected" data-id="${task.id}">保存所选 ${pendingDrafts} 条</button>`:''}<button data-org-action="toggle" data-id="${task.id}">${open.has(task.id)?'收起':'查看结果'}</button>${failed?`<button class="write" data-org-action="retry" data-id="${task.id}">重试</button><button data-org-action="resegment" data-id="${task.id}">重新分段</button>`:''}</div></div>
   ${task.error_code?`<p class="error" role="alert">${esc(({extraction_failed:'知识提炼未完成',extraction_summary_missing:'提炼结果缺少摘要'})[task.error_code]||errorText(task.error_code))}</p>`:''}
   <details class="task-diagnostics"><summary>来源与处理详情</summary><p>${esc(task.source_key)} · 尝试 ${task.attempts} 次</p>${task.error_detail?`<p>${esc(task.error_detail)}</p>`:''}</details>
   ${stale?'<p class="hint">这是旧版本结果，仅供参考；最新结果见当前列表中的任务。</p>':''}
   ${open.has(task.id)?`<div class="organization-units">${rows.length?unitGroups(task,rows):'<p class="hint">暂无单元。</p>'}</div>`:''}
  </article>`;
  }
  function unitGroups(task,rows){
    // Grouping is only a convenience: every item keeps its own decision and save.
    const order = [['review','待确认（需要你决定）'],['auto','自动归属'],['manual','人工已确认'],['aside','已暂存（可恢复）'],['history','仅留历史（可恢复处理）']];
    return order.map(([decision,label]) => {
      const group = rows.filter(unit => decision==='history' ? unit.disposition==='history_only' : decision==='aside' ? unit.disposition==='set_aside' : !['set_aside','history_only'].includes(unit.disposition) && (decision==='review' ? unit.decision==='review'||unit.disposition==='review'||!unit.project : unit.disposition!=='review'&&unit.decision===decision&&unit.project));
      if(!group.length) return '';
      const heading = `<div class="organization-group-head"><strong>${label} · ${group.length} 条</strong>${decision==='review'?`<span class="actions"><label>批量选择项目<select data-org-group-project="${task.id}"><option value="">不批量改</option>${projects.filter(p=>p.status==='active').map(p=>`<option value="${esc(p.project)}">${esc(p.display_name||p.project)}</option>`).join('')}</select></label><button class="write" data-org-action="stage-group" data-id="${task.id}">应用到本组所选</button><label class="extraction-choice"><input type="checkbox" data-org-select-group="${task.id}">选择本组全部</label></span>`:''}</div>`;
      return decision==='review'?`<section class="organization-group" data-org-group="${decision}">${heading}${group.map(unitRow).join('')}</section>`:`<details class="organization-group" data-org-group="${decision}"><summary>${label} · ${group.length} 条</summary>${group.map(unitRow).join('')}</details>`;
    }).join('');
  }
  const extractionText={pending:'待提炼',running:'提炼中',done:'已提炼',skipped:'无需重复提炼',failed:'提炼失败'};
  function unitRow(unit){
    const key = `${unit.task_id}:${unit.digest}`, draft = drafts.get(key);
    const applied = typeof draft?.guidance === 'string' ? draft.guidance : '';
    const aside = unit.disposition === 'set_aside', historyOnly = unit.disposition === 'history_only';
    const ctx = unit.context && unit.context.state === 'ready' ? unit.context : null;
    return `<div class="organization-unit ${unit.decision==='manual'?'is-manual':''} ${aside?'is-aside':''}" data-org-unit="${esc(unit.digest)}">
   <div class="section-head"><div><label class="extraction-choice"><input type="checkbox" data-org-select-unit="${key}" ${selected.has(key)?'checked':''} aria-label="选择 ${esc(unit.title)}"> <strong>${esc(unit.title)}</strong></label> <span class="unit-category">${esc(({habit:'长期习惯',project_convention:'项目约定',task_requirement:'任务要求',environment:'环境事实',decision:'决策依据',experience:'技术经验',reference:'参考资料'})[unit.category]||unit.category)} · ${historyOnly?'仅留历史':unit.decision==='manual'?'人工已确认':unit.decision==='auto'?'自动归属':'待确认'} · 提炼：${historyOnly?'无需提炼，可恢复':extractionText[unit.extraction_stage]||esc(unit.extraction_stage)}${unit.extraction_error?'（'+esc(unit.extraction_error)+'）':''}</span>${aside?' <span class="badge">已暂存，不进入当前历史</span>':''}</div><span>${esc(unit.project?name(unit.project):'未归属')}</span></div>
   <div class="review-evidence-grid"><section><span class="review-question-label">${unit.decision==='review'||unit.disposition==='review'?'为什么需要你':'整理依据'}</span><p class="organization-reason">${esc(unit.disposition==='review'?unit.disposition_reason||unit.reason:unit.reason)}</p></section><section><span class="review-question-label">当前判断</span><p>${esc(unit.project?name(unit.project):'尚未确定所属项目')}</p></section></div>
   ${unit.diagnostic_text?`<details><summary>查看失败详情</summary><p class="error">${esc(unit.diagnostic_text)}</p></details>`:''}
   <details class="optional-check"><summary>抽检反馈（可选）</summary><div class="actions"><span>整理判断${unit.feedback?` · 已标记${unit.feedback==='correct'?'正确':'有误'}`:''}</span><button data-org-action="feedback" data-verdict="correct" data-key="${key}" data-revision="${esc(unit.revision)}">判断正确</button><button data-org-action="feedback" data-verdict="incorrect" data-key="${key}" data-revision="${esc(unit.revision)}">判断有误</button></div></details>
   ${ctx?`<p class="organization-reason organization-context">前文已确认项目：${esc(name(ctx.project))}，原话「${esc(ctx.quote||'')}」</p>`:''}
   <span class="review-example-label">关键原话</span><blockquote class="organization-quote">${esc(unit.evidence_quote||unit.text.slice(0,120))}</blockquote>
   <details><summary>查看原文片段（${unit.source_end-unit.source_start} 字）</summary><pre class="organization-text">${esc(unit.text)}</pre></details>
   <details class="unit-decision" ${unit.decision==='review'||unit.disposition==='review'?'open':''}><summary>${unit.decision==='review'||unit.disposition==='review'?'你的决定':'修改整理结果'}</summary><div class="organization-correct">
    <label>所属项目<select data-org-project="${key}" aria-label="所属项目">${'<option value="">保持未归属</option>'+projects.filter(p=>p.status==='active').map(p=>`<option value="${esc(p.project)}" ${p.project===(draft?.project??unit.project)?'selected':''}>${esc(p.display_name||p.project)}</option>`).join('')}</select></label>
    <label>补充指导（可选）<input data-org-guidance="${key}" maxlength="200" placeholder="例如：这类导出要求属于 Evo 项目" value="${esc(applied)}"></label>
    <label>适用范围<select data-org-scope="${key}" aria-label="适用范围"><option value="batch" ${draft?.scope!=='future'?'selected':''}>仅本次</option><option value="future" ${draft?.scope==='future'?'selected':''}>以后同类适用</option></select></label>
    ${draft?.scope==='future'?`<label>适用条件<input data-org-condition="${key}" maxlength="200" placeholder="填写具体适用短语" value="${esc(draft?.condition||'')}"></label><label>例外<input data-org-exceptions="${key}" maxlength="200" placeholder="例如：其他平台 | 其他店铺" value="${esc(draft?.exceptions||'')}"></label><label class="extraction-choice"><input type="checkbox" data-org-negative="${key}" ${draft?.negative?'checked':''}>这是反例：以后遇到类似内容不要套用</label>`:''}
    <span class="actions"><button class="write" data-org-action="correct" data-key="${key}">保存决定</button>${unit.disposition==='review'?`<button class="write primary" data-org-action="disposition" data-key="${key}" data-value="keep" data-revision="${esc(unit.revision)}">保留并继续提炼</button>`:''}${`<button class="write" data-org-action="disposition" data-key="${key}" data-value="${aside||historyOnly?'keep':'set_aside'}" data-revision="${esc(unit.revision)}">${aside||historyOnly?'恢复处理':'稍后处理'}</button>`}</span>
    ${errors.has(key)?`<p class="error" role="alert">${esc(errors.get(key))}</p>`:''}
    ${draft?`<p class="review-outcome">保存后：${draft.scope==='future'?'按条件处理以后同类资料':'仅修改当前资料'}</p>`:''}
   </div></details></div>`;
  }
  function renderTaskList(){
    const ordered = [...tasks].sort((a,b) => (current.has(b.id)-current.has(a.id)) || b.id-a.id);
    $('#org-tasks').innerHTML = ordered.length ? ordered.map(taskCard).join('') : `<div class="empty">${tab==='failed'?'当前没有处理失败':tab==='completed'?'暂无完成记录':tab==='review'?'当前没有需要逐条判断的任务':'暂无处理记录'}</div>`;
    updatePager();
  }
  // Polling only refreshes the counters: rewriting the list would collapse an
  // open unit list and reset a field the user is editing.
  function updatePager(){
    $('#org-page').textContent = `共 ${total} 个任务 · 第 ${page} / ${Math.max(1,Math.ceil(total/PAGE))} 页 · 待处理 ${counts.pending||0} · 处理中 ${counts.running||0} · 已完成 ${counts.completed||0} · 待确认 ${counts.review||0} · 失败 ${counts.failed||0}`;
    $('#org-filter').value = filter;
    $('[data-org-action="previous"]').disabled = busy || page <= 1;
    $('[data-org-action="next"]').disabled = busy || page * PAGE >= total;
    $('[data-org-arrival]').checked = !!arrival.auto_new;
    $('#org-arrival-state').dataset.enabled=String(!!arrival.auto_new);
    $('#org-arrival-state').textContent=arrival.auto_new?'自动处理已开启':'自动处理已暂停';
  }
  const PAGE = queue.page_size || 20;
  function drawGuidance(){
    $('#org-guidance').innerHTML = guidanceRows.length ? `<div class="table-wrap"><table><thead><tr><th>指导</th><th>项目</th><th>范围与条件</th><th>状态</th><th></th></tr></thead><tbody>${guidanceRows.map(row=>`<tr><td>${esc(row.source_text||row.guidance)}${row.negative?' <span class="badge amber">反例</span>':''}</td><td>${esc(row.project?name(row.project):'未指定')}</td><td>${esc(row.scope==='future'?'以后同类适用':'仅本次')}${row.condition?' · 条件：'+esc(row.condition):''}${row.exceptions?' · 例外：'+esc(row.exceptions):''}${row.negative?' · 反例':''}</td><td>${row.scope==='future'&&row.state!=='reusable'?'<span class="badge amber">条件太宽，仅作建议</span>':row.enabled?'<span class="badge green">已启用</span>':'<span class="badge">已停用</span>'}</td><td><button class="write" data-org-action="toggle-guidance" data-id="${row.id}" data-enabled="${row.enabled}" data-revision="${row.revision}">${row.enabled?'停用':'启用'}</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="hint">还没有保存过整理指导。</p>';
  }
  function focusState(){
    const el = document.activeElement;
    if(!el || !mount.contains(el) || !('selectionStart' in el)) return null;
    return {selector: el.dataset.orgGuidance ? `[data-org-guidance="${el.dataset.orgGuidance}"]`
      : el.dataset.orgCondition ? `[data-org-condition="${el.dataset.orgCondition}"]`
      : el.dataset.orgExceptions ? `[data-org-exceptions="${el.dataset.orgExceptions}"]` : null,
      start: el.selectionStart, end: el.selectionEnd};
  }
  function restoreFocus(state){
    if(!state?.selector) return;
    const el = mount.querySelector(state.selector);
    if(!el) return;
    el.focus();
    try{el.setSelectionRange(state.start, state.end);}catch(error){/* non-text inputs */}
  }
  function draw(){
    const focus = focusState();
    renderTaskList(); drawGuidance(); drawCapture(); dirty();
    restoreFocus(focus);
  }
  function snapshot(){return JSON.stringify(tasks.map(t=>[t.id,t.status,t.stage,t.unit_count,t.review_count,t.attempts,t.updated_at,t.error_code,current.has(t.id)]));}
  function snapshotFor(result){return JSON.stringify(result.items.map(t=>[t.id,t.status,t.stage,t.unit_count,t.review_count,t.attempts,t.updated_at,t.error_code,(result.current_task_ids||[]).includes(t.id)]));}
  async function load(){
    const sequence=++loadSequence;
    const result = await api('organization?' + new URLSearchParams({page:String(page),status:filter}));
    if(sequence!==loadSequence)return;
    const previous = snapshot();
    const next = snapshotFor(result);
    tasks = result.items; current = new Set(result.current_task_ids||[]);
    total = result.total || 0; Object.assign(counts, result.counts || {});
    for(const id of [...open.keys()]) if(!tasks.some(t=>t.id===id)) open.delete(id);
    // Background polling must not collapse an open list, drop drafts or move
    // the caret of a field the user is editing.
    let changed = next !== previous;
    for(const id of open.keys()){
      const detail = await api('organization/detail?'+new URLSearchParams({task_id:id}));
      if(sequence!==loadSequence)return;
      if(JSON.stringify(open.get(id)) !== JSON.stringify(detail.units)){open.set(id,detail.units||[]);changed=true;}
    }
    if(changed) {draw();await loadMetrics();} else { updatePager(); dirty(); }
  }
  async function openTask(id){
    if(open.has(id)){open.delete(id);draw();return;}
    const result = await api('organization/detail?'+new URLSearchParams({task_id:id}));
    open.set(id,result.units||[]); draw();
  }
  function draftFor(key, unit){
    const draft = drafts.get(key) || {};
    return {task_id:Number(key.split(':')[0]), digest:unit.digest, expected_revision:unit.revision,
      project:draft.project??unit.project, guidance:draft.guidance||'', scope:draft.scope||'batch',
      condition:draft.condition||'', exceptions:draft.exceptions||'', negative:draft.negative===true};
  }
  async function saveKeys(keys){
    const payloads = [];
    for(const key of keys){
      const [taskId,digest] = key.split(':');
      const unit = (open.get(Number(taskId))||[]).find(u=>u.digest===digest);
      if(!unit) continue;
      if(!drafts.has(key)){errors.set(key,'请先选择项目或填写指导。');continue;}
      payloads.push(draftFor(key,unit));
    }
    if(!payloads.length) throw Error('没有可保存的修改。');
    const result = await api('organization/correct',{items:payloads});
    let saved = 0, failed = 0, future = 0;
    for(const row of result.items){
      const key = `${payloads.find(p=>p.digest===row.digest)?.task_id}:${row.digest}`;
      if(row.ok){
        const rows = open.get(payloads.find(p=>p.digest===row.digest).task_id) || [];
        open.set(payloads.find(p=>p.digest===row.digest).task_id, rows.map(u=>u.digest===row.digest?row.unit:u));
        drafts.delete(key); errors.delete(key); selected.delete(key); saved++;
        if(row.guidance?.scope==='future' && row.guidance?.state==='reusable') future++;
      } else { errors.set(key,errorText(row.error)); failed++; }
    }
    guidanceRows = (await api('organization/guidance')).items;
    notice(`已保存 ${saved} 条，失败 ${failed} 条。${failed?'失败项保留修改，可核对后重试。':''}${future?`其中 ${future} 条指导会在后续匹配单元中沿用。`:''}`);
    await load();await groupHandle.refresh();draw();
  }
  mount.oninput = e => {const el = e.target;
    if(el.dataset.orgGuidance){const d = drafts.get(el.dataset.orgGuidance)||{};drafts.set(el.dataset.orgGuidance,{...d,guidance:el.value});dirty();}
    else if(el.dataset.orgCondition){const d = drafts.get(el.dataset.orgCondition)||{};drafts.set(el.dataset.orgCondition,{...d,condition:el.value});dirty();}
    else if(el.dataset.orgExceptions){const d = drafts.get(el.dataset.orgExceptions)||{};drafts.set(el.dataset.orgExceptions,{...d,exceptions:el.value});dirty();}
  };
  mount.onchange = e => {const el = e.target;
    if(el.id === 'org-filter'){filter = el.value; page = 1; load().catch(error=>notice(errorText(error.message)));return;}
    if(el.dataset.orgArrival !== undefined){
      arrival = {...arrival, auto_new: el.checked?1:0};
      api('organization/settings',{auto_new:el.checked, expected_revision:arrival.revision}).then(result=>{
        arrival = result; notice(result.auto_new?'已开启：只处理开启后新到的来源，历史存量请用“整理待处理资料”。':'已关闭新资料自动处理；排队中的任务不受影响。'); draw();
      }).catch(error=>{notice(errorText(error.message)); draw();});
      return;
    }
    if(el.dataset.orgSelectUnit){el.checked?selected.add(el.dataset.orgSelectUnit):selected.delete(el.dataset.orgSelectUnit);draw();return;}
    if(el.dataset.orgSelectGroup !== undefined){const id = Number(el.dataset.orgSelectGroup);for(const unit of open.get(id)||[]) if(unit.disposition!=='set_aside' && (unit.decision==='review'||!unit.project)){const key = `${id}:${unit.digest}`;el.checked?selected.add(key):selected.delete(key);}draw();return;}
    const key = el.dataset.orgProject || el.dataset.orgScope || el.dataset.orgNegative;
    if(key){const d = drafts.get(key)||{};
      if(el.dataset.orgProject !== undefined) drafts.set(key,{...d,project:el.value});
      else if(el.dataset.orgScope !== undefined) drafts.set(key,{...d,scope:el.value});
      else drafts.set(key,{...d,negative:el.checked});
      dirty(); draw();}
  };
  mount.onclick = async e => {const button = e.target.closest('[data-org-action]');if(!button||busy)return;e.preventDefault();const action = button.dataset.orgAction;
    if(action==='toggle'){await openTask(Number(button.dataset.id));return;}
    if(action==='stage-group'){
      const id = Number(button.dataset.id), project = $(`[data-org-group-project="${id}"]`).value;
      if(!project) throw Error('请先选择要批量应用的项目。');
      const keys = [...(open.get(id)||[])].filter(u=>u.disposition!=='set_aside' && (u.decision==='review'||!u.project)).map(u=>`${id}:${u.digest}`).filter(k=>selected.has(k));
      if(!keys.length) throw Error('请先勾选本组要修改的单元。');
      for(const key of keys){const d = drafts.get(key)||{};drafts.set(key,{...d,project,scope:d.scope||'batch'});}
      notice(`已为 ${keys.length} 条待确认单元预填项目；仍需核对后保存，未勾选单元不受影响。`);draw();return;
    }
    busy = true;onBusy(true);
    try{
      if(action==='correct') await saveKeys([button.dataset.key]);
      else if(action==='correct-selected') await saveKeys([...selected]);
      else if(action==='refresh') {await load();await loadMetrics();await groupHandle.refresh();}
      else if(action==='feedback'){
        const [id,digest]=button.dataset.key.split(':');
        await api('organization/feedback',{task_id:Number(id),digest,expected_revision:button.dataset.revision,verdict:button.dataset.verdict});
        notice('已记录本条抽检反馈；需要修正时可在下方保存项目或恢复处理。');await load();await loadMetrics();
      }
      else if(action==='retry'||action==='retry-failed'||action==='resegment'){
        const targets = action==='retry'||action==='resegment' ? [Number(button.dataset.id)] : tasks.filter(t=>t.status==='failed').map(t=>t.id);
        if(!targets.length) throw Error('没有可重试的失败任务。');
        const route = action==='resegment' ? 'organization/resegment' : 'organization/retry';
        for(const id of targets) await api(route,{task_id:id});
        if(action==='resegment') open.delete(Number(button.dataset.id));
        notice(action==='resegment'?`已丢弃旧分段并重新排队 ${targets.length} 个任务。`:`已重新排队 ${targets.length} 个任务。`);
        await load();
      }
      else if(action==='disposition'){
        const value = button.dataset.value === 'set_aside' ? 'set_aside' : 'keep';
        const payload = {task_id: Number(button.dataset.key.split(':')[0]),
                         digest: button.dataset.key.split(':')[1],
                         expected_revision: button.dataset.revision, disposition: value};
        if(value === 'set_aside') payload.reason = '用户在本页暂存，未删除原文';
        const result = await api('organization/disposition', payload);
        const id = Number(button.dataset.key.split(':')[0]);
        open.set(id, (open.get(id)||[]).map(u => u.digest === result.unit.digest ? {...result.unit, revision: result.unit.revision} : u));
        notice(value === 'set_aside' ? '已暂存这一条：不再进入当前项目历史，可随时恢复，原文未删除。' : '已保留；项目明确的内容会继续提炼。');
        await load();await groupHandle.refresh();draw();
      }
      else if(action==='backlog'){const result = await api('organization/backlog',{limit:20});notice(`已排队 ${result.created} 条，队列剩 ${result.remaining} 条（本次共 ${result.queue_total} 条待整理）。`);await load();}
      else if(action==='toggle-guidance'){
        const id = Number(button.dataset.id);
        const row = guidanceRows.find(item=>item.id===id);
        const enabledNow = row ? !!row.enabled : button.dataset.enabled === '1';
        const result = await api('organization/guidance',{id,enabled:!enabledNow,expected_revision:row?.revision});
        guidanceRows = guidanceRows.map(item=>item.id===id?result.items[0]:item);
        notice(result.items[0].enabled?'已启用这条指导。':'已停用这条指导，后续匹配单元不再套用。');
        draw();
      }
      else {if(action==='previous')page = Math.max(1,page-1);if(action==='next')page++;await load();}
    }catch(error){notice(errorText(error.message));toast(errorText(error.message));}
    finally{busy = false;onBusy(false);}
  };
  await selectTab(initialTab);draw();
  await loadCapture();
  let running = false;
  const tick = async () => {
    // Stop cleanly once this view is replaced or detached: no detached timer
    // chain survives navigation.
    if(active?.token !== token || !mount.isConnected){running = false;return;}
    try{if(!busy)await load();}catch(error){notice('任务状态读取失败，请刷新重试。');}
    try{await loadCapture();}catch(error){}
    if(active?.token === token && mount.isConnected) setTimeout(tick,4000);else running = false;
  };
  const resume = () => {
    if(running || active?.token !== token) return;
    running = true;
    if(mount.isConnected) tick();
  };
  running = true;
  setTimeout(tick,4000);
  return {resume,selectTab};
  }};
})();
