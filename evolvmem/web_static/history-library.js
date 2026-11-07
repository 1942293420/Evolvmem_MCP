/* Read-only history library inside the existing EvolvMem console.
 *
 * The view consumes the owner-gated /api/history-candidate endpoints. Raw
 * history is rendered as text through textContent-safe escaping; no HTML from
 * stored messages is executed, no external images are fetched, and no token is
 * stored in the page.
 */
(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch]);
  let API = '/api/history-candidate', attachmentBase = '/history/attachment';
  let storageKey = 'evolvmem.history.', canManage = true, inspecting = false;
  const storage = {getItem:key=>sessionStorage.getItem(storageKey+key.replace('evolvmem.history.','')),setItem:(key,value)=>sessionStorage.setItem(storageKey+key.replace('evolvmem.history.',''),value),removeItem:key=>sessionStorage.removeItem(storageKey+key.replace('evolvmem.history.',''))};
  const COVERAGE = {
    whole_payload_observed: '整份载荷已读取（未证明完整）',
    captured_ranges_contiguous: '增量区间连续（起点未证明）',
    captured_ranges_with_gaps: '增量区间存在缺口',
    no_visible_dialogue: '无可见问答',
    partial_source_failures: '部分来源失败',
  };
  const coverage = value => COVERAGE[value] || value || '覆盖未知';
  const numeric = value => {
    const n = Number(value);
    return Number.isFinite(n) && n >= 0 ? n : null;
  };
  const messageText = row => {
    const n = numeric(row && row.messages);
    return n === null ? '' : `${n} 条消息`;
  };
  const turnText = row => {
    const n = numeric(row && row.turns);
    return n === null ? '' : `${n} 轮问答`;
  };
  const joinMeta = (...parts) => parts.filter(Boolean).join(' · ');
  const bytes = value => {
    let n = Number(value || 0);
    for (const unit of ['B', 'KB', 'MB', 'GB']) {
      if (n < 1024 || unit === 'GB') return `${unit === 'B' ? n.toFixed(0) : n.toFixed(1)} ${unit}`;
      n /= 1024;
    }
    return `${n.toFixed(1)} GB`;
  };
  const decode = value => {
    try { return decodeURIComponent(value || ''); } catch (_) { return value || ''; }
  };
  const hashForProject = key => `#history/project/${encodeURIComponent(key)}`;
  const hashForBlock = (context, blockId, page) => context.kind === 'project'
    ? `#history/project/${encodeURIComponent(context.key)}/block/${encodeURIComponent(blockId)}/${page}`
    : `#history/unassigned/block/${encodeURIComponent(blockId)}/${page}`;
  const state = {
    mode: 'projects', projects: null, unassigned: null,
    projectCache: new Map(), query: '', request: 0, blockRequest: 0,
    queries: {projects: '', unassigned: ''},
    projectFilter: 'all', unassignedFilter: 'all', returnScroll: null,
    gapSummary: null, gapGroup: 'projects',
    gapData: {projects: null, sources: null}, gapPages: {projects: 1, sources: 1},
    gapQueries: {projects: '', sources: ''}, gapLoading: {projects: false, sources: false},
    gapErrors: {projects: null, sources: null}, gapSeq: {projects: 0, sources: 0},
    gapSourceDetail: null, gapSourceLoading: false, gapSourceError: null,
  };
  let searchTimer = null;

  try {
    const legacyQuery = storage.getItem('evolvmem.history.query') || '';
    state.queries.projects = storage.getItem('evolvmem.history.projectsQuery') || legacyQuery;
    state.queries.unassigned = storage.getItem('evolvmem.history.unassignedQuery') || '';
    state.projectFilter = storage.getItem('evolvmem.history.projectFilter') || 'all';
    state.unassignedFilter = storage.getItem('evolvmem.history.unassignedFilter') || 'all';
  } catch (_) { state.queries = {projects: '', unassigned: ''}; }

  function parseHash() {
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    if (parts[0] !== 'history') return { mode: 'projects' };
    if (parts[1] === 'unassigned') {
      if (parts[2] === 'block') {
        return { mode: 'unassigned', block: decode(parts[3]), page: Math.max(1, Number(parts[4]) || 1), message: parts[5] === 'm' ? Math.max(0,Number(parts[6])||0) : null };
      }
      if (parts[2] === 'source') {
        return { mode: 'unassigned', unassignedSource: decode(parts[3]) };
      }
      return { mode: 'unassigned' };
    }
    if (parts[1] === 'project' && parts[2]) {
      const project = decode(parts[2]);
      if (parts[3] === 'block') {
        return { mode: 'projects', project, block: decode(parts[4]), page: Math.max(1, Number(parts[5]) || 1), message: parts[6] === 'm' ? Math.max(0,Number(parts[7])||0) : null };
      }
      return { mode: 'projects', project };
    }
    if (parts[1] === 'gaps') {
      if (parts[2] === 'source') return { mode: 'gaps', group: 'sources', gapSource: decode(parts[3]) };
      if (parts[2] === 'sources') return { mode: 'gaps', group: 'sources' };
      if (parts[2] === 'projects') return { mode: 'gaps', group: 'projects' };
      return { mode: 'gaps', group: 'projects', gapDefault: true };
    }
    return { mode: 'projects' };
  }

  function navigate(hash) {
    if (location.hash === hash) route();
    else location.hash = hash;
  }

  async function getJSON(path) {
    if (inspecting && path.startsWith(API)) path += (path.includes('?') ? '&' : '?') + 'inspect=1';
    const response = await fetch(path, { cache: 'no-store', credentials: 'same-origin' });
    let data = null;
    try { data = await response.json(); } catch (_) { data = null; }
    if (!response.ok) {
      const error = new Error(
        response.status === 401 || response.status === 403
          ? '登录已失效，或你没有读取此用户历史的权限。'
          : response.status === 410 ? '资料已移至回收站，可恢复后查看。' : response.status === 404 ? '该记录不存在或已被移除。' : '暂时无法读取历史资料，请稍后重试。');
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function setBusy(busy) {
    const main = $('history-main');
    if (main) main.setAttribute('aria-busy', busy ? 'true' : 'false');
  }

  function setNote(text) {
    const note = $('history-list-note');
    if (note) note.textContent = text || '';
  }

  function renderModes() {
    document.querySelectorAll('[data-history-mode]').forEach(button => {
      button.setAttribute('aria-pressed', button.dataset.historyMode === state.mode ? 'true' : 'false');
    });
  }

  function syncToolbar() {
    const select = $('history-status-filter');
    const search = $('history-search');
    if (!select) return;
    const project = state.mode === 'projects';
    const options = project
      ? [['all', '全部项目'], ['with_history', '有原文'], ['without_history', '暂无原文']]
      : [['all', '全部来源'], ['dialogue', '有问答'], ['empty', '空来源']];
    select.innerHTML = options.map(([value, label]) =>
      `<option value="${value}">${label}</option>`).join('');
    select.value = project ? state.projectFilter : state.unassignedFilter;
    if (![...select.options].some(option => option.value === select.value)) {
      select.value = 'all';
      if (project) state.projectFilter = 'all'; else state.unassignedFilter = 'all';
    }
    if (search) {
      search.placeholder = project ? '搜索项目、摘要、问题或回答' : '搜索会话标题、问题或回答';
      search.value = state.query;
    }
  }

  function emptyRow(text) {
    return `<div class="history-empty"><p>${esc(text)}</p></div>`;
  }

  function projectIsPartial(row) {
    return Boolean(row && (row.partial || row.partial_original || row.recovery?.partial ||
      row.recovery_status === 'partial' || row.recovery_status === '部分恢复' ||
      row.coverage_status === 'partial' || row.recovered_status === 'partial'));
  }

  function projectLifecycleLabel(value) {
    return ({active: '进行中', archived: '已归档'}[String(value || '').toLowerCase()] || '状态未标注');
  }

  function evidenceStatusLabel(value) {
    return ({
      documented: '资料较完整',
      partial: '部分资料',
      insufficient: '资料不足',
      task_only: '仅任务记录',
      recovered_original: '已恢复原文',
      recovered_partial_missing_answers: '部分恢复，回答缺失',
    }[String(value || '').toLowerCase()] || '资料状态待核对');
  }

  const selection = new Map();
  let managing = false, visibleTargets = [], searchSeq = 0;
  let searchJob = null, searchPage = 1, pendingDelete = [];
  const trashIcon = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/></svg>';
  const targetId = t => JSON.stringify(t);
  const activeFilter = () => state.mode === 'projects' ? state.projectFilter : state.unassignedFilter;
  const searchKey = () => JSON.stringify([state.mode, state.query.trim(), activeFilter(), searchPage]);
  const deletionButton = (target, title, label = '') => !canManage ? '' : `<button type="button" class="history-delete ${label ? 'history-delete-text' : ''}" data-history-delete="${esc(targetId(target))}" data-delete-title="${esc(title)}" aria-label="删除${esc(title)}" title="移至回收站">${trashIcon}${label ? esc(label) : ''}</button>`;
  function managedRow(button, target, title, selectable = true) {
    if (selectable) visibleTargets.push({target, title});
    return `<div class="history-managed-row">${managing && selectable ? `<input class="history-select" type="checkbox" data-history-select="${esc(targetId(target))}" data-select-title="${esc(title)}" aria-label="选择${esc(title)}" ${selection.has(targetId(target)) ? 'checked' : ''}>` : ''}${button}${deletionButton(target, title)}</div>`;
  }
  function highlight(text) {
    const s = String(text || ''), q = state.query.trim();
    const p = q ? s.toLowerCase().indexOf(q.toLowerCase()) : -1;
    return p < 0 ? esc(s) : `${esc(s.slice(0,p))}<mark>${esc(s.slice(p,p+q.length))}</mark>${esc(s.slice(p+q.length))}`;
  }
  function renderManageBar() {
    const bar = $('history-manage-bar');
    if (!bar) return;
    $('history-manage').textContent = managing ? '退出管理' : '批量管理';
    bar.hidden = !managing;
    bar.innerHTML = `<label class="history-select-all"><input type="checkbox" id="history-select-all" ${visibleTargets.length && visibleTargets.every(x => selection.has(targetId(x.target))) ? 'checked' : ''}>全选当前结果</label><span>已选 ${selection.size} 项</span><button type="button" class="history-danger" id="history-delete-selected" ${selection.size ? '' : 'disabled'}>删除所选</button>`;
  }
  async function refreshSearch(reset = true) {
    if (reset) searchPage = 1;
    const seq = ++searchSeq;
    if (!state.query.trim()) { searchJob = null; renderList(); return; }
    const key = searchKey();
    searchJob = {key, busy: true}; renderList();
    try {
      const data = await getJSON(`${API}/search?scope=${state.mode}&q=${encodeURIComponent(state.query.trim())}&status=${encodeURIComponent(activeFilter())}&page=${searchPage}&page_size=50`);
      if (seq !== searchSeq || key !== searchKey()) return;
      searchJob = {key, data};
    } catch (error) {
      if (seq !== searchSeq || key !== searchKey()) return;
      searchJob = {key, error: error.message};
    }
    renderList();
  }
  function renderList() {
    const list = $('history-list');
    if (!list) return;
    syncToolbar(); visibleTargets = [];
    let rows;
    const searching = !!state.query.trim();
    if (searching) {
      if (!searchJob || searchJob.key !== searchKey() || searchJob.busy) {
        list.innerHTML = emptyRow('正在搜索项目摘要、问题和回答…');
        setNote('正在搜索正文…'); renderManageBar(); return;
      }
      if (searchJob.error) { list.innerHTML = emptyRow(searchJob.error); setNote('搜索失败，请修改关键词或刷新重试。'); renderManageBar(); return; }
      rows = searchJob.data.items;
      setNote(`找到 ${searchJob.data.total} 项 · 搜索范围：名称、摘要和问答正文`);
    } else if (state.mode === 'projects') {
      const all = state.projects || [];
      rows = all.filter(r => state.projectFilter === 'all' || (state.projectFilter === 'with_history' ? r.dialogue_blocks > 0 : r.dialogue_blocks === 0));
      setNote(`${rows.length} / ${all.length} 个项目 · 有原文 ${all.filter(r => r.dialogue_blocks > 0).length} · 暂无原文 ${all.filter(r => !r.dialogue_blocks).length}`);
    } else {
      const data = state.unassigned;
      if (!data) { list.innerHTML = emptyRow('正在读取…'); return; }
      rows = [...data.dialogue.map(r => ({...r,__kind:'dialogue'})), ...data.empty.map(r => ({...r,__kind:'empty'}))]
        .filter(r => state.unassignedFilter === 'all' || state.unassignedFilter === r.__kind);
      setNote(`${rows.length} 项 · ${data.dialogue_count} 段原文 · ${data.empty_count} 个空来源`);
    }
    list.innerHTML = rows.map(row => {
      const project = state.mode === 'projects', title = project ? (row.display_name || row.project) : (row.title || row.block_id);
      const target = project ? {kind:'project',project:row.project,key:row.project} : {kind:'block',project:'',key:row.block_id};
      const context = project ? {kind:'project',key:row.project} : {kind:'unassigned'};
      const match = row.match;
      const hash = match?.block_id ? hashForBlock(context,match.block_id,match.page || 1) + (Number.isInteger(match.ordinal) ? `/m/${match.ordinal}` : '')
        : project ? hashForProject(row.project) : row.__kind === 'empty' ? `#history/unassigned/source/${encodeURIComponent(row.block_id)}` : hashForBlock(context,row.block_id,1);
      const meta = project ? `${row.dialogue_blocks ? (projectIsPartial(row) ? '部分原文' : '有原文') : '暂无原文'} · ${row.dialogue_blocks} 段原文 · ${row.messages} 条消息`
        : row.__kind === 'empty' ? '空来源 · 仅保留来源元数据' : `${messageText(row)} · ${turnText(row)}`;
      const button = `<button type="button" class="history-entry" data-hash="${esc(hash)}" aria-current="${project ? parseHash().project === row.project : parseHash().block === row.block_id}"><span class="history-entry-title">${highlight(title)}</span><span class="history-entry-meta">${esc(meta)}</span>${match ? `<span class="history-match">${match.role === 'user' ? '问题：' : match.role === 'assistant' ? '回答：' : ''}${highlight(match.snippet)}</span>` : ''}</button>`;
      return managedRow(button,target,title);
    }).join('') || emptyRow('没有匹配的资料。');
    if (searching && searchJob.data.pages > 1) list.innerHTML += `<div class="history-pager"><button type="button" data-search-page="${searchPage-1}" ${searchPage<=1?'disabled':''}>上一页</button><span>${searchPage} / ${searchJob.data.pages}</span><button type="button" data-search-page="${searchPage+1}" ${searchPage>=searchJob.data.pages?'disabled':''}>下一页</button></div>`;
    renderManageBar();
  }
  function toast(message, error = false) {
    const el = $('history-feedback'); el.hidden = false; el.textContent = message;
    el.classList.toggle('is-error',error);
  }
  function openDelete(entries) {
    if (!entries.length) return;
    pendingDelete = entries;
    const dialog = $('history-delete-dialog');
    $('history-delete-summary').innerHTML = `<p>将以下 ${entries.length} 项移至回收站：</p><ul>${entries.slice(0,8).map(x=>`<li>${esc(x.title)}</li>`).join('')}</ul>${entries.length>8?`<p>另 ${entries.length-8} 项</p>`:''}<p class="insights-note">可在回收站恢复。删除项目或项目内会话时，其他项目共用的原文仍会保留。</p>`;
    $('history-delete-error').textContent = '';
    if (!dialog.open) dialog.showModal();
    $('history-delete-cancel').focus();
  }
  let requestCounter = 0;
  const requestNonce = Math.random().toString(36).slice(2);
  function createRequestId() {
    // This is an idempotency key, not a credential. LAN HTTP may lack randomUUID.
    let api;
    try { api = globalThis.crypto; } catch (_) { /* optional browser API */ }
    if (typeof api?.randomUUID === 'function') {
      try { return api.randomUUID(); } catch (_) { /* use the portable fallback */ }
    }
    let random = '';
    if (typeof api?.getRandomValues === 'function') {
      try {
        const bytes = api.getRandomValues(new Uint8Array(16));
        random = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
      } catch (_) { /* some browser environments disable Web Crypto entirely */ }
    }
    return `history-${Date.now().toString(36)}-${requestNonce}-${(++requestCounter).toString(36)}-${random || Math.random().toString(36).slice(2)}`;
  }
  async function mutate(action, targets) {
    if (!canManage || inspecting) throw new Error('当前知识库为只读，无法管理。');
    const auth = await getJSON('/api/auth/me');
    const response = await fetch(`${API}/management/${action}`, {method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':auth.csrf_token || '', 'X-EvolvMem-Action':'history-manage'},body:JSON.stringify({targets,request_id:createRequestId()})});
    const data = await response.json();
    if (!response.ok) throw new Error(response.status===403||response.status===401 ? '登录已失效或没有管理权限，请重新登录。' : '操作未完成，请刷新资料后重试。');
    return data;
  }
  async function reloadAfterChange(deleted = []) {
    ++searchSeq; searchJob = null; selection.clear();
    state.projects = null; state.unassigned = null; state.projectCache.clear();
    state.gapSourceDetail = null; state.gapData = {projects:null,sources:null}; state.gapSummary=null;
    const current = parseHash();
    if (deleted.some(t => t.kind==='project' && t.key===current.project)) history.replaceState(null,'','#history');
    else if (deleted.some(t => t.key===(current.block||current.unassignedSource))) history.replaceState(null,'',current.project?hashForProject(current.project):'#history/unassigned');
    await route();
  }
  async function loadTrash() {
    const box = $('history-trash-items');
    box.innerHTML = '<p>正在读取回收站…</p>';
    try {
      const data = await getJSON(`${API}/management/trash`);
      box.innerHTML = data.items.map(item => `<div class="history-trash-row"><div><strong>${esc(item.title)}</strong><p>${esc(({project:'项目',project_block:'项目内会话',block:'未归属来源'})[item.kind])}${item.project && item.kind!=='project'?` · ${esc(item.project)}`:''} · ${esc(gapTime(item.deleted_at))}</p></div><button type="button" class="ghost" data-history-restore="${esc(targetId({kind:item.kind,project:item.project,key:item.key}))}">恢复</button></div>`).join('') || '<p class="insights-note">回收站为空。</p>';
    } catch (error) { box.innerHTML = `<p role="alert">${esc(error.message)}</p>`; }
  }
  function bindManagement() {
    const shell = document.querySelector('.history-shell');
    shell.addEventListener('click', async event => {
      const del = event.target.closest('[data-history-delete]');
      if (del) { openDelete([{target:JSON.parse(del.dataset.historyDelete),title:del.dataset.deleteTitle}]); return; }
      const restore = event.target.closest('[data-history-restore]');
      if (restore) {
        restore.disabled=true;
        try { await mutate('restore',[JSON.parse(restore.dataset.historyRestore)]); await reloadAfterChange(); await loadTrash(); toast('资料已恢复。'); }
        catch(error){toast(error.message,true);restore.disabled=false;}
        return;
      }
      const page = event.target.closest('[data-search-page]');
      if (page && !page.disabled) { searchPage=Number(page.dataset.searchPage); selection.clear(); await refreshSearch(false); }
      if (event.target.id==='history-delete-selected') openDelete([...selection.values()]);
    });
    shell.addEventListener('change', event => {
      if(event.target.matches('[data-history-select]')) {
        const target=JSON.parse(event.target.dataset.historySelect), id=targetId(target);
        if(event.target.checked)selection.set(id,{target,title:event.target.dataset.selectTitle});else selection.delete(id);
        renderManageBar();
      }
      if(event.target.id==='history-select-all') {
        visibleTargets.forEach(x=>{if(event.target.checked)selection.set(targetId(x.target),x);else selection.delete(targetId(x.target));});renderList();
      }
    });
    $('history-manage').onclick=()=>{managing=!managing;selection.clear();renderList();};
    $('history-trash').onclick=()=>{ $('history-trash-dialog').showModal();loadTrash(); };
    $('history-trash-close').onclick=()=> $('history-trash-dialog').close();
    $('history-delete-cancel').onclick=()=> $('history-delete-dialog').close();
    $('history-delete-confirm').onclick=async()=>{
      const button=$('history-delete-confirm');button.disabled=true;
      const targets=pendingDelete.map(x=>x.target);
      try { const result=await mutate('delete',targets); $('history-delete-dialog').close();await reloadAfterChange(targets);toast(`已将 ${result.count} 项移至回收站，可随时恢复。`); }
      catch(error){$('history-delete-error').textContent=error.message;}
      finally{button.disabled=false;}
    };
  }


  function ul(values, fallback = '未记录') {
    const list = (values || []).filter(value => String(value || '').trim());
    return list.length ? `<ul>${list.map(value => `<li>${esc(value)}</li>`).join('')}</ul>`
      : `<p class="insights-note">${esc(fallback)}</p>`;
  }

  function conflictBlock(conflicts) {
    if (!conflicts || !conflicts.length) return '';
    return `<section class="history-conflict" aria-label="来源范围冲突">
      <h3>来源范围冲突（仅作证据参考，不合并、不改历史归属）</h3>
      <ul>${conflicts.map(ref => {
        const tags = [];
        if (ref.scope_conflict) tags.push('平台范围冲突');
        if (ref.attribution_conflict) tags.push('归属冲突');
        const title = ref.title || ref.source || ref.result || '';
        return `<li><strong>${esc(tags.join(' / ') || '来源说明')}</strong>：${esc(title)}${ref.note ? `——${esc(ref.note)}` : ''}</li>`;
      }).join('')}</ul>
    </section>`;
  }

  function historyButtons(rows, context) {
    return rows.length ? `<div class="history-items">${rows.map(row => {
      const button = `<button type="button" class="history-entry" data-hash="${esc(hashForBlock(context,row.block_id,1))}"><span class="history-entry-title">${esc(row.title || '（无标题）')}</span><span class="history-entry-meta">${esc(row.first_at_beijing || '时间缺失')} · ${esc(joinMeta(messageText(row),turnText(row)))}</span></button>`;
      return managedRow(button,{kind:context.kind==='project'?'project_block':'block',project:context.key||'',key:row.block_id},row.title || row.block_id,false);
    }).join('')}</div>` : '<p class="insights-note">当前没有可读的问答会话。</p>';
  }

  function renderOverview(route) {
    const main = $('history-main');
    if (!main) return;
    if (route.mode === 'unassigned') {
      const data = state.unassigned;
      if (!data) { main.innerHTML = emptyRow('正在读取未归属资料…'); return; }
      const all = [...(data.dialogue || []), ...(data.empty || [])];
      const selected = state.unassignedFilter === 'all' ? all.length
        : state.unassignedFilter === 'dialogue' ? data.dialogue_count : data.empty_count;
      const filterLabel = state.unassignedFilter === 'all' ? '全部来源'
        : state.unassignedFilter === 'dialogue' ? '有问答' : '空来源';
      main.innerHTML = `
        <p class="insights-kicker">Unassigned / 未归属资料</p>
        <h2>未归属资料</h2>
        <p class="insights-lede">没有可靠项目归属的会话与空来源，统一在这里按原文或来源元数据读取；不伪造项目、总体用途或环境。</p>
        <p class="history-gap-meta">当前筛选：${esc(filterLabel)} · ${selected} 项 · 段原文 ${data.dialogue_count} · 空来源 ${data.empty_count}</p>
        <p class="insights-note">在左侧同一列表中选择会话或空来源；搜索和范围筛选会保留。</p>`;
      return;
    }
    const projects = state.projects || [];
    const withHistory = projects.filter(row => row.dialogue_blocks > 0);
    const archived = projects.filter(row => row.status === 'archived');
    const emptySources = projects.reduce((sum, row) => sum + (row.empty_sources || 0), 0);
    const withoutHistory = projects.length - withHistory.length;
    main.innerHTML = `
      <p class="insights-kicker">Projects / 项目资料</p>
      <h2>项目资料</h2>
      <p class="insights-lede">全部登记项目；从项目摘要进入历史原文。仅 owner 可读原文与图片。</p>
      <div class="history-metrics">
        <div><strong>${projects.length}</strong><span>项目登记（含已归档）</span></div>
        <div><strong>${withHistory.length}</strong><span>有原文</span></div>
        <div><strong>${archived.length}</strong><span>已归档</span></div>
        <div><strong>${emptySources}</strong><span>空源（来源检查）</span></div>
      </div>
      <h3>最近有原文的项目</h3>
      <div class="history-items">${withHistory.slice(0, 6).map(row => `
        <button type="button" class="history-entry" data-hash="${esc(hashForProject(row.project))}">
          <span class="history-entry-title">${esc(row.display_name || row.project)}</span>
          <span class="history-entry-meta">${row.dialogue_blocks} 段原文 · ${esc(joinMeta(messageText(row), turnText(row)))}</span>
        </button>`).join('') || '<p class="insights-note">暂无可读项目。</p>'}</div>
      <p class="insights-note">完整项目列表在左侧；${withoutHistory} 个暂无原文的项目仍保留摘要与登记入口。</p>`;
  }

  async function ensureData(mode) {
    if (mode === 'unassigned') {
      if (!state.unassigned) state.unassigned = await getJSON(`${API}/unassigned`);
      return;
    }
    if (!state.projects) {
      const data = await getJSON(`${API}/projects`);
      state.projects = data.projects || [];
    }
  }

  async function getProject(key) {
    if (!state.projectCache.has(key)) {
      state.projectCache.set(key, await getJSON(`${API}/project/${encodeURIComponent(key)}`));
    }
    return state.projectCache.get(key);
  }

  function renderProject(data) {
    const main = $('history-main');
    const summary = data.summary || {};
    const recoveryLabel = (projectIsPartial(data) || (data.history || []).some(row =>
      /partial|user_only|incomplete/.test(String(row.coverage || row.recovery_status || '')))) ? '部分原文'
      : (data.history || []).length ? '有原文' : '暂无原文';
    main.innerHTML = `
      <p class="insights-kicker">Project / 项目资料</p>
      <div class="history-detail-heading"><h2>${esc(data.display_name || data.project)}</h2>${deletionButton({kind:"project",project:data.project,key:data.project},data.display_name || data.project,"删除项目")}</div>
      <p class="history-submeta">${esc(data.project)} · ${esc(projectLifecycleLabel(data.status))} · ${esc(recoveryLabel)} · 证据：${esc(evidenceStatusLabel(data.evidence_status))}</p>
      <section class="history-summary" id="history-project-summary">
        <h3>项目摘要</h3>
        <p class="history-purpose">${esc(summary.purpose || '暂无总体介绍。')}</p>
        <nav class="history-jump" aria-label="项目页快捷跳转">
          <button type="button" data-scroll-target="history-project-history">查看简短历史 ↓</button>
          <button type="button" data-scroll-target="history-summary-extra">摘要详情</button>
        </nav>
        <details class="history-summary-extra" id="history-summary-extra">
          <summary>环境、功能、必要信息、缺项与历史备注</summary>
          <h4>环境与入口</h4>${ul(summary.environment, '来源未提供可核对的环境信息。')}
          <h4>主要功能 / 业务范围</h4>${ul(summary.main_features, '来源不足，未确认主要功能。')}
          <h4>必要约定（不含真实密钥）</h4>${ul(summary.necessary_info, '暂无。')}
          <h4>未核实 / 缺项</h4>${ul(summary.unknowns, '暂无额外缺项。')}
          <h4>历史记录（历史时点，不作为当前状态）</h4>${ul(summary.historical_notes, '无单独的历史时点记录。')}
        </details>
      </section>
      ${conflictBlock(summary.source_conflict_refs)}
      <section class="history-summary" id="history-project-history">
        <h3>简短历史</h3>
        ${historyButtons(data.history || [], { kind: 'project', key: data.project })}
        ${data.empty_source_count ? `<p class="insights-note">另有 ${data.empty_source_count} 个无可见问答的来源，仅保留在来源检查中。</p>` : ''}
      </section>
      <details class="history-source">
        <summary>来源依据与边界（${(summary.source_refs || []).filter(ref => ref.kind !== 'registry').length} 条）</summary>
        <ul>${(summary.source_refs || []).filter(ref => ref.kind !== 'registry').map(ref =>
          `<li>${esc(ref.kind || '来源')}：${esc(ref.title || ref.source || ref.result || '')}</li>`).join('')}</ul>
        <p>会话结束时间未知；项目关联不扩大原会话读取权限；不展示工具调用或隐藏推理。</p>
      </details>
      ${(!(data.history || []).length || projectIsPartial(data))
        ? `<section class="history-audit" id="project-audit" aria-label="项目备份核查"><p class="history-audit-status">正在读取备份核查状态…</p></section>`
        : ''}`;
    if (!(data.history || []).length || projectIsPartial(data)) loadProjectAudit(data.project);
  }

  async function loadProjectAudit(key) {
    const slot = document.getElementById('project-audit');
    if (!slot) return;
    try {
      const data = await getJSON(`${API}/coverage/audit/projects/${encodeURIComponent(key)}`);
      const audit = data.backup_audit || {};
      if (audit.status === 'pending_acceptance' || !audit.status) {
        slot.innerHTML = `<p class="history-audit-status">备份核查：${esc(audit.message || '结果待验收')}</p>`;
        return;
      }
      if (audit.status === 'invalid') {
        slot.innerHTML = '<p class="history-audit-status">备份核查：结果格式无效</p>';
        return;
      }
      slot.innerHTML = `<p class="history-audit-status">备份核查：<span>${esc(audit.status_label || '尚无法核实')}</span></p>
        <details class="history-source"><summary>备份核查详情</summary>
          <p>检查时间：${esc(gapTime(audit.checked_at))}</p>
          ${audit.conclusion ? `<p>结论：${esc(audit.conclusion)}</p>` : ''}
          ${audit.evidence ? `<p>证据：${esc(audit.evidence)}</p>` : ''}
          ${audit.scope ? `<p>范围：${esc(JSON.stringify(audit.scope).slice(0, 1600))}</p>` : ''}
          ${audit.limitations && audit.limitations.length ? `<p>限制：${esc(audit.limitations.join('；').slice(0, 1600))}</p>` : ''}
        </details>`;
    } catch (_) {
      slot.innerHTML = '<p class="history-audit-status">备份核查暂不可用</p>';
    }
  }

  const IMAGE_TAG = /<image\b[^>]*>[\s\S]*?<\/image\s*>|<image\b[^>]*\/?>/gi;

  function readableContent(message) {
    let content = message.content || '';
    if ((message.attachments || []).length) {
      // Original JSON keeps structural image tags; the UI hides them when media
      // metadata exists, while code examples without attachments stay intact.
      content = content.replace(IMAGE_TAG, '');
    }
    return content;
  }

  function messageHtml(message) {
    const role = message.role === 'user' ? '用户' : 'AI 助手';
    const attachments = (message.attachments || []).map(item => {
      if (item.status !== 'recovered' || !item.sha256) return '';
      const mime = item.mime_type || '';
      const url = `${attachmentBase}/${encodeURIComponent(item.sha256)}${inspecting ? '?inspect=1' : ''}`;
      const size = `${bytes(item.bytes)}${item.width && item.height ? ` · ${item.width}×${item.height}` : ''}`;
      if (mime.startsWith('image/') && item.format_ok) {
        return `<figure class="history-att"><img loading="lazy" src="${url}" alt="原始附图"><figcaption>原始附图 · ${esc(size)}</figcaption></figure>`;
      }
      return `<p class="history-att-file">附件：<a href="${url}" rel="noopener">下载</a> · ${esc(size)}</p>`;
    }).join('');
    return `<article data-message-ordinal="${Number(message.ordinal)}" class="history-msg ${message.role === 'user' ? 'user' : 'assistant'}">
      <div class="history-meta"><span>${esc(role)}</span><span>${esc(message.recorded_at_beijing || '时间缺失')}</span></div>
      <div class="history-text">${highlight(readableContent(message))}</div>${attachments}</article>`;
  }

  const GAP_LABELS = {
    no_linked_source_archive: '尚未找到关联原文',
    content_linked_elsewhere: '内容已在其他项目或未归属中可读',
    metadata_only_sources: '关联来源只有元数据',
    linked_archive_without_body: '关联归档暂无可确认正文',
  };
  const gapLabel = code => GAP_LABELS[code] || code || '原因未知';
  const gapTime = value => {
    const text = String(value || '');
    const m = text.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
    return m ? `${m[1]} ${m[2]}` : (text || '时间未知');
  };
  const gapGroupPath = group => group === 'sources' ? '#history/gaps/sources' : '#history/gaps/projects';
  const gapProjectStatus = row => {
    const lifecycle = {active: '进行中', archived: '已归档'}[row.status] || '状态未知';
    const evidence = {documented: '资料较完整', partial: '部分资料', task_only: '仅任务记录', insufficient: '资料不足'}[row.evidence_status] || '资料待核对';
    return `${lifecycle} · ${evidence}`;
  };
  const gapSourceClient = value => ({codex: 'Codex', kimi: 'Kimi', claude: 'Claude'}[value] || (value ? value : '来源未知'));
  const gapSourceId = value => {
    const text = String(value || '');
    if (text.startsWith('snapshot-')) return '#' + text.slice('snapshot-'.length);
    return '#' + (text ? text.slice(0, 8) + (text.length > 8 ? '…' : '') : '未知');
  };
  let gapSearchTimer = null;

  async function ensureGapSummary() {
    if (state.gapSummary) return;
    const data = await getJSON(`${API}/coverage/summary`);
    if (!data || !data.counts || !Number.isFinite(Number(data.counts.projects))
        || !Number.isFinite(Number(data.counts.sources))) {
      const error = new Error('待补全审计摘要格式不可用。');
      error.status = 500;
      throw error;
    }
    state.gapSummary = data;
  }

  async function loadGapGroup(group, force = false) {
    const page = state.gapPages[group] || 1;
    const q = state.gapQueries[group] || '';
    const prior = state.gapData[group];
    if (!force && prior && prior.page === page && prior.q === q) return;
    const ticket = ++state.gapSeq[group];
    state.gapLoading[group] = true;
    state.gapErrors[group] = null;
    if (state.mode === 'gaps' && state.gapGroup === group) renderGaps();
    try {
      const data = await getJSON(`${API}/coverage/${group}?page=${page}&page_size=10&q=${encodeURIComponent(q)}`);
      if (ticket !== state.gapSeq[group]) return;
      if (!data || data.group !== group || !Array.isArray(data.items)) {
        const error = new Error('待补全列表格式不可用。');
        error.status = 500;
        throw error;
      }
      state.gapData[group] = data;
      state.gapPages[group] = data.page;
    } catch (error) {
      if (ticket !== state.gapSeq[group]) return;
      state.gapErrors[group] = error;
    } finally {
      if (ticket === state.gapSeq[group]) {
        state.gapLoading[group] = false;
        if (state.mode === 'gaps' && state.gapGroup === group) renderGaps();
      }
    }
  }

  async function loadGapSource(blockId) {
    const ticket = ++state.gapSeq.sources;
    state.gapSourceLoading = true;
    state.gapSourceError = null;
    state.gapSourceDetail = null;
    renderGapSource(blockId);
    try {
      const data = await getJSON(`${API}/coverage/source/${encodeURIComponent(blockId)}`);
      if (ticket !== state.gapSeq.sources) return;
      if (!data || !data.item) {
        const error = new Error('该空来源的审计记录不存在。');
        error.status = 404;
        throw error;
      }
      state.gapSourceDetail = data;
    } catch (error) {
      if (ticket !== state.gapSeq.sources) return;
      state.gapSourceError = error;
    } finally {
      if (ticket === state.gapSeq.sources) {
        state.gapSourceLoading = false;
        if (state.mode === 'unassigned' && (location.hash.includes('/unassigned/source/') || location.hash.includes('/gaps/source/'))) renderGapSource(blockId);
      }
    }
  }

  function gapPager(group, data) {
    if (!data || data.pages <= 1) return '';
    return `<div class="history-pager history-gap-pager" aria-label="待补全分页">
      <button type="button" class="ghost small" data-gap-page="${group}" data-gap-value="${data.page - 1}" ${data.page <= 1 ? 'disabled' : ''}>上一页</button>
      <span>第 ${data.page} / ${data.pages} 页 · 共 ${data.total} 项</span>
      <button type="button" class="ghost small" data-gap-page="${group}" data-gap-value="${data.page + 1}" ${data.page >= data.pages ? 'disabled' : ''}>下一页</button>
    </div>`;
  }

  function renderGaps() {
    const main = $('history-main');
    if (!main) return;
    const summary = state.gapSummary;
    const group = state.gapGroup;
    if (!summary) {
      main.innerHTML = `<div class="history-gap-head"><p class="insights-kicker">History Coverage</p>
        <h2>历史资料待补全</h2><p class="history-gap-status" role="status">正在读取审计摘要…</p></div>`;
      return;
    }
    const data = state.gapData[group];
    const backup = summary.backup_audit || {};
    const backupText = backup.status === 'accepted'
      ? `备份核查已验收 · ${gapTime(backup.checked_at || backup.generated_at_local)} · ${numeric(backup.item_count) ?? 0} 条`
      : backup.status === 'pending_acceptance'
        ? '备份核查结果待验收'
        : backup.status === 'invalid' ? '备份核查结果格式无效' : '备份核查状态待独立结果';
    const backupDetails = backup.status === 'accepted'
      ? `<details class="gap-audit-scope"><summary>备份核查范围与限制</summary>
          <p>检查时间：${esc(gapTime(backup.checked_at || backup.generated_at_local))}；条目：${numeric(backup.item_count) ?? 0}。</p>
          <p>范围：${esc(JSON.stringify(backup.scope || {}).slice(0, 1800))}</p>
          <p>限制：${esc((backup.limitations || []).join('；').slice(0, 1800) || '未提供额外限制')}</p>
        </details>` : '';
    const tabs = `<nav class="history-gap-tabs" aria-label="待补全资料范围">
      <button type="button" data-hash="#history/gaps/projects" aria-pressed="${group === 'projects'}">暂无问答项目 <span>${summary.counts.projects}</span></button>
      <button type="button" data-hash="#history/gaps/sources" aria-pressed="${group === 'sources'}">未归属空来源 <span>${summary.counts.sources}</span></button>
    </nav>`;
    const head = `<header class="history-gap-head">
      <p class="insights-kicker">History Coverage / 待补全资料</p>
      <h2>历史资料待补全</h2>
      <p class="history-gap-sub">审计快照 ${esc(gapTime(summary.snapshot && summary.snapshot.generated_at_local))} · ${esc(backupText)}</p>
      ${backupDetails}
      ${tabs}
    </header>`;
    const searchValue = state.gapQueries[group] || '';
    const controls = `<div class="history-gap-controls">
      <label class="history-gap-search">搜索<input type="search" id="gap-search" maxlength="200" autocomplete="off" value="${esc(searchValue)}" placeholder="${group === 'projects' ? '项目名或项目键' : '标题或空源 ID'}"></label>
      <span class="history-gap-count" role="status">${data ? `${data.total} 项` : (state.gapLoading[group] ? '读取中…' : '')}</span>
    </div>`;
    let body = '';
    if (state.gapLoading[group] && !data) {
      body = `<p class="history-gap-status" role="status">正在读取${group === 'projects' ? '暂无问答项目' : '未归属空来源'}…</p>`;
    } else if (state.gapErrors[group]) {
      body = `<div class="history-error" role="alert"><p>${esc(state.gapErrors[group].message || '读取失败。')}</p>
        <button type="button" class="ghost small" data-gap-retry="${group}">重试</button></div>`;
    } else if (!data || !data.items.length) {
      body = `<p class="history-gap-status">没有匹配的${group === 'projects' ? '项目' : '空来源'}。</p>`;
    } else if (group === 'projects') {
      body = data.items.map(row => {
        const materials = row.existing_materials || {};
        const mat = joinMeta(
          materials.summary_present ? '项目摘要' : '',
          numeric(materials.event_count) !== null && materials.event_count ? `项目记录 ${materials.event_count}` : '',
          numeric(materials.context_source_count) !== null && materials.context_source_count ? `历史摘要 ${materials.context_source_count}` : '',
          row.linked_archive_count ? `来源线索 ${row.linked_archive_count}` : '',
        ) || '仅有项目登记';
        const title = row.display_name || row.project;
        const showKey = row.display_name && row.display_name !== row.project;
        const ids = (row.technical_archive_ids || []).join(', ') || '无';
        const audit = row.backup_audit;
        return `<article class="history-gap-row">
          <div class="history-gap-row-main">
            <button type="button" class="history-gap-title" data-hash="${esc(hashForProject(row.project))}">${esc(title)}</button>
            ${showKey ? `<span class="history-gap-key">${esc(row.project)}</span>` : ''}
            <span class="history-gap-chip">${esc(gapProjectStatus(row))}</span>
            ${audit ? `<span class="history-gap-audit-chip" title="${esc(audit.short_explanation || '')}">${esc(audit.status_label || '尚无法核实')}</span>` : ''}
          </div>
          <p class="history-gap-meta">${esc(mat)}</p>
          <div class="history-gap-actions"><button type="button" class="ghost small" data-hash="${esc(hashForProject(row.project))}">打开项目 →</button></div>
          <details class="history-source gap-detail"><summary>核查详情</summary>
            <p><b>为何暂无问答：</b>${esc(row.reason || gapLabel(row.reason_code))}</p>
            <p><b>下一步：</b>${esc(row.next_step || '待核查。')}</p>
            <p>项目键：${esc(row.project)}；证据：${esc(row.evidence_status || '未标注')}；原因代码：${esc(row.reason_code || '')}</p>
            <p>archive：${esc(ids)}</p>
            <div class="gap-audit-evidence" data-audit-kind="projects" data-audit-key="${esc(row.project)}">展开后读取备份核查证据。</div>
          </details>
        </article>`;
      }).join('');
    } else {
      body = data.items.map(row => {
        const payload = row.payload || {};
        const audit = row.backup_audit;
        return `<article class="history-gap-row">
          <div class="history-gap-row-main">
            <button type="button" class="history-gap-title" data-hash="#history/gaps/source/${encodeURIComponent(row.block_id)}">${esc(row.title || '未命名来源')}</button>
            <span class="history-gap-chip">无问答</span>
            ${audit ? `<span class="history-gap-audit-chip" title="${esc(audit.short_explanation || '')}">${esc(audit.status_label || '尚无法核实')}</span>` : ''}
          </div>
          <p class="history-gap-meta">${esc(gapSourceClient(row.adapter))} · ${esc(gapSourceId(row.block_id))} · 来源时间未知</p>
          <p class="history-gap-meta">当前只保留来源元数据</p>
          <div class="history-gap-actions"><button type="button" class="ghost small" data-hash="#history/gaps/source/${encodeURIComponent(row.block_id)}">查看核查详情 →</button></div>
          <details class="history-source gap-detail"><summary>技术详情</summary>
            <p>块 ID：${esc(row.block_id)}；archive：${esc((row.source_archive_ids || []).join(', ') || '无')}</p>
            <p>原因代码：${esc(row.reason_code || '')}；manifest：${row.manifest_verified ? '通过' : '未通过'}；内容 SHA：${row.content_verified ? '通过' : '未通过'}</p>
            <p>解包：${numeric(payload.unpacked_bytes) ?? 0} bytes；conversation：${payload.conversation_len === null ? '无该字段' : (numeric(payload.conversation_len) ?? 0)}；对话消息：${numeric(payload.dialogue_messages) ?? 0} 条</p>
            <div class="gap-audit-evidence" data-audit-kind="sources" data-audit-key="${esc(row.block_id)}">展开后读取备份核查证据。</div>
          </details>
        </article>`;
      }).join('');
    }
    main.innerHTML = `${head}${controls}${body}${gapPager(group, data)}`;
  }

  function renderGapSource(blockId) {
    const main = $('history-main');
    if (!main) return;
    if (state.gapSourceLoading) {
      main.innerHTML = `<p><button type="button" class="history-back" data-hash="#history/unassigned">← 返回未归属资料</button></p>
        <p class="history-gap-status" role="status">正在读取来源审计…</p>`;
      return;
    }
    if (state.gapSourceError) {
      main.innerHTML = `<p><button type="button" class="history-back" data-hash="#history/unassigned">← 返回未归属资料</button></p>
        <div class="history-error" role="alert"><p>${esc(state.gapSourceError.message || '读取失败。')}</p></div>`;
      return;
    }
    const item = state.gapSourceDetail && state.gapSourceDetail.item;
    if (!item) {
      main.innerHTML = `<p class="history-gap-status">没有找到该空来源的审计记录。</p>`;
      return;
    }
    const payload = item.payload || {};
    const audit = (state.gapSourceDetail && state.gapSourceDetail.backup_audit) || {};
    const auditReady = !!audit.status && audit.status !== 'pending_acceptance' && audit.status !== 'invalid';
    const auditHtml = auditReady
      ? `<p><b>状态：</b>${esc(audit.status_label || '尚无法核实')}</p>
         <p><b>检查时间：</b>${esc(gapTime(audit.checked_at))}</p>
         ${audit.conclusion ? `<p><b>结论：</b>${esc(audit.conclusion)}</p>` : ''}
         ${audit.evidence ? `<p><b>证据：</b>${esc(audit.evidence)}</p>` : ''}
         ${audit.scope ? `<p><b>范围：</b>${esc(JSON.stringify(audit.scope).slice(0, 1600))}</p>` : ''}
         ${audit.limitations && audit.limitations.length ? `<p><b>限制：</b>${esc(audit.limitations.join('；').slice(0, 1600))}</p>` : ''}`
      : `<p>${esc(audit.message || '备份核查结果待验收。')}</p>`;
    main.innerHTML = `
      <p><button type="button" class="history-back" data-hash="#history/unassigned">← 返回未归属资料</button></p>
      <p class="insights-kicker">Unassigned Source / 来源详情</p>
      <div class="history-detail-heading"><h2>${esc(item.title || item.block_id)}</h2>${deletionButton({kind:"block",project:"",key:item.block_id},item.title || item.block_id,"删除来源")}</div>
      <p class="history-gap-sub">未归属空来源 · 快照 ${esc(gapTime(state.gapSourceDetail.snapshot && state.gapSourceDetail.snapshot.generated_at_local))}</p>
      <section class="history-summary">
        <h3>为什么没有问答</h3>
        <p class="history-purpose">归档里只有会话初始化记录，没有用户问题或 AI 回答。</p>
        <p class="history-gap-meta">当前只保留来源元数据；没有正文时不展示空白问答，也不虚构内容。</p>
      </section>
      <details class="history-source"><summary>备份核查详情</summary>${auditHtml}</details>
      <details class="history-source"><summary>技术来源</summary>
        <p>块 ID：${esc(item.block_id)}；archive：${esc((item.source_archive_ids || []).join(', ') || '无')}</p>
        <p>manifest：${item.manifest_verified ? '通过' : '未通过'}；内容 SHA：${item.content_verified ? '通过' : '未通过'}；解包：${numeric(payload.unpacked_bytes) ?? 0} bytes</p>
        <p>conversation：${payload.conversation_len === null ? '无该字段' : (numeric(payload.conversation_len) ?? 0)}；transcript 事件：${numeric(payload.transcript_events) ?? 0}；对话消息：${numeric(payload.dialogue_messages) ?? 0} 条</p>
        <p>payload SHA：${esc(payload.file_sha256 || '未知')}；content SHA：${esc(payload.content_sha256 || '未知')}</p>
      </details>`;
  }

  function renderBlockDetail(data, route) {
    const main = $('history-main');
    const block = data.block || {};
    const context = route.mode === 'unassigned' ? { kind: 'unassigned' } : { kind: 'project', key: route.project };
    const back = context.kind === 'project' ? hashForProject(context.key) : '#history/unassigned';
    const turns = data.turns || [];
    const turnHtml = turns.map(turn => {
      let html = '';
      if (turn.question) html += messageHtml(turn.question);
      else html += '<p class="insights-note">当前保留片段未包含与该助手内容对应的用户问题；不能据此断言原始会话没有更早问题，覆盖范围仍未知。</p>';
      (turn.answers || []).forEach((answer, index) => {
        const rendered = messageHtml(answer);
        html += index < turn.answers.length - 1
          ? `<details class="history-mid"><summary>中间过程回复 ${index + 1}/${turn.answers.length}</summary>${rendered}</details>`
          : rendered;
      });
      if (turn.question && !(turn.answers || []).length) {
        html += '<p class="insights-note">当前只保留了问题，对应回答尚未找到。</p>';
      }
      return `<section class="history-turn">${html}</section>`;
    }).join('');
    const pager = data.total_pages > 1 ? `<div class="history-pager">
      <button type="button" class="ghost small" data-history-page="${data.page - 1}" ${data.page <= 1 ? 'disabled' : ''}>上一页</button>
      <span>第 ${data.page} / ${data.total_pages} 页</span>
      <button type="button" class="ghost small" data-history-page="${data.page + 1}" ${data.page >= data.total_pages ? 'disabled' : ''}>下一页</button>
    </div>` : '';
    main.innerHTML = `
      <p><button type="button" class="history-back" data-hash="${esc(back)}">← 返回${context.kind === 'project' ? '项目历史' : '未归属资料'}</button></p>
      <h2>${esc(block.title || '会话详情')}</h2>
      <p class="history-submeta">${esc(joinMeta(messageText(block), turnText(block), '北京时间', '结束时间未知'))}</p>
      ${data.pagination && data.pagination.continuation_note ? `<p class="insights-note">${esc(data.pagination.continuation_note)}</p>` : ''}
      ${pager}${turnHtml || '<p class="insights-note">本来源没有可见问答。</p>'}${pager}
      <details class="history-source">
        <summary>来源与校验说明（会话 ID、覆盖区间、时间原值）</summary>
        <p>会话块：${esc(block.block_id || '')} · 覆盖：${esc(coverage(block.coverage))} · 原生完整覆盖：否</p>
        <p>原始观察时间（后端原值，UTC）：${esc(block.first_at_raw || '缺失')} → ${esc(block.last_at_raw || '缺失')}</p>
        <p>来源 archive id：${esc((block.source_archive_ids || []).join(', ') || '无')}</p>
        <p>图片仅显示已恢复且校验通过的附件；不可恢复图片不显示占位符。AI“已修复/已通过”仅是当时报告，本轮未独立复测。</p>
      </details>`;
  }

  function renderError(error) {
    const main = $('history-main');
    if (!main) return;
    main.innerHTML = `<div class="history-error" role="alert">
      <p>${esc(error.message || '读取失败。')}</p>
      <button type="button" class="ghost small" id="history-retry">重试</button>
    </div>`;
    const retry = $('history-retry');
    if (retry) retry.addEventListener('click', () => route());
  }

  function renderContextError(route, title, message) {
    const main = $('history-main');
    const back = route.mode === 'unassigned' ? '#history/unassigned' : hashForProject(route.project);
    main.innerHTML = `<div class="history-error" role="alert">
      <p><strong>${esc(title)}</strong></p>
      <p>${esc(message)}</p>
      <p>为避免误标父项目，本页不展示该会话原文；旧版按 block 独立读取接口不受影响。</p>
      <button type="button" class="ghost small" data-hash="${esc(back)}">返回${route.mode === 'unassigned' ? '未归属资料' : '项目历史'}</button>
    </div>`;
  }

  function renderAssignedContextError(route, context) {
    const main = $('history-main');
    const projects = context.projects || [];
    const links = projects.map(item => {
      const hash = `#history/project/${encodeURIComponent(item.project)}/block/${encodeURIComponent(context.block_id)}/1`;
      return `<button type="button" class="ghost small" data-hash="${esc(hash)}">在项目「${esc(item.project)}」中查看</button>`;
    }).join(' ');
    main.innerHTML = `<div class="history-error" role="alert">
      <p><strong>这条来源已经归属项目</strong></p>
      <p>它不再出现在「未归属资料」中；请从实际项目阅读，避免把已归属资料重复当成未归属。</p>
      <p>${links || '已记录项目关系，但当前项目入口不可用。'}</p>
      <button type="button" class="ghost small" data-hash="#history/unassigned">返回未归属资料</button>
    </div>`;
  }

  async function blockContextOk(route) {
    if (route.mode === 'unassigned') {
      const data = state.unassigned || {dialogue: [], empty: []};
      const ids = new Set([...(data.dialogue || []), ...(data.empty || [])]
        .map(row => row.block_id));
      if (!ids.has(route.block)) {
        try {
          const context = await getJSON(`${API}/block/${encodeURIComponent(route.block)}/context`);
          if (context && (context.projects || []).length) {
            renderAssignedContextError(route, context);
            return false;
          }
        } catch (_) {
          // Fall through to the bounded generic message; no raw detail.
        }
        renderContextError(route, '该会话不属于未归属资料',
          '它可能已归属某个项目，或不在当前恢复集合中；不能把它显示为“未归属”。请从实际项目或正确的会话入口打开。');
        return false;
      }
      return true;
    }
    const project = await getProject(route.project);
    const ids = new Set((project.history || []).map(row => row.block_id));
    if (!ids.has(route.block)) {
      renderContextError(route, '该会话不属于此项目',
        `会话块不在「${project.display_name || project.project}」的正式历史 block ID 列表中；不能把它标在这个项目下。合法多项目关联仍可从对应项目打开。`);
      return false;
    }
    return true;
  }

  function setGapMode(enabled) {
    const shell = document.querySelector('.history-shell');
    if (shell) shell.classList.toggle('gap-mode', !!enabled);
  }

  function normalizeLegacyGapRoute() {
    const match = location.hash.match(/^#history\/gaps(?:\/(.*))?$/);
    if (!match) return;
    const rest = (match[1] || '').replace(/\/$/, '');
    if (rest.startsWith('source/')) {
      state.unassignedFilter = 'empty';
      try { storage.setItem('evolvmem.history.unassignedFilter', 'empty'); } catch (_) {}
      history.replaceState(null, '', `#history/unassigned/source/${rest.slice('source/'.length)}`);
      return;
    }
    if (rest === 'sources') {
      state.unassignedFilter = 'empty';
      try { storage.setItem('evolvmem.history.unassignedFilter', 'empty'); } catch (_) {}
      history.replaceState(null, '', '#history/unassigned');
      return;
    }
    state.projectFilter = 'without_history';
    state.queries.projects = '';
    try {
      storage.setItem('evolvmem.history.projectFilter', 'without_history');
      storage.removeItem('evolvmem.history.projectsQuery');
    } catch (_) {}
    history.replaceState(null, '', '#history');
  }

  async function route() {
    normalizeLegacyGapRoute();
    if (!location.hash.startsWith('#history')) return;
    const parsed = parseHash();
    if (state.mode !== parsed.mode) { selection.clear(); searchPage=1; searchJob=null; ++searchSeq; }
    state.mode = parsed.mode;
    state.query = state.queries[state.mode] || '';
    renderModes();
    syncToolbar();
    setGapMode(parsed.mode === 'gaps');
    const ticket = ++state.request;
    ++state.blockRequest;
    setBusy(true);
    try {
      await ensureData(parsed.mode);
      await refreshSearch(false);
      if (ticket !== state.request) return;
      renderList();
      if (parsed.unassignedSource) {
        renderGapSource(parsed.unassignedSource);
        await loadGapSource(parsed.unassignedSource);
      } else if (parsed.block) {
        if (!(await blockContextOk(parsed))) return;
        if (ticket !== state.request) return;
        const data = await getJSON(`${API}/block/${encodeURIComponent(parsed.block)}/turns?page=${parsed.page}&page_size=50`);
        if (ticket !== state.request) return;
        renderBlockDetail(data, parsed);
        if (parsed.message !== null && parsed.message !== undefined) {
          const hit = document.querySelector(`[data-message-ordinal="${parsed.message}"]`);
          if (hit) { let parent=hit.parentElement;while(parent && parent!==$('history-main')){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;}hit.classList.add('history-search-hit');requestAnimationFrame(()=>hit.scrollIntoView({block:'center'})); }
        }
      } else if (parsed.project) {
        const data = await getProject(parsed.project);
        if (ticket !== state.request) return;
        renderProject(data);
      } else {
        renderOverview(parsed);
      }
      const main = $('history-main');
      if (main) main.focus({ preventScroll: true });
      const saved = state.returnScroll;
      if (saved && !parsed.block) {
        state.returnScroll = null;
        if (location.hash === saved.hash) {
          requestAnimationFrame(() => {
            const list = $('history-list');
            if (list) list.scrollTop = saved.listY || 0;
            window.scrollTo(0, saved.windowY || 0);
          });
        }
      }
    } catch (error) {
      if (ticket === state.request) renderError(error);
    } finally {
      if (ticket === state.request) setBusy(false);
    }
  }

  const SHELL = `
    <div class="history-shell">
      <header class="history-head">
        <div>
          <h1>历史资料</h1>
          <p class="insights-lede">查找问题与答案，按项目管理历史资料。删除后可在回收站恢复。</p>
        </div>
<div class="history-head-actions"><button class="ghost" id="history-trash" type="button">回收站</button><button class="ghost" id="history-refresh" type="button">刷新</button></div>
      </header>
      <nav class="history-modes" aria-label="历史资料范围">
        <button class="history-mode" type="button" data-history-mode="projects" aria-pressed="true">项目资料</button>
        <button class="history-mode" type="button" data-history-mode="unassigned" aria-pressed="false">未归属资料</button>
      </nav>
      <div id="history-feedback" class="history-feedback" role="status" hidden></div>
      <div class="history-layout">
        <aside class="history-sidebar" aria-label="历史资料列表">
          <div class="history-toolbar">
            <label class="history-search">搜索<input type="search" id="history-search" maxlength="200" autocomplete="off" placeholder="项目名、项目键或会话标题"></label>
            <label class="history-filter">范围<select id="history-status-filter"></select></label>
          </div>
<div class="history-list-heading"><p class="insights-note" id="history-list-note" role="status"></p><button type="button" id="history-manage" class="ghost small">批量管理</button></div><div id="history-manage-bar" class="history-manage-bar" hidden></div>
          <div id="history-list" class="history-list" aria-label="项目或未归属会话列表"></div>
        </aside>
        <section id="history-main" class="history-main" tabindex="-1" aria-live="polite"></section>
      </div>
      <dialog id="history-delete-dialog" class="history-dialog" aria-labelledby="history-delete-title">
        <h2 id="history-delete-title">删除资料</h2><div id="history-delete-summary"></div>
        <p id="history-delete-error" role="alert" class="history-dialog-error"></p>
        <div class="history-dialog-actions"><button type="button" id="history-delete-cancel" class="ghost">取消</button><button type="button" id="history-delete-confirm" class="history-danger">移至回收站</button></div>
      </dialog>
      <dialog id="history-trash-dialog" class="history-dialog history-trash-dialog" aria-labelledby="history-trash-title">
        <div class="history-trash-heading"><h2 id="history-trash-title">回收站</h2><button type="button" id="history-trash-close" class="ghost">关闭</button></div>
        <p class="insights-note">删除的项目和会话保留在这里，恢复后会回到原来的位置。</p><div id="history-trash-items"></div>
      </dialog>
    </div>`;

  function mount(container) {
    if (!container) return;
    if (!container.querySelector('#history-list')) container.innerHTML = SHELL;
    $('history-manage').hidden = !canManage;
    $('history-trash').hidden = !canManage;
    container.classList.toggle('history-management-readonly', !canManage);
    const lede = container.querySelector('.insights-lede');
    if (lede) lede.textContent = canManage ? '按项目查看摘要、简短历史与原始问答。删除后可在回收站恢复。' : '按项目查看摘要、简短历史与原始问答。当前为只读浏览。';
    bindOnce();
    route();
  }

  async function loadGapAuditEvidence(box) {
    if (!box || box.dataset.loaded === '1') return;
    box.dataset.loaded = '1';
    box.textContent = '正在读取备份核查证据…';
    const kind = box.dataset.auditKind;
    const key = box.dataset.auditKey;
    try {
      const data = await getJSON(`${API}/coverage/audit/${kind}/${encodeURIComponent(key)}`);
      const audit = data.backup_audit || {};
      if (audit.status === 'pending_acceptance' || !audit.status) {
        box.innerHTML = `<p>${esc(audit.message || '备份核查结果待验收。')}</p>`;
        return;
      }
      box.innerHTML = `<p><b>备份核查：</b>${esc(audit.status_label || audit.status || '尚无法核实')}</p>
        ${audit.conclusion ? `<p><b>结论：</b>${esc(audit.conclusion)}</p>` : ''}
        ${audit.evidence ? `<p><b>证据：</b>${esc(audit.evidence)}</p>` : ''}
        ${audit.scope ? `<p><b>范围：</b>${esc(JSON.stringify(audit.scope).slice(0, 1600))}</p>` : ''}
        ${audit.limitations && audit.limitations.length ? `<p><b>限制：</b>${esc(audit.limitations.join('；').slice(0, 1600))}</p>` : ''}`;
    } catch (error) {
      box.innerHTML = `<p>${esc(error.message || '备份核查证据读取失败。')}</p>`;
    }
  }

  function bindOnce() {
    if (bindOnce.done) return;
    bindOnce.done = true;
    bindManagement();
    const list = $('history-list');
    const main = $('history-main');
    document.querySelectorAll('[data-history-mode]').forEach(button => {
      button.addEventListener('click', () => {
        const mode = button.dataset.historyMode;
        navigate(mode === 'unassigned' ? '#history/unassigned'
          : mode === 'gaps' ? '#history/gaps' : '#history');
      });
    });
    const search = $('history-search');
    if (search) {
      search.value = state.query;
      search.addEventListener('input', () => {
        state.query = search.value;
        state.queries[state.mode] = state.query;
        try {
          storage.setItem('evolvmem.history.query', state.query);
          storage.setItem(`evolvmem.history.${state.mode}Query`, state.query);
        } catch (_) { /* local only */ }
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          selection.clear(); refreshSearch();
          const current = parseHash();
          if (!current.block && !current.project && !current.unassignedSource) renderOverview(current);
        }, 160);
      });
    }
    const statusSelect = $('history-status-filter');
    if (statusSelect) {
      statusSelect.addEventListener('change', () => {
        if (state.mode === 'projects') {
          state.projectFilter = statusSelect.value;
          try { storage.setItem('evolvmem.history.projectFilter', state.projectFilter); } catch (_) {}
        } else {
          state.unassignedFilter = statusSelect.value;
          try { storage.setItem('evolvmem.history.unassignedFilter', state.unassignedFilter); } catch (_) {}
        }
        selection.clear(); refreshSearch();
        const current = parseHash();
        if (!current.block && !current.project && !current.unassignedSource) renderOverview(current);
      });
    }
    if (main) {
      main.addEventListener('input', event => {
        if (event.target.id !== 'gap-search' || state.mode !== 'gaps') return;
        const group = state.gapGroup;
        state.gapQueries[group] = event.target.value;
        clearTimeout(gapSearchTimer);
        gapSearchTimer = setTimeout(() => {
          state.gapPages[group] = 1;
          loadGapGroup(group, true);
        }, 220);
      });
    }
    [list, main].forEach(container => {
      if (!container) return;
      container.addEventListener('click', event => {
        const jump = event.target.closest('[data-scroll-target]');
        if (jump) {
          const target = document.getElementById(jump.dataset.scrollTarget);
          if (target) {
            if (target.tagName === 'DETAILS') target.open = true;
            target.scrollIntoView({ block: 'start', behavior: 'smooth' });
          }
          return;
        }
        const gapButton = event.target.closest('[data-gap-page]');
        if (gapButton && !gapButton.disabled) {
          const kind = gapButton.dataset.gapPage;
          const value = Math.max(1, Number(gapButton.dataset.gapValue) || 1);
          state.gapPages[kind] = value;
          renderGaps();
          loadGapGroup(kind, true);
          return;
        }
        const gapRetry = event.target.closest('[data-gap-retry]');
        if (gapRetry) {
          loadGapGroup(gapRetry.dataset.gapRetry, true);
          return;
        }
        const page = event.target.closest('[data-history-page]');
        if (page && !page.disabled) {
          const current = parseHash();
          const next = Math.max(1, Number(page.dataset.historyPage));
          navigate(hashForBlock({ kind: current.mode === 'unassigned' ? 'unassigned' : 'project', key: current.project },
            current.block, next));
          return;
        }
        const target = event.target.closest('[data-hash]');
        if (target) {
          if (String(target.dataset.hash || '').includes('/block/')) {
            state.returnScroll = { hash: location.hash, windowY: window.scrollY,
                                   listY: list ? list.scrollTop : 0 };
          }
          navigate(target.dataset.hash);
        }
      });
    });
    if (main) {
      main.addEventListener('toggle', event => {
        const details = event.target;
        if (!(details instanceof HTMLDetailsElement) || !details.open
            || !details.classList.contains('gap-detail')) return;
        loadGapAuditEvidence(details.querySelector('.gap-audit-evidence'));
      }, true);
    }
    const refresh = $('history-refresh');
    if (refresh) refresh.addEventListener('click', () => {
      state.projects = null;
      state.unassigned = null;
      state.gapSummary = null;
      state.gapData = {projects: null, sources: null};
      state.gapErrors = {projects: null, sources: null};
      state.gapSeq = {projects: state.gapSeq.projects + 1, sources: state.gapSeq.sources + 1};
      state.projectCache.clear();
      route();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && (location.hash.includes('/unassigned/source/') || location.hash.includes('/gaps/source/'))) {
        navigate('#history/unassigned');
      } else if (event.key === 'Escape' && location.hash.includes('/block/')) {
        const routeNow = parseHash();
        navigate(routeNow.mode === 'unassigned' ? '#history/unassigned'
          : hashForProject(routeNow.project));
      }
    });
  }

  async function load() {
    const slot = document.querySelector('[data-slot="history-library"]');
    if (slot) { mount(slot); return; }
    bindOnce();
    if (!location.hash.startsWith('#history')) {
      navigate('#history');
      return;
    }
    await route();
  }

  function configure(options) {
    API = options.api; attachmentBase = options.attachmentBase;
    storageKey = options.storageKey; canManage = !!options.canManage; inspecting = !!options.inspect;
    state.queries = {projects:'',unassigned:''}; state.projectFilter='all';state.unassignedFilter='all';
    try {
      state.queries.projects=storage.getItem('evolvmem.history.projectsQuery')||'';
      state.queries.unassigned=storage.getItem('evolvmem.history.unassignedQuery')||'';
      state.projectFilter=storage.getItem('evolvmem.history.projectFilter')||'all';
      state.unassignedFilter=storage.getItem('evolvmem.history.unassignedFilter')||'all';
    } catch (_) {}
  }
  window.EvolvMemHistory = { load, route, mount, configure, createRequestId };
})();
