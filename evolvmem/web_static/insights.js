/* Read-only views of evidence-backed experiences and saved project progress. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
  const API = {experiences: 'experiences', summaries: 'project-summaries', workstreams: 'workstreams'};
  const LABELS = {
    active: '有效', candidate: '待验证', archived: '已归档', superseded: '已被替代',
    single_verified: '单次验证', repeated_verified: '重复验证', unverified: '尚未验证', contradicted: '存在反证',
    technical: '技术验证', business: '业务结果', user_confirmed: '用户确认',
    success: '成功', confirmed: '确认有效', failure: '适用条件内失败', inapplicable: '本场景不适用',
    unknown: '结果未知', used: '已采用，未判定结果',
    ready: '摘要已更新', pending: '等待生成', failed: '最近更新失败', vector_dirty: '摘要已生成，检索索引待同步',
    open: '进行中', paused: '已暂停', blocked: '有阻塞', completed: '已完成', cancelled: '已取消',
    tool_result: '工具验证记录', user_confirmation: '用户反馈', historical_record: '历史验证记录',
    context_reference: '记忆来源', experience: '经验来源',
  };
  const STATES = {
    experiences: [['all', '全部经验'], ['verified', '已验证'], ['candidate', '待验证'], ['inactive', '已归档 / 已失效']],
    summaries: [['all', '全部摘要'], ['ready', '已更新'], ['failed', '更新失败'], ['pending', '等待生成'], ['vector_dirty', '索引待同步']],
    workstreams: [['all', '全部任务'], ['unfinished', '未完成'], ['open', '进行中'], ['blocked', '有阻塞'], ['paused', '已暂停'], ['completed', '已完成'], ['cancelled', '已取消']],
  };
  const state = {kind: 'experiences', page: 1, size: 20, selected: null, request: 0, detailRequest: 0, rows: [], total: 0};
  let projectNames = {}, overview = null, overviewRequest = 0, debounce;
  const name = project => projectNames[project] ? `${projectNames[project]} · ${project}` : project;
  const label = value => LABELS[value] || value || '未记录';
  const date = value => {
    if (!value) return '未记录';
    const time = new Date(/[Z+]/.test(value) ? value : value.replace(' ', 'T') + 'Z');
    return Number.isNaN(time.valueOf()) ? value : time.toLocaleString('zh-CN', {hour12: false});
  };
  const badge = (text, good = false) => `<span class="insights-badge${good ? ' verified' : ''}">${esc(text)}</span>`;
  const plain = (text, fallback = '尚未记录') => `<p class="insights-text">${esc(text || fallback)}</p>`;
  const list = (values, ordered = false) => Array.isArray(values) && values.length
    ? `<${ordered ? 'ol' : 'ul'}>${values.map(v => `<li>${esc(v)}</li>`).join('')}</${ordered ? 'ol' : 'ul'}>`
    : '<p class="insights-note">尚未记录</p>';
  const conditions = values => values && Object.keys(values).length
    ? `<dl class="insights-conditions">${Object.entries(values).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`
    : '<p class="insights-note">没有记录具体条件，采用前仍需核对当前场景。</p>';
  const link = (row, kind = 'experiences') => `<button class="insights-link" data-open="${esc(row.id)}" data-kind="${kind}">#${esc(row.id)} · ${esc(row.problem || row.summary || row.project)}</button>`;

  async function get(path) {
    const response = await fetch(path, {cache: 'no-store'});
    if (!response.ok) throw new Error(response.status === 404 ? '记录已不存在，请刷新列表。' : '暂时无法读取记忆，请稍后重试。');
    return response.json();
  }

  function metrics(data) {
    return [
      [data.verified_experiences, '已验证经验', `其中 ${data.repeated_experiences} 条经过独立任务重复验证`, 'experiences', 'verified'],
      [data.candidate_experiences, '待验证经验', '等待真实结果，不作为成功方案推荐', 'experiences', 'candidate'],
      [data.ready_summaries, '已更新项目摘要', '查看项目目标、决定与进展', 'summaries', 'ready'],
      [data.unfinished_workstreams, '未完成任务', '包括进行中、暂停与阻塞', 'workstreams', 'unfinished'],
    ].map(([n, title, note, kind, status]) => `<button class="insights-metric" data-insight-kind="${kind}" data-insight-status="${status}"><strong>${n}</strong><span>${title} ↗</span><small>${note}</small></button>`).join('');
  }

  async function refreshOverview() {
    const token = ++overviewRequest;
    try {
      const result = await get('/api/insights');
      if (token !== overviewRequest) return;
      overview = result;
      projectNames = Object.fromEntries(result.projects.map(p => [p.project, p.display_name]));
      ['insights-overview', 'dash-insights-overview'].forEach(id => { $(id).innerHTML = metrics(result); });
      $('insights-asof').textContent = `全库概览 · 读取于 ${date(result.as_of)}`;
      $('dash-insights-note').textContent = '成功次数按独立任务计数；高频命中不等于验证成功。';
      const current = $('insights-project').value;
      $('insights-project').innerHTML = '<option value="">全部项目</option>' + result.projects.map(p => `<option value="${esc(p.project)}">${esc(name(p.project))}</option>`).join('');
      $('insights-project').value = current;
    } catch (error) {
      if (token !== overviewRequest) return;
      $('insights-asof').textContent = error.message;
      $('dash-insights-note').textContent = '经验统计暂时无法读取，点击刷新重试。';
      ['insights-overview', 'dash-insights-overview'].forEach(id => { $(id).textContent = '统计暂不可用'; });
    }
  }

  function setKind(kind, status = kind === 'experiences' ? 'verified' : 'all') {
    if (!API[kind]) return;
    state.kind = kind; state.page = 1; state.selected = null;
    $('insights-query').value = '';
    document.querySelectorAll('[data-insights-tab]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.insightsTab === kind)));
    $('insights-status').innerHTML = STATES[kind].map(([v, text]) => `<option value="${v}">${text}</option>`).join('');
    $('insights-status').value = STATES[kind].some(s => s[0] === status) ? status : 'all';
    $('insights-query').placeholder = kind === 'experiences' ? '搜索问题、步骤或适用条件' : '搜索项目或进展内容';
    $('insights-context-note').textContent = {
      experiences: '展示有结构化条件与步骤的案例；早期普通记忆仍可在归档整理中查看。',
      summaries: '展示最近保存的项目摘要；内容截至时间可在详情中查看。',
      workstreams: '展示助手保存的任务断点；这里的状态以最后一次保存为准。',
    }[kind];
  }

  function rowStatus(row) {
    if (state.kind !== 'experiences') return badge(label(row.status), row.status === 'ready' || row.status === 'completed');
    if (row.expired) return badge('已过期');
    const valid = row.status === 'active' && ['single_verified', 'repeated_verified'].includes(row.validation_level);
    return badge(valid ? label(row.validation_level) : label(row.status), valid)
      + (row.validation_level === 'contradicted' ? badge('存在反证') : '');
  }

  function renderRows() {
    $('insights-count').textContent = `共 ${state.total} 条记录`;
    $('insights-list').innerHTML = state.rows.length ? state.rows.map(row => `<button class="insights-entry" data-select="${esc(row.id ?? '')}" ${row.id == null ? 'disabled' : ''} aria-current="${String(row.id === state.selected)}">
      <span class="insights-badges">${rowStatus(row)}${row.content_type === 'playbook' ? badge('归纳方法') : ''}${row.is_focus ? badge('当前焦点') : ''}</span>
      <span class="insights-entry-title">${esc(row.problem || row.summary)}</span>
      <span class="insights-entry-meta"><span>${esc(name(row.project))}</span><span>${esc(date(row.updated_at))}</span></span>
      ${state.kind === 'experiences' ? `<span class="insights-entry-meta"><span>${row.success_count} 次独立成功 · ${row.failure_count} 次失败</span><span>${row.transferable ? '可跨项目参考' : '限原项目'}</span></span>` : ''}
    </button>`).join('') : '<div class="insights-empty"><p>没有符合当前筛选的记录。</p><p class="insights-note">可以切换项目或状态；新的经验和断点会在对话中逐步积累。</p></div>';
    $('insights-page').textContent = `${state.page} / ${Math.max(1, Math.ceil(state.total / state.size))}`;
    $('insights-prev').disabled = state.page <= 1;
    $('insights-next').disabled = state.page * state.size >= state.total;
  }

  async function loadList() {
    clearTimeout(debounce);
    const token = ++state.request;
    ++state.detailRequest;
    state.selected = null;
    $('insights-list').setAttribute('aria-busy', 'true');
    $('insights-detail').innerHTML = '<div class="insights-empty">选择一条记录，查看它的内容和依据。</div>';
    $('insights-detail').setAttribute('aria-busy', 'false');
    $('insights-error').hidden = true;
    $('insights-prev').disabled = $('insights-next').disabled = true;
    const params = new URLSearchParams({q: $('insights-query').value, project: $('insights-project').value,
      status: $('insights-status').value, page: state.page, page_size: state.size});
    try {
      const data = await get(`/api/${API[state.kind]}?${params}`);
      if (token !== state.request) return;
      state.rows = data.rows; state.total = data.total;
      renderRows();
      if (state.rows[0]?.id != null) await select(state.rows[0].id, false);
    } catch (error) {
      if (token !== state.request) return;
      state.rows = []; state.total = 0;
      renderRows();
      $('insights-list').innerHTML = '<div class="insights-empty">列表尚未加载。</div>';
      $('insights-error').textContent = `${error.message} 可点击上方“刷新”重试。`;
      $('insights-error').hidden = false;
    } finally {
      if (token === state.request) $('insights-list').setAttribute('aria-busy', 'false');
    }
  }

  function sources(rows) {
    if (!rows?.length) return '<p class="insights-note">暂无来源记录。</p>';
    return rows.map(source => `<div class="insights-proof"><span class="insights-note">${esc(label(source.source_kind))} · #${source.id}</span>
      ${source.summary ? plain(source.summary) : ''}<div class="insights-ref">${esc(source.source_ref)}</div></div>`).join('');
  }

  function experienceDetail(row) {
    const valid = row.status === 'active' && !row.expired && ['single_verified', 'repeated_verified'].includes(row.validation_level);
    const sourceMap = Object.fromEntries(row.sources.map(source => [source.id, source]));
    const verdictNote = row.expired ? '这条经验已过期，当前不会作为有效案例推荐。'
      : valid ? '这条经验有真实结果支持。复用前仍需比较问题机制、环境和约束。'
      : '这条经验当前不作为成功方案推荐。已有描述和历史结果保留，等待验证或复核。';
    return `<div class="insights-badges">${badge(row.expired ? '已过期' : label(row.status), valid)}${badge(label(row.validation_level), valid)}${badge(row.transferable ? '可跨项目参考' : '限原项目')}</div>
      <h2>${esc(row.problem)}</h2><p class="insights-note">${esc(name(row.project))} · 经验 #${row.id} · 更新于 ${esc(date(row.updated_at))}</p>
      <div class="insights-explanation">${verdictNote}<br>${row.success_count} 次独立成功 · ${row.failure_count} 次适用条件内失败。场景不适用不计为失败。</div>
      <h3>当时的条件</h3>${conditions(row.conditions)}
      <h3>采用的步骤</h3>${list(row.steps, true)}
      <h3>为何有效</h3>${plain(row.rationale)}
      <h3>案例记录的结果</h3>${plain(row.result)}
      <h3>适用范围</h3>${list(row.applicability)}
      <h3>不适用的情况</h3>${list(row.exclusions)}
      <h3>当前有效的成功证据</h3>${row.verification.length ? row.verification.map(proof => `<div class="insights-proof">${badge(label(proof.level), true)}${conditions(proof.conditions)}<div class="insights-ref">${esc(proof.source?.ref || '来源引用未保留')}</div></div>`).join('') : '<p class="insights-note">尚无有效成功证据。案例中的结果描述本身不等于验证通过。</p>'}
      <h3>反馈记录 <span class="insights-note">${row.evidence.length} 个事件</span></h3>
      <p class="insights-note">同一验证事件只展示最新修订，多条事件不一定代表多个独立任务。</p>
      ${row.evidence.length ? [...row.evidence].reverse().map(event => `<div class="insights-proof">
        <div class="insights-badges">${badge(label(event.outcome))}${event.verification_level ? badge(label(event.verification_level)) : ''}</div>
        <p class="insights-note">${esc(date(event.observed_at))} · 修订 ${event.revision}</p>${plain(event.note)}
        ${conditions(event.conditions)}<div class="insights-ref">${esc(sourceMap[event.source_id]?.source_ref || '来源未保留')}</div></div>`).join('') : '<p class="insights-note">尚未记录反馈。</p>'}
      <h3>场景派生关系</h3>${row.parent_case ? '<p class="insights-note">由这条经验调整而来</p>' + link(row.parent_case) : row.parent_experience_id ? `<p class="insights-note">父案例 #${row.parent_experience_id} 当前不可查看。</p>` : '<p class="insights-note">这是独立案例。</p>'}
      ${row.derived_cases.length ? '<p class="insights-note">在新场景中衍生的案例</p>' + row.derived_cases.map(r => link(r)).join('') : '<p class="insights-note">尚未形成新的派生案例。</p>'}
      <details><summary>全部来源 · ${row.sources.length} 条</summary>${sources(row.sources)}</details>`;
  }

  function summaryDetail(row) {
    return `${badge(label(row.status), row.status === 'ready')}<h2>${esc(name(row.project))}</h2>
      <p class="insights-note">内容更新于 ${esc(date(row.content_updated_at))}<br>来源截至 ${esc(date(row.covered_through))}</p>
      ${row.status === 'failed' ? '<div class="insights-explanation">最近一次生成失败，下面保留的是上一次成功保存的内容。</div>' : ''}
      ${row.status === 'vector_dirty' ? '<div class="insights-explanation">摘要正文已保存，检索索引仍待同步。</div>' : ''}
      <h3>项目摘要</h3>${plain(row.l1)}
      <details><summary>展开详细内容</summary>${plain(row.l2)}</details>
      <details><summary>查看来源 · ${row.sources.length} 条</summary>${sources(row.sources)}</details>`;
  }

  function workstreamDetail(row) {
    const done = ['completed', 'cancelled'].includes(row.status);
    return `${badge(label(row.status), row.status === 'completed')}<h2>${esc(row.objective || '任务断点')}</h2>
      <p class="insights-note">${esc(name(row.project))} · 保存于 ${esc(date(row.updated_at))}</p>
      ${!row.content_available ? '<div class="insights-explanation">断点正文暂不可用，请在原会话中核实任务内容。</div>' : ''}
      ${done ? '<div class="insights-explanation">这个任务已结束。查看历史记录时，无需重新执行已完成事项。</div>' : '<div class="insights-explanation">这是最近保存的进度；续接时助手还会核对当前工作区与实际状态。</div>'}
      <h3>已经完成</h3>${list(row.completed_steps)}<h3>当前步骤</h3>${plain(row.current_step)}
      <h3>下一步</h3>${plain(row.next_action)}<h3>阻塞事项</h3>${row.blockers?.length ? list(row.blockers) : '<p class="insights-note">没有记录阻塞。</p>'}
      <h3>已接受的决定</h3>${list(row.accepted_decisions)}
      ${!done && row.content_available ? '<div class="insights-copy"><button id="insights-copy-task" class="ghost">复制续接提示</button><span id="insights-copy-message" class="insights-copy-message" role="status"></span><textarea id="insights-copy-fallback" aria-label="续接提示文本" readonly hidden></textarea></div>' : ''}
      <details><summary>断点编号与来源</summary><p class="insights-ref">${esc(row.id)} · 第 ${row.checkpoint_revision} 版</p>${sources(row.sources)}</details>`;
  }

  async function select(id, scroll = true, kind = state.kind) {
    const token = ++state.detailRequest;
    state.selected = id;
    renderRows();
    $('insights-detail').innerHTML = '<div class="insights-empty">正在读取详情…</div>';
    $('insights-detail').setAttribute('aria-busy', 'true');
    try {
      const row = await get(`/api/${API[kind]}/${encodeURIComponent(id)}`);
      if (token !== state.detailRequest) return;
      $('insights-detail').innerHTML = ({experiences: experienceDetail, summaries: summaryDetail, workstreams: workstreamDetail})[kind](row);
      const copyButton = $('insights-copy-task');
      if (copyButton) copyButton.onclick = async () => {
        const prompt = `继续 ${row.project} 项目的任务“${row.objective}”。请先恢复任务断点 ${row.id}，核对当前工作区和实际完成情况，再执行剩余工作。最近记录的下一步：${row.next_action || '请查看断点内容'}。`;
        try {
          await navigator.clipboard.writeText(prompt);
          if (token !== state.detailRequest) return;
          $('insights-copy-message').textContent = '已复制，粘贴到助手对话即可';
        } catch {
          if (token !== state.detailRequest) return;
          const field = $('insights-copy-fallback');
          field.hidden = false; field.value = prompt; field.focus(); field.select();
          $('insights-copy-message').textContent = '请选择下方文字复制';
        }
      };
      if (scroll && matchMedia('(max-width: 700px)').matches) $('insights-detail').scrollIntoView({block: 'start'});
    } catch (error) {
      if (token !== state.detailRequest) return;
      $('insights-detail').innerHTML = `<div class="insights-error">${esc(error.message)}</div><button class="ghost" id="insights-detail-retry">重试详情</button>`;
      $('insights-detail-retry').onclick = () => select(id, false, kind);
    } finally {
      if (token === state.detailRequest) $('insights-detail').setAttribute('aria-busy', 'false');
    }
  }

  document.addEventListener('click', event => {
    const metric = event.target.closest('[data-insight-kind]');
    if (metric) document.dispatchEvent(new CustomEvent('evolvmem:open-insights', {detail: {kind: metric.dataset.insightKind, status: metric.dataset.insightStatus}}));
  });
  $('insights-list').onclick = event => {
    if ($('insights-list').getAttribute('aria-busy') === 'true') return;
    const entry = event.target.closest('[data-select]');
    if (entry && !entry.disabled) select(state.kind === 'workstreams' ? entry.dataset.select : Number(entry.dataset.select));
  };
  $('insights-detail').onclick = event => {
    const entry = event.target.closest('[data-open]');
    if (entry) select(Number(entry.dataset.open), true, entry.dataset.kind);
  };
  document.querySelectorAll('[data-insights-tab]').forEach(button => button.onclick = () => { setKind(button.dataset.insightsTab); loadList(); });
  ['insights-project', 'insights-status'].forEach(id => $(id).onchange = () => { state.page = 1; loadList(); });
  $('insights-query').oninput = () => {
    clearTimeout(debounce);
    ++state.request; ++state.detailRequest;
    debounce = setTimeout(() => { state.page = 1; loadList(); }, 250);
  };
  $('insights-prev').onclick = () => { --state.page; loadList(); };
  $('insights-next').onclick = () => { ++state.page; loadList(); };
  $('insights-refresh').onclick = async () => { await refreshOverview(); await loadList(); };
  setKind('experiences');
  window.EvolvMemInsights = {
    refreshOverview,
    async load() { if (!overview) await refreshOverview(); await loadList(); },
    async open(kind, status) { setKind(kind, status); $('insights-project').value = ''; if (!overview) await refreshOverview(); await loadList(); },
  };
})();
