/* Trust views: read-only sync chain, sourced project knowledge, review backlog
   and the decision temporal window. User-facing copy is Chinese; raw field
   names, reason codes and revisions live in collapsed technical details.
   All server strings pass through esc(). */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
  // Every stored timestamp is naive UTC; label it so a local reader cannot
  // mistake it for Beijing time.
  const when = value => value ? `${esc(value)} UTC` : '未知';
  const request = (path, options = {}) =>
    (window.EvolvAuth && window.EvolvAuth.fetch ? window.EvolvAuth.fetch : fetch)(path, options);

  const ERROR_LABEL = {
    item_not_found: '没有找到这条记录，请检查编号',
  };

  async function getJson(path) {
    const response = await request(path, {cache: 'no-store'});
    const data = await response.json().catch(() => null);
    if (!response.ok || !data || data.ok === false) {
      const code = data && data.error;
      throw new Error(ERROR_LABEL[code] || code || `请求失败（${response.status}）`);
    }
    return data;
  }

  async function postJson(path, body) {
    const response = await request(path, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {}),
    });
    return response.json().catch(() => ({}));
  }

  const STATE_LABEL = {
    success: '已证明', pending: '等待中', error: '失败', unknown: '未知',
    unverified: '未验证',
  };
  const STATE_CLASS = {
    success: 'good', pending: 'warn', error: 'bad', unknown: 'unknown', unverified: 'unknown',
  };
  const STAGE_LABEL = {
    client_capture: '本机采集', upload_archive: '上传与归档', extraction: '内容提炼',
    recall: '召回准备', hook_delivery: '钩子投递', model_adoption: '模型采纳',
  };
  const REASON_LABEL = {
    client_local_stage_unobservable: '本机采集发生在客户端，服务端无法观测',
    no_upload_receipt: '还没有任何上传回执',
    archive_chunks_receiving: '仍在接收归档分片',
    archive_receipts_complete: '归档回执完整',
    no_archive_to_extract: '还没有可提炼的归档',
    extraction_queued: '等待提炼',
    extraction_failed: '最近一次提炼失败',
    extraction_complete: '提炼已完成',
    extraction_not_requested: '尚未请求提炼',
    extraction_state_unavailable: '提炼状态无法确认',
    hook_retrieval_not_recorded: '服务端不记录钩子是否检索过',
    client_hook_delivery_unobservable: '钩子投递发生在客户端，服务端无法观测',
    no_live_desktop_test: '未做真实桌面会话测试',
    rollup_generation_failed: '最近一次刷新失败，下面是上一次成功内容',
    vector_index_not_synced: '正文已保存，检索索引待同步',
    held_source_ownership_or_validity: '有来源的归属或时间待确认',
    new_or_modified_source_after_coverage: '覆盖之后有新的或修改过的来源',
    covered_through_watermark: '已覆盖到最近一次刷新范围',
    rollup_pending: '等待生成摘要',
    no_trusted_source: '还没有可用的可信来源',
    project_ownership_pending: '归属待人工确认',
    project_ownership_conflict: '归属存在冲突',
    project_ownership_rejected: '归属已被拒绝',
    project_ownership_unverified: '归属未确认',
    low_confidence: '置信度不足',
    expired: '已过期',
    out_of_window: '不在生效时间窗口内',
    not_current: '当前不生效',
    item_candidate: '仍是候选，未确认',
    item_superseded: '已被替代',
    item_deleted: '已删除',
    checkpoint_missing: '当前断点内容不可用',
    project_mismatch: '项目归属不一致',
    no_eligible_source: '没有可用来源',
  };
  const FRESH_LABEL = {
    fresh: '内容已覆盖', stale: '有新来源待刷新', failed: '最近刷新失败',
    pending: '等待生成', empty: '暂无内容', index_pending: '索引待同步',
    source_hold: '有来源待确认',
  };
  const FRESH_CLASS = {
    fresh: 'good', stale: 'warn', failed: 'bad', index_pending: 'warn',
    source_hold: 'warn',
  };
  const CONTENT_LABEL = {
    decision: '决定', constraint: '约束', workflow_policy: '工作策略',
    fact: '事实', experience: '经验', session_summary: '会话摘要',
    project_summary: '项目摘要', workstream_checkpoint: '任务断点',
  };
  const OWNERSHIP_LABEL = {
    confirmed: '归属已确认', unverified: '归属未标记', excluded: '归属未确认',
  };
  const WS_STATUS_LABEL = {open: '进行中', paused: '已暂停', blocked: '有阻塞'};

  const state = {project: '', canWrite: false, knowledge: null, projects: [], window: null};

  const badge = (text, cls) => `<span class="trust-badge ${cls || ''}">${esc(text)}</span>`;
  const empty = message => `<div class="trust-empty">${esc(message)}</div>`;
  const pretty = value => esc(JSON.stringify(value, null, 2));

  // ---- project selector ----

  async function loadProjects() {
    let data;
    try {
      data = await getJson('/api/trust/projects');
    } catch (error) {
      $('trust-project').innerHTML = '<option value="">暂时无法读取项目</option>';
      return;
    }
    state.projects = (data.projects || []).filter(p => p.status === 'active');
    const options = ['<option value="">选择项目…</option>'].concat(
      state.projects.map(p => `<option value="${esc(p.project)}">${esc(p.display_name ? p.display_name + ' · ' + p.project : p.project)}</option>`),
    );
    $('trust-project').innerHTML = options.join('');
    const wanted = new URLSearchParams(location.search).get('project');
    const initial = state.project || wanted || (state.projects[0] && state.projects[0].project) || '';
    if (initial) {
      state.project = initial;
      $('trust-project').value = initial;
    }
    if (!state.projects.length) {
      $('trust-rollup').innerHTML = empty('还没有已登记的项目。');
    }
  }

  // ---- freshness summary ----

  function renderRollup(knowledge) {
    const rollup = knowledge.rollup;
    const badges = [
      badge(FRESH_LABEL[rollup.freshness] || '状态未知', FRESH_CLASS[rollup.freshness] || 'unknown'),
      rollup.needs_refresh ? badge('需要刷新', 'warn') : badge('无需刷新', 'unknown'),
    ];
    if (rollup.new_trusted_source_ids.length) badges.push(badge(`新增 ${rollup.new_trusted_source_ids.length} 条来源`, 'warn'));
    if (rollup.modified_covered_source_ids.length) badges.push(badge(`修改 ${rollup.modified_covered_source_ids.length} 条来源`, 'warn'));
    if (rollup.held_source_ids.length) badges.push(badge(`待确认来源 ${rollup.held_source_ids.length} 条`, 'warn'));
    $('trust-rollup').innerHTML = `<div class="trust-notice">
      <div class="trust-badges">${badges.join('')}</div>
      <div class="trust-item-meta" style="margin-top:10px">
        <span>内容更新：${when(knowledge.latest_progress.content_updated_at)}</span>
        <span>来源覆盖时间：${rollup.covered_through_known ? when(rollup.covered_through) : '未知'}</span>
        <span>已覆盖来源：${rollup.covered_source_ids.length} 条</span>
      </div>
      <p class="trust-note" style="margin:10px 0 0">${esc(REASON_LABEL[rollup.reason] || knowledge.latest_progress.note || '')}</p>
    </div>`;
  }

  // ---- current rules ----

  // L0 summaries may carry a raw "content_type: " prefix from the source key;
  // the badge already shows the type, so the title drops the prefix.
  const bareTitle = value => String(value ?? '').replace(/^[A-Za-z_]+:\s*/, '');

  function renderRules(section) {
    if (!section.items.length) {
      $('trust-rules').innerHTML = empty('当前没有已确认且正在生效的规则或决定。');
      return;
    }
    $('trust-rules').innerHTML = section.items.map(item => `
      <div class="trust-item">
        <p class="trust-item-title">${esc(bareTitle(item.l0) || '（无摘要）')}</p>
        <div class="trust-badges">
          ${badge(CONTENT_LABEL[item.content_type] || item.content_type, '')}
          ${badge(item.applicability === 'current_predecessor' ? '当前生效（继任尚未生效）' : '当前生效', 'good')}
          ${badge(OWNERSHIP_LABEL[item.ownership.state] || item.ownership.state, item.ownership.state === 'confirmed' ? 'good' : 'unknown')}
        </div>
        <div class="trust-item-meta">
          <span>生效时间：${when(item.effective_from)}</span>
          <span>结束时间：${when(item.effective_until)}</span>
          ${item.successor_effective_from ? `<span>继任生效：${when(item.successor_effective_from)}</span>` : ''}
          <span>记录更新：${when(item.updated_at)}</span>
        </div>
      </div>`).join('');
  }

  // ---- latest progress + current workstreams ----

  function renderProgress(progress, workstreams) {
    const parts = [];
    if (!progress.available) {
      parts.push(empty('还没有项目摘要。出现可用来源后需要一次刷新才会生成，本页不会自动调用模型。'));
    } else {
      parts.push(`
        <div class="trust-badges">
          ${badge(progress.retained ? '保留的旧内容（非最新）' : '当前摘要', progress.retained ? 'bad' : 'good')}
        </div>
        <p class="trust-item-title" style="margin-top:10px">${esc(progress.l0 || '（无一句话摘要）')}</p>
        <p class="trust-note" style="white-space:pre-wrap">${esc(progress.l1 || '（无细节摘要）')}</p>
        <div class="trust-item-meta">
          <span>内容更新：${when(progress.content_updated_at)}</span>
          <span>来源覆盖时间：${when(progress.covered_through)}</span>
        </div>
        <details class="trust-details"><summary>展开详细内容与来源编号</summary>
          <p class="trust-note" style="white-space:pre-wrap">${esc(progress.l2 || '（无详细内容）')}</p>
          <p class="trust-source">来源编号：${progress.source_ids.length ? esc(progress.source_ids.join('、')) : '未知'}</p>
        </details>`);
    }
    parts.push(renderWorkstreams(workstreams, '当前进行中的任务'));
    $('trust-progress').innerHTML = parts.join('');
  }

  function renderWorkstreams(workstreams, title) {
    const items = workstreams && workstreams.items ? workstreams.items : [];
    if (!items.length) {
      return `<h3 class="trust-subhead">${esc(title)}</h3>${empty('当前没有进行中的任务断点。')}`;
    }
    return `<h3 class="trust-subhead">${esc(title)}</h3>` + items.map(item => `
      <div class="trust-item">
        <p class="trust-item-title">${esc(item.objective || '任务断点')}</p>
        <div class="trust-badges">
          ${badge(WS_STATUS_LABEL[item.status] || item.status, item.status === 'blocked' ? 'bad' : 'warn')}
          ${badge(OWNERSHIP_LABEL[item.ownership.state] || item.ownership.state, item.ownership.state === 'confirmed' ? 'good' : 'unknown')}
        </div>
        <div class="trust-item-meta">
          <span>当前步骤：${esc(item.current_step || '未记录')}</span>
          <span>下一步：${esc(item.next_action || '未记录')}</span>
          ${item.blockers && item.blockers.length ? `<span>阻塞：${esc(item.blockers.join('；'))}</span>` : ''}
          <span>更新：${when(item.updated_at)}</span>
        </div>
      </div>`).join('');
  }

  // The open-issues card only summarizes the tasks; the full list stays in the
  // latest-progress card so the page does not render it twice.
  function renderWorkstreamsSummary(workstreams, title) {
    const items = workstreams && workstreams.items ? workstreams.items : [];
    if (!items.length) {
      return `<h3 class="trust-subhead">${esc(title)}</h3>${empty('当前没有进行中的任务断点。')}`;
    }
    const preview = items.slice(0, 3).map(item => `
      <div class="trust-item">
        <p class="trust-item-title">${esc(item.objective || '任务断点')}</p>
        <div class="trust-item-meta">
          <span>${esc(WS_STATUS_LABEL[item.status] || item.status)}</span>
          <span>更新：${when(item.updated_at)}</span>
        </div>
      </div>`).join('');
    const rest = items.length > 3 ? `<p class="trust-note">其余 ${items.length - 3} 条见「最新进展」中的完整任务列表。</p>` : '';
    return `<h3 class="trust-subhead">${esc(title)} · ${items.length} 条</h3>${preview}${rest}`;
  }

  // ---- open issues / review backlog ----

  function projectOptions(selected) {
    return state.projects.map(p => `<option value="${esc(p.project)}"${p.project === selected ? ' selected' : ''}>${esc(p.display_name ? p.display_name + ' · ' + p.project : p.project)}</option>`).join('');
  }

  function renderIssues(issues, workstreams) {
    const backlog = issues.review_backlog;
    $('trust-backlog-count').textContent = `待确认归属 ${backlog.total} 条 · 进行中任务 ${(workstreams.items || []).length} 条 · 待确认来源 ${issues.held_source_count || 0} 条`;
    const backlogRows = backlog.items.length ? backlog.items.map(row => {
      const actions = state.canWrite ? `
        <div class="trust-backlog-actions">
          <label class="trust-inline-label">归入项目
            <select class="trust-select" data-target="${esc(row.item_id)}">${projectOptions(row.current_project || state.project)}</select>
          </label>
          <button class="trust-button primary" type="button" data-confirm="${esc(row.item_id)}" data-revision="${esc(row.revision)}">确认归属</button>
          <button class="trust-button ghost" type="button" data-reject="${esc(row.item_id)}" data-revision="${esc(row.revision)}">拒绝这条归属</button>
        </div>` : '<p class="trust-note">只读账号，无法评审。</p>';
      return `<div class="trust-item">
        <p class="trust-item-title">${esc(row.value || row.key || '（无预览）')}</p>
        <div class="trust-badges">
          ${badge(row.kind === 'unreviewed' ? '没有归属记录' : '等待人工评审', 'warn')}
          ${badge(CONTENT_LABEL[row.content_type] || row.content_type, '')}
        </div>
        <div class="trust-item-meta">
          <span>当前项目：${esc(row.current_project || '未标记')}</span>
          ${row.proposed_project ? `<span>系统建议：${esc(row.proposed_project)}</span>` : ''}
          <span>更新：${when(row.updated_at)}</span>
        </div>
        ${actions}
      </div>`;
    }).join('') : empty('没有待确认归属的条目。默认知识页只使用已确认归属的内容。');

    const unfinished = renderWorkstreamsSummary(workstreams, '进行中任务');
    const heldSources = (issues.held_sources || []).length ? `
      <h3 class="trust-subhead">待确认来源</h3>
      ${issues.held_sources.map(source => `<div class="trust-item">
        <div class="trust-badges">${badge('来源内容待确认', 'warn')}</div>
        <div class="trust-item-meta"><span>来源编号 ${esc(source.id)}</span>
        <span>${esc((source.reasons || []).map(r => REASON_LABEL[r] || r).join('；'))}</span></div>
      </div>`).join('')}` : '';
    const heldWorkstreams = (issues.held_workstreams || []).length ? `
      <h3 class="trust-subhead">暂不展示的任务断点</h3>
      ${issues.held_workstreams.map(item => `<div class="trust-item">
        <div class="trust-item-meta"><span>${esc(item.id)}</span>
        <span>${esc(REASON_LABEL[item.reason] || item.reason)}</span></div>
      </div>`).join('')}` : '';
    $('trust-backlog').innerHTML = backlogRows;
    $('trust-issues').innerHTML = `${unfinished}${heldSources}${heldWorkstreams}`;
  }

  // ---- sources & technical details ----

  function renderSources(knowledge) {
    const sources = knowledge.sources || [];
    if (!sources.length) {
      $('trust-sources').innerHTML = empty('没有可列出的来源条目。');
      return;
    }
    $('trust-sources').innerHTML = sources.map(source => `
      <div class="trust-item">
        <div class="trust-badges">
          ${badge(source.covered ? '已被摘要覆盖' : '覆盖之后新增', source.covered ? 'good' : 'warn')}
          ${badge(source.trusted ? '来源可信' : '来源待确认', source.trusted ? 'good' : 'bad')}
          ${badge(CONTENT_LABEL[source.content_type] || source.content_type, '')}
        </div>
        <div class="trust-item-meta">
          <button class="trust-button ghost trust-id-link" type="button" data-source-id="${esc(source.id)}">来源 ${esc(source.id)} · 查看生效时间</button>
          <span>项目：${esc(source.project || '未知')}</span>
          <span>创建：${when(source.created_at)}</span>
          <span>更新：${when(source.updated_at)}</span>
        </div>
      </div>`).join('');
  }

  function renderTech(knowledge, sync) {
    const raw = {
      rollup: knowledge ? knowledge.rollup : null,
      current_rule_exclusions: knowledge ? knowledge.current_rules.excluded : null,
      latest_progress: knowledge ? {
        item_id: knowledge.latest_progress.item_id,
        held: knowledge.latest_progress.held,
        held_reason: knowledge.latest_progress.held_reason,
      } : null,
      workstreams_held: knowledge ? knowledge.workstreams.held : null,
      sync_stages: sync ? sync.stages.map(stage => ({
        stage: stage.stage, state: stage.state, reason: stage.reason,
        backlog: stage.backlog, evidence_at: stage.evidence_at,
      })) : null,
      model_adoption: knowledge ? knowledge.model_adoption : (sync ? sync.model_adoption : null),
    };
    $('trust-tech').innerHTML = `<details class="trust-details"><summary>技术细节（原始字段与原因码）</summary>
      <pre class="trust-pre">${pretty(raw)}</pre></details>`;
  }

  // ---- sync chain ----

  function renderSync(data) {
    $('trust-sync-asof').textContent = `读取于 ${data.as_of} UTC`;
    $('trust-sync').innerHTML = (data.stages || []).map(stage => {
      const cls = STATE_CLASS[stage.state] || 'unknown';
      const backlog = stage.backlog === null || stage.backlog === undefined ? '未知' : String(stage.backlog);
      return `<div class="trust-stage">
        <span class="trust-stage-name">${esc(STAGE_LABEL[stage.stage] || stage.stage)}</span>
        <span>${badge(STATE_LABEL[stage.state] || stage.state, cls)}</span>
        <span class="trust-stage-reason">${esc(REASON_LABEL[stage.reason] || stage.reason || '')} · 积压 ${esc(backlog)}</span>
        <span class="trust-stage-time">${stage.evidence_at ? when(stage.evidence_at) : '时间未知'}</span>
      </div>`;
    }).join('');
    $('trust-sync-notes').textContent = '桌面模型是否采纳召回内容：未验证。本页不会把连接、配置或提炼成功当作模型采纳的证据。';
  }

  // ---- knowledge load ----

  async function loadKnowledge() {
    if (!state.project) {
      $('trust-rollup').innerHTML = empty('请选择一个已登记项目。');
      $('trust-rules').innerHTML = $('trust-progress').innerHTML = $('trust-issues').innerHTML = '';
      $('trust-backlog-count').textContent = '';
      return;
    }
    $('trust-generated').textContent = `正在读取 ${state.project} …`;
    try {
      const knowledge = await getJson(`/api/knowledge?project=${encodeURIComponent(state.project)}`);
      state.knowledge = knowledge;
      $('trust-generated').textContent = `${knowledge.display_name || knowledge.project} · 读取于 ${knowledge.generated_at} UTC`;
      renderRollup(knowledge);
      renderRules(knowledge.current_rules);
      renderProgress(knowledge.latest_progress, knowledge.workstreams);
      renderIssues(knowledge.open_issues, knowledge.workstreams);
      renderSources(knowledge);
      renderTech(knowledge, state.sync);
    } catch (error) {
      $('trust-generated').innerHTML = `<span class="trust-error">${esc(error.message)}</span>`;
      $('trust-rollup').innerHTML = empty('知识页暂时无法读取，请点击刷新重试。');
    }
  }

  async function loadSync() {
    try {
      state.sync = await getJson('/api/sync-chain');
      renderSync(state.sync);
      renderTech(state.knowledge, state.sync);
    } catch (error) {
      $('trust-sync').innerHTML = empty('同步状态暂时无法读取，请稍后重试。');
      $('trust-sync-asof').textContent = '';
    }
  }

  // ---- review actions ----

  async function review(itemId, revision, action, targetProject) {
    $('trust-generated').textContent = action === 'accept' ? '正在确认归属…' : '正在拒绝归属…';
    const body = action === 'accept'
      ? {project: targetProject, expected_revision: revision}
      : {expected_revision: revision};
    const result = await postJson(`/api/resolutions/${encodeURIComponent(itemId)}/${action}`, body);
    if (!result.ok) {
      const code = result.error || 'unknown_error';
      const note = code === 'workstream_project_mismatch'
        ? '该工作流已绑定到其他项目，不能跨项目移动。'
        : code === 'revision_conflict' ? '这条记录已被其他人评审，请刷新后重试。'
        : code === 'identity_conflict' ? '身份唯一索引冲突，未做任何移动。'
        : code === 'invalid_project' ? '请选择一个已登记的项目。'
        : `操作未完成（${code}）。`;
      $('trust-generated').innerHTML = `<span class="trust-error">${esc(note)}</span>`;
      return;
    }
    await loadKnowledge();
  }

  // ---- decision window ----

  const WINDOW_FIELDS = {from: 'effective_from', until: 'effective_until', occurred: 'occurred_at', mentioned: 'mentioned_at'};
  const WINDOW_LABEL = {from: '生效开始', until: '生效结束', occurred: '发生时间', mentioned: '提及时间'};

  function renderWindow(payload) {
    $('trust-window-form').hidden = false;
    $('trust-window-actions').hidden = !state.canWrite;
    for (const [key, field] of Object.entries(WINDOW_FIELDS)) {
      $(`trust-wf-${key}`).value = payload[field] || '';
    }
    $('trust-window-readout').innerHTML = `<div class="trust-badges">
        ${badge(`记录 ${payload.id}`, 'unknown')}
        ${badge(payload.temporal_state === 'known' ? '时间已知' : '时间未知', payload.temporal_state === 'known' ? 'good' : 'warn')}
      </div>
      <div class="trust-item-meta" style="margin-top:10px">
        <span>生效开始：${when(payload.effective_from)}</span>
        <span>生效结束：${when(payload.effective_until)}</span>
        <span>发生时间：${when(payload.occurred_at)}</span>
        <span>提及时间：${when(payload.mentioned_at)}</span>
      </div>
      <p class="trust-note">时间以 UTC 保存和显示，不是北京时间；输入可带时区偏移（如 +08:00）或 Z，保存时换算为 UTC。留空表示未知，不会用记录创建时间推断事件时间。</p>
      <details class="trust-details"><summary>技术细节（原始字段）</summary>
        <pre class="trust-pre">${pretty(payload)}</pre></details>`;
  }

  async function loadWindow() {
    const itemId = Number($('trust-window-id').value);
    if (!Number.isInteger(itemId) || itemId < 1) {
      $('trust-window-readout').innerHTML = empty('请输入正整数记录编号。');
      return;
    }
    try {
      const payload = await getJson(`/api/decision-window?id=${itemId}`);
      state.window = payload;
      renderWindow(payload);
    } catch (error) {
      state.window = null;
      $('trust-window-form').hidden = true;
      $('trust-window-actions').hidden = true;
      $('trust-window-readout').innerHTML = `<div class="trust-error">${esc(error.message)}</div>`;
    }
  }

  async function saveWindow() {
    if (!state.window) return;
    const body = {id: state.window.id};
    for (const [key, field] of Object.entries(WINDOW_FIELDS)) {
      const value = $(`trust-wf-${key}`).value.trim();
      body[field] = value === '' ? null : value;
    }
    const result = await postJson('/api/decision-window', body);
    if (!result.ok) {
      $('trust-window-readout').innerHTML = `<div class="trust-error">保存失败，请检查时间格式后重试。</div>`;
      return;
    }
    state.window = result;
    renderWindow(result);
  }

  // ---- events ----

  $('trust-project').addEventListener('change', event => {
    state.project = event.target.value;
    const url = new URL(location.href);
    if (state.project) url.searchParams.set('project', state.project); else url.searchParams.delete('project');
    history.replaceState(null, '', url);
    loadKnowledge();
  });
  $('trust-refresh').addEventListener('click', () => { loadKnowledge(); loadSync(); });
  $('trust-backlog').addEventListener('click', event => {
    const confirm = event.target.closest('[data-confirm]');
    if (confirm) {
      const itemId = confirm.dataset.confirm;
      const select = $('trust-backlog').querySelector(`[data-target="${CSS.escape(itemId)}"]`);
      review(itemId, Number(confirm.dataset.revision), 'accept', select ? select.value : state.project);
      return;
    }
    const reject = event.target.closest('[data-reject]');
    if (reject) review(reject.dataset.reject, Number(reject.dataset.revision), 'reject');
  });
  $('trust-window-load').addEventListener('click', loadWindow);
  $('trust-window-save').addEventListener('click', saveWindow);
  $('trust-sources').addEventListener('click', event => {
    const button = event.target.closest('[data-source-id]');
    if (!button) return;
    $('trust-window-id').value = button.dataset.sourceId;
    loadWindow();
    $('trust-window-readout').scrollIntoView({block: 'center'});
  });

  (async () => {
    if (window.EvolvAuth && window.EvolvAuth.ready) {
      try { await window.EvolvAuth.ready; } catch { return; }
      state.canWrite = window.EvolvAuth.canWrite === true;
    }
    await loadProjects();
    await Promise.all([loadSync(), loadKnowledge()]);
  })();
})();
