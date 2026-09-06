/* Native organization controls for the shared memory data page. */
(() => {
  'use strict';
  const $ = (selector, root) => root.querySelector(selector);
  const $$ = (selector, root) => [...root.querySelectorAll(selector)];
  const TIERS = [['pinned', '固定注入'], ['normal', '常用记忆'], ['reference', '参考资料']];
  const ATTRS = [['fact', '事实'], ['decision', '决定'], ['preference', '偏好'], ['constraint', '约束'], ['user_profile', '用户画像']];
  const REVIEW = {pending:'待审核', accepted:'已采纳', rejected:'暂不归属', not_required:'无需审核'};
  const ERRORS = {revision_conflict:'记录已被其他操作更新。草稿已保留，请核对最新记录后重试。',
    project_not_found:'项目尚未注册，请打开项目名录检查。', resolution_not_found:'该记忆没有可编辑的项目决议。',
    invalid_revision:'缺少有效版本，无法保存项目归属。', llm_unavailable:'AI 整理暂不可用，请使用手动归属。',
    llm_bad_response:'AI 返回的建议无法读取，请稍后重试。', invalid_display_name:'显示名最多 32 个字符，不能包含控制字符。'};

  function create(mount, helpers) {
    const esc = helpers.esc;
    const c = {kind:'memories', page:1, query:'', project:'', status:'active', tier:'', attribute:'', review:'', preset:'', sort:'created_at', order:'desc', pageSize:30};
    let rows = [], total = 0, loaded = false, loading = false, busy = false, request = 0, timer;
    let registry = helpers.projects?.() || [], registryLoaded = false, advanced = false, message = '', messageError = false;
    let batchProject = '', editId = null, dialogKind = '', newProject = {name:'', display:''};
    const selected = new Set(), drafts = new Map(), suggestions = new Map(), metadata = new Map(), registryDrafts = new Map(), rowErrors = new Map();
    const dialog = document.createElement('dialog');
    dialog.className = 'ui-dialog data-dialog org-dialog';
    document.body.append(dialog);
    const options = (items, value) => items.map(([key, title]) => `<option value="${esc(key)}"${key === value ? ' selected' : ''}>${esc(title)}</option>`).join('');
    const projectName = slug => registry.find(p => p.project === slug)?.display_name || helpers.projectName?.(slug) || slug || '未归属';
    const projectOptions = (value, filter = false) => {
      const items = [[ '', filter ? '全部项目' : '选择项目' ]];
      if (filter) items.push(['__none__', '未归属项目']);
      registry.filter(p => p.status === 'active').forEach(p => items.push([p.project, p.display_name ? `${p.display_name} · ${p.project}` : p.project]));
      if (value && !items.some(([key]) => key === value)) items.push([value, projectName(value)]);
      return options(items, value);
    };
    const rowById = id => rows.find(row => String(row.id) === String(id)) || drafts.get(String(id))?.row || metadata.get(String(id))?.row;
    const eligible = row => row && row.item_id != null && Number.isInteger(row.resolution_revision) && row.resolution_revision > 0;
    const selectedRows = () => rows.filter(row => selected.has(String(row.id)));
    const pendingCount = () => drafts.size + [...metadata.values()].filter(d => d.dirty).length + registryDrafts.size + (newProject.name || newProject.display ? 1 : 0);
    const button = (action, title, id = '', disabled = false, extra = '') => `<button type="button" class="ui-button ${extra}" data-org-action="${action}"${id !== '' ? ` data-org-id="${esc(id)}"` : ''}${disabled || busy ? ' disabled' : ''}>${title}</button>`;
    function notice(value, error = false) { message = value; messageError = error; }
    function errorText(error) { const code = error?.message || String(error); return ERRORS[code] || code; }
    async function post(path, body = {}, allowPartial = false) {
      const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
      let data;
      try { data = await response.json(); } catch { throw new Error(`请求失败（${response.status}），请检查结果后重试。`); }
      if (!response.ok || data.ok === false) {
        if (allowPartial && data.assignments && Object.keys(data.assignments).length) return data;
        throw new Error(data.error || `请求失败（${response.status}）`);
      }
      return data;
    }
    async function refreshRegistry() {
      const data = await helpers.get('/api/projects');
      registry = data.projects || []; registryLoaded = true;
    }
    async function changed() {
      try { await helpers.onChanged?.(); } catch { notice(`${message} 统计暂未刷新，可稍后重试。`, true); }
    }
    async function run(operation) {
      if (busy) return;
      const reloadInterruptedRead = loading;
      busy = true; ++request; clearTimeout(timer); loading = false; c.draw(); renderDialog();
      try { await operation(); } catch (error) { notice(errorText(error), true); }
      finally { busy = false; c.draw(); renderDialog(); if (reloadInterruptedRead) c.load(); }
    }
    function setDraft(row, project, source = 'manual') {
      if (!eligible(row)) return;
      const id = String(row.id), previous = drafts.get(id);
      if (!project || (project === row.project && row.review_state !== 'pending')) { drafts.delete(id); rowErrors.delete(id); return; }
      drafts.set(id, {project, row:previous?.row || {...row}, revision:previous?.revision ?? row.resolution_revision,
        source, error:previous?.error || '', conflict:previous?.conflict || false});
    }
    function filters() {
      const select = (key, title, choices) => `<label class="ui-filter-label">${title}<select class="ui-select" data-org-filter="${key}"${busy ? ' disabled' : ''}>${options(choices, c[key])}</select></label>`;
      return `<div class="ui-toolbar org-toolbar"><label class="ui-filter-label">搜索<input class="ui-search" type="search" data-org-filter="query" placeholder="搜索记忆内容或标识" value="${esc(c.query)}" maxlength="500"${busy ? ' disabled' : ''}></label>
        <label class="ui-filter-label">项目归属<select class="ui-select" data-org-filter="project"${busy ? ' disabled' : ''}>${projectOptions(c.project, true)}</select></label>
        ${select('status', '状态', [['active','活跃'],['archived','已归档'],['superseded','已被替代'],['all','全部状态']])}
        <div class="org-actions">${button('advanced', advanced ? '收起筛选' : '更多筛选')}${button('registry', '项目名录')}${button('ai', busy ? '处理中…' : 'AI 整理', '', loading || !rows.length)}</div></div>
        <div class="org-advanced"${advanced ? '' : ' hidden'}>${select('tier','层级',[['','全部层级'],...TIERS])}${select('attribute','属性',[['','全部属性'],...ATTRS,...[...new Set(rows.map(r => r.attribute).filter(Boolean))].filter(a => !ATTRS.some(([k]) => k === a)).map(a => [a, helpers.label(a)])])}
        ${select('review','归属审核',[['','全部审核状态'],...Object.entries(REVIEW),['none','没有审核记录']])}
        ${select('preset','快捷筛选',[['','全部记录'],['today','今日新增'],['skill','高频记忆（≥3 次命中）'],['forgetting','待归档候选']])}
        ${select('sort','排序',[['created_at','创建时间'],['last_accessed','最近访问'],['access_count','检索命中'],['importance','重要性']])}${select('order','顺序',[['desc','从高到低 / 从新到旧'],['asc','从低到高 / 从旧到新']])}
        <label class="ui-filter-label">每页<select class="ui-select" data-org-filter="pageSize"${busy ? ' disabled' : ''}>${options([['30','30 条'],['50','50 条'],['100','100 条']], String(c.pageSize))}</select></label></div>`;
    }
    function bars() {
      const chosen = selectedRows(), count = drafts.size;
      return `<div class="org-batchbar"${chosen.length ? '' : ' hidden'}><strong>已选 ${chosen.length} 条</strong>
        <select class="ui-select" data-org-batch-project aria-label="批量指派项目"${busy ? ' disabled' : ''}>${projectOptions(batchProject)}</select>
        ${button('batch-project','暂存归属','',!batchProject)}${button('batch-archive','批量归档','',!chosen.some(r => r.status === 'active'))}${button('batch-restore','批量恢复','',!chosen.some(r => r.status === 'archived'))}${button('clear-selection','取消选择')}</div>
        <div class="org-draftbar"${count ? '' : ' hidden'}><strong>${count} 条归属待保存</strong><span>筛选和翻页会保留草稿</span>${button('save-all','保存全部归属')}${button('discard-all','放弃改动')}</div>`;
    }
    function rowHTML(row) {
      const id = String(row.id), draft = drafts.get(id), suggestion = suggestions.get(id), error = draft?.error || rowErrors.get(id);
      const current = draft ? draft.project : row.project;
      const review = row.review_state === 'pending' && eligible(row);
      const status = row.status === 'active' ? '活跃' : helpers.label(row.status);
      let notes = '';
      if (draft) notes += `<div class="org-row-note">待保存 → ${esc(projectName(draft.project))}${!registry.some(p => p.project === draft.project) ? '（保存时注册新项目）' : ''}</div>`;
      if (suggestion) notes += `<div class="org-row-note" data-org-suggestion="${esc(id)}">${suggestion.via === 'key' ? '标识建议' : 'AI 建议'}：${esc(projectName(suggestion.project))}${button('use-suggestion','采用建议',id)}${button('dismiss-suggestion','忽略',id)}</div>`;
      if (review) notes += `<div class="org-row-note">待审核${row.proposed_project ? ` · 建议 ${esc(projectName(row.proposed_project))}` : ''}${row.confidence ? ` · ${esc(row.confidence)}` : ''}</div>`;
      if (error) notes += `<div class="org-error" role="alert" data-org-error="${esc(id)}">${esc(error)}${draft?.conflict ? button('rebase','核对最新版本',id) : ''}</div>`;
      return `<tr data-org-row="${esc(id)}" class="${draft ? 'org-row-staged' : ''} ${error ? 'org-row-error' : ''}">
        <td class="org-col-check"><input type="checkbox" data-org-select="${esc(id)}" aria-label="选择记忆 ${esc(id)}"${selected.has(id) ? ' checked' : ''}${busy ? ' disabled' : ''}></td>
        <td><button type="button" class="data-record" data-org-action="detail" data-org-id="${esc(id)}"><span class="data-primary">${esc(row.value || '尚无内容')}</span><span class="data-secondary">#${esc(id)} · ${esc(row.key)}</span></button>${notes}</td>
        <td class="org-col-project">${eligible(row) ? `<select class="ui-select org-project-pick" data-org-project="${esc(id)}" aria-label="记忆 ${esc(id)} 的项目归属"${busy ? ' disabled' : ''}>${projectOptions(current)}</select>` : `<span>${esc(projectName(row.project))}</span><span class="data-secondary">暂无可编辑归属</span>`}${draft ? button('save-row','保存归属',id,!!draft.conflict) : ''}</td>
        <td class="data-col-status"><span class="data-status">${esc(status)}</span><span class="data-secondary">${esc(helpers.label(row.tier))}</span></td>
        <td class="data-col-type data-col-extra">${esc(helpers.label(row.attribute))}<span class="data-secondary">重要性 ${esc(row.importance ?? '—')}</span></td>
        <td class="data-col-number data-col-extra">${esc(row.access_count ?? 0)}</td>
        <td class="org-col-actions"><div class="org-actions">${button('edit','编辑',id)}${row.status === 'active' ? button('archive','归档',id) : row.status === 'archived' ? button('restore','恢复',id) : ''}
          <details class="org-more"><summary aria-label="记忆 ${esc(id)} 的更多操作">更多</summary><div class="org-more-menu">
          ${review ? button('review-accept','采纳归属',id,!draft?.project && !row.proposed_project) + button('review-reject','暂不归属',id) : ''}
          ${draft ? button('discard-row','放弃归属改动',id) : ''}${button('delete','删除',id,false,'org-danger')}${button('hard_delete','彻底删除',id,false,'org-danger')}</div></details></div></td></tr>`;
    }
    c.draw = () => {
      const focused = mount.contains(document.activeElement) ? document.activeElement : null;
      const focusedFilter = focused?.dataset.orgFilter;
      const selection = focused?.tagName === 'INPUT' && focused.type === 'search' ? [focused.selectionStart, focused.selectionEnd] : null;
      const allSelected = rows.length && rows.every(row => selected.has(String(row.id)));
      mount.innerHTML = `${filters()}${bars()}<div class="org-message${messageError ? ' error' : ''}" role="${messageError ? 'alert' : 'status'}" data-org-message${message ? '' : ' hidden'}>${esc(message)}</div>
        <div class="ui-result-info"><span data-results-info role="status">${loading ? '正在读取…' : loaded ? `${total} 条记录` : '等待读取'}</span><span>点击内容查看详情 · AI 建议需确认后保存</span></div>
        <div data-results aria-busy="${loading || busy}">${rows.length ? `<div class="data-table-wrap"><table class="data-table org-table"><caption class="data-sr-only">记忆管理，可选择、编辑和整理归属</caption><thead><tr>
          <th class="org-col-check"><input type="checkbox" data-org-select-all aria-label="选择当前页全部记忆"${allSelected ? ' checked' : ''}${busy ? ' disabled' : ''}></th><th scope="col">记忆内容 / 标识</th><th scope="col" class="org-col-project">项目归属</th><th scope="col" class="data-col-status">状态 / 层级</th><th scope="col" class="data-col-type data-col-extra">属性 / 重要性</th><th scope="col" class="data-col-number data-col-extra">命中</th><th scope="col" class="org-col-actions">操作</th></tr></thead><tbody>${rows.map(rowHTML).join('')}</tbody></table></div>` : `<div class="ui-empty"><strong>${loading ? '正在读取记忆…' : loaded ? '没有符合筛选条件的记录' : '记忆列表尚未加载'}</strong><span>${loaded ? '调整搜索词、项目或状态后再试。' : '可以重新读取列表。'}</span>${!loading ? button('reload','重新读取') : ''}</div>`}</div>
        <div class="ui-pager" data-pager>${button('previous','← 上一页','',loading || c.page <= 1)}<span>${c.page} / ${Math.max(1,Math.ceil(total / c.pageSize))}</span>${button('next','下一页 →','',loading || c.page * c.pageSize >= total)}</div>`;
      const all = $('[data-org-select-all]', mount);
      if (all) all.indeterminate = selectedRows().length > 0 && !allSelected;
      if (focusedFilter && !busy) {
        const replacement = $$('[data-org-filter]',mount).find(field => field.dataset.orgFilter === focusedFilter);
        replacement?.focus({preventScroll:true});
        if (replacement && selection) replacement.setSelectionRange(...selection);
      }
    };
    c.load = async () => {
      if (busy) return;
      const token = ++request; clearTimeout(timer); loading = true; c.draw();
      const params = new URLSearchParams({q:c.query.trim(), attribution:c.project, status:c.status, tier:c.tier, attribute:c.attribute, review:c.review, preset:c.preset, sort:c.sort, order:c.order, page:c.page, page_size:c.pageSize});
      try {
        const data = await helpers.get(`/api/memories?${params}`);
        if (token !== request) return;
        rows = data.rows || []; total = data.total || 0; loaded = true;
        const visible = new Set(rows.map(row => String(row.id)));
        [...selected].forEach(id => { if (!visible.has(id)) selected.delete(id); });
        rows.forEach(row => {
          const draft = drafts.get(String(row.id));
          if (draft && draft.revision !== row.resolution_revision) { draft.conflict = true; draft.error = ERRORS.revision_conflict; }
        });
        helpers.onRows?.(rows);
        if (c.page > 1 && (c.page - 1) * c.pageSize >= total) { c.page = Math.max(1, Math.ceil(total / c.pageSize)); return c.load(); }
        if (!registryLoaded) {
          try {
            const projects = await helpers.get('/api/projects');
            if (token !== request) return;
            registry = projects.projects || []; registryLoaded = true;
          } catch { if (token === request) notice('项目名录暂未读取，可稍后在项目名录中重试。', true); }
        }
      } catch (error) { if (token === request) notice(`列表读取失败：${errorText(error)}`, true); }
      finally { if (token === request) { loading = false; c.draw(); } }
    };
    c.canLeave = () => {
      if (busy) { notice('操作进行中，请等结果返回后再离开。', true); c.draw(); return false; }
      if ((pendingCount() || suggestions.size) && !window.confirm('还有未保存的编辑或整理建议。离开将放弃这些改动，是否继续？')) return false;
      ++request; clearTimeout(timer); loading = false;
      registryLoaded = false;
      drafts.clear(); suggestions.clear(); metadata.clear(); registryDrafts.clear(); rowErrors.clear(); selected.clear();
      newProject = {name:'', display:''}; dialog.close(); dialogKind = ''; return true;
    };

    async function refreshAfterWrite() {
      // Reads refresh the current values, but draft revisions and error text stay intact.
      const params = new URLSearchParams({q:c.query.trim(), attribution:c.project, status:c.status, tier:c.tier, attribute:c.attribute, review:c.review, preset:c.preset, sort:c.sort, order:c.order, page:c.page, page_size:c.pageSize});
      try {
        const data = await helpers.get(`/api/memories?${params}`);
        rows = data.rows || []; total = data.total || 0; loaded = true;
        if (c.page > 1 && (c.page - 1) * c.pageSize >= total) {
          c.page = Math.max(1,Math.ceil(total / c.pageSize)); return refreshAfterWrite();
        }
        const visible = new Set(rows.map(row => String(row.id)));
        [...selected].forEach(id => { if (!visible.has(id)) selected.delete(id); });
        rows.forEach(row => { const d = drafts.get(String(row.id)); if (d && d.revision !== row.resolution_revision) { d.conflict = true; d.error = ERRORS.revision_conflict; } });
        helpers.onRows?.(rows);
      } catch { notice(`${message} 列表暂未刷新，已保留草稿。`, true); }
      await changed();
    }
    async function registerForDraft(project) {
      if (registry.some(p => p.project === project)) return;
      await post('/api/projects/register', {name:project});
      registry.push({project, display_name:'', status:'active', revision:1, active_items:0});
    }
    async function saveDrafts(ids) {
      const targets = ids.map(id => [String(id), drafts.get(String(id))]).filter(([, draft]) => draft);
      if (!targets.length) return;
      await run(async () => {
        let saved = 0, failed = 0;
        const groups = new Map();
        targets.forEach(([id, draft]) => {
          if (draft.conflict) { failed++; return; }
          if (!groups.has(draft.project)) groups.set(draft.project, []);
          groups.get(draft.project).push([id, draft]);
        });
        for (const [project, group] of groups) {
          for (let offset = 0; offset < group.length; offset += 500) {
            const entries = group.slice(offset, offset + 500);
            try {
              await registerForDraft(project);
              const results = entries.length === 1
                ? [{...(await post(`/api/resolution/${entries[0][1].row.item_id}/accept`, {project, expected_revision:entries[0][1].revision})), item_id:entries[0][1].row.item_id}]
                : (await post('/api/resolutions/batch_accept', {project, items:entries.map(([, d]) => ({item_id:d.row.item_id, expected_revision:d.revision}))})).results;
              for (const [id, draft] of entries) {
                const result = results?.find(r => r.item_id === draft.row.item_id);
                if (result?.ok) { drafts.delete(id); suggestions.delete(id); rowErrors.delete(id); saved++; }
                else { draft.error = errorText(result?.error || '未收到该条记录的保存结果，请核对后重试。'); draft.conflict = result?.error === 'revision_conflict'; failed++; }
              }
            } catch (error) {
              entries.forEach(([, draft]) => { draft.error = errorText(error); draft.conflict = error.message === 'revision_conflict'; failed++; });
            }
          }
        }
        notice(`归属保存：成功 ${saved} 条，失败 ${failed} 条。${failed ? '失败项的草稿已保留。' : ''}`, !!failed);
        await refreshAfterWrite();
      });
    }
    async function organize() {
      const chosen = selectedRows();
      const targets = (chosen.length ? chosen : rows.filter(row => !row.project)).filter(row => eligible(row) && !drafts.has(String(row.id)) && !suggestions.has(String(row.id))).slice(0,100);
      if (!targets.length) { notice('没有可整理的记忆。请选择有归属决议的记录，已有草稿或建议会保留。'); c.draw(); return; }
      await run(async () => {
        const result = await post('/api/memories/organize_suggest', {legacy_ids:targets.map(row => row.id)}, true);
        let count = 0;
        targets.forEach(row => {
          const project = result.assignments?.[String(row.id)];
          if (!project || project === row.project) return;
          suggestions.set(String(row.id), {project, via:result.via?.[String(row.id)] || 'ai', row:{...row}}); count++;
        });
        notice(`${count ? `生成 ${count} 条建议，请逐条采用后保存。` : '本次没有明确的新归属建议。'}${result.ok === false ? ` ${errorText(result.error)}` : ''}`, result.ok === false);
      });
    }
    async function memoryAction(action, targets) {
      const actionName = {archive:'归档', restore:'恢复', delete:'删除', hard_delete:'彻底删除'}[action];
      if (!targets.length) return;
      if (targets.some(row => drafts.has(String(row.id)) || metadata.get(String(row.id))?.dirty) && !window.confirm(`所选记忆仍有未保存改动。继续${actionName}并放弃操作成功条目的草稿？`)) return;
      if ((targets.length > 1 || ['delete','hard_delete'].includes(action)) && !window.confirm(`${actionName}${targets.length === 1 ? `记忆 #${targets[0].id}` : `所选 ${targets.length} 条记忆`}？${action === 'hard_delete' ? '\n将永久删除记录，无法恢复。' : action === 'delete' ? '\n删除后将从记忆列表移除；需要暂时保留时可使用归档。' : ''}`)) return;
      await run(async () => {
        let saved = 0, failed = 0;
        for (const row of targets) {
          const id = String(row.id);
          try { await post(`/api/memory/${row.id}/${action}`); saved++; drafts.delete(id); suggestions.delete(id); metadata.delete(id); rowErrors.delete(id); selected.delete(id); }
          catch (error) { failed++; rowErrors.set(id, errorText(error)); }
        }
        notice(`${actionName}完成：成功 ${saved} 条，失败 ${failed} 条。`, !!failed);
        await refreshAfterWrite();
      });
    }
    async function rejectReview(row) {
      if (!eligible(row) || !window.confirm(`将记忆 #${row.id} 的归属建议标记为不采纳？会保留${row.project ? '当前归属' : '未映射状态'}并移出待审核。`)) return;
      await run(async () => {
        const id = String(row.id);
        try {
          await post(`/api/resolution/${row.item_id}/reject`, {expected_revision:row.resolution_revision});
          drafts.delete(id); suggestions.delete(id); rowErrors.delete(id); notice(`记忆 #${row.id} 已标记为暂不归属。`);
          await refreshAfterWrite();
        } catch (error) { rowErrors.set(id, errorText(error)); throw error; }
      });
    }

    function openEdit(row) {
      if (!row) return;
      const id = String(row.id);
      if (!metadata.has(id)) {
        const tags = Array.isArray(row.tags) ? row.tags : String(row.tags || '').split(',').map(t => t.trim()).filter(Boolean);
        const values = {importance:String(row.importance ?? ''), tier:row.tier || 'normal', attribute:row.attribute || '', tags:tags.filter(t => !t.startsWith('分类:')).join(', ')};
        metadata.set(id, {row:{...row}, original:{...values}, values, categories:tags.filter(t => t.startsWith('分类:')), dirty:false, error:''});
      }
      editId = id; dialogKind = 'edit'; renderDialog(); dialog.showModal();
    }
    function closeDialog() {
      if (busy) return;
      const dirty = dialogKind === 'edit' ? metadata.get(editId)?.dirty : registryDrafts.size || newProject.name || newProject.display;
      if (dirty && !window.confirm('关闭并放弃当前弹窗中未保存的修改？')) return;
      if (dialogKind === 'edit') metadata.delete(editId);
      else { registryDrafts.clear(); newProject = {name:'', display:''}; }
      dialogKind = ''; dialog.close(); c.draw();
    }
    function renderDialog() {
      if (!dialogKind) return;
      const close = `<button type="button" class="ui-dialog-close" data-org-action="close-dialog" aria-label="关闭"${busy ? ' disabled' : ''}>✕</button>`;
      const field = (key, title, input) => `<label class="org-field">${title}${input}</label>`;
      if (dialogKind === 'edit') {
        const draft = metadata.get(editId); if (!draft) return;
        const values = draft.values;
        dialog.setAttribute('aria-label', `编辑记忆 ${editId} 的元数据`);
        dialog.innerHTML = `<div class="ui-dialog-shell">${close}<div class="ui-detail-meta">记忆 #${esc(editId)}</div><h2>编辑记忆信息</h2><p class="ui-note">${esc(draft.row.key)}</p><div class="org-form-grid">
          ${field('importance','重要性（0–10）',`<input type="number" data-org-field="importance" min="0" max="10" step="0.5" value="${esc(values.importance)}"${busy ? ' disabled' : ''}>`)}
          ${field('tier','层级',`<select data-org-field="tier"${busy ? ' disabled' : ''}>${options(TIERS, values.tier)}</select>`)}
          ${field('attribute','属性',`<input data-org-field="attribute" list="org-attribute-options" value="${esc(values.attribute)}"${busy ? ' disabled' : ''}><datalist id="org-attribute-options">${ATTRS.map(([key]) => `<option value="${key}"></option>`).join('')}</datalist>`)}
          ${field('tags','检索标签（逗号分隔）',`<input data-org-field="tags" value="${esc(values.tags)}"${busy ? ' disabled' : ''}>`)}</div>
          ${draft.categories.length ? `<p class="ui-note">保留原有分类：${esc(draft.categories.join('、'))}</p>` : ''}
          <div class="org-error" role="alert" data-org-edit-error${draft.error ? '' : ' hidden'}>${esc(draft.error)}</div>
          <div class="org-dialog-actions">${button('close-dialog','取消')}${button('save-metadata',busy ? '保存中…' : '保存修改','',!draft.dirty)}</div></div>`;
      } else {
        dialog.setAttribute('aria-label', '项目名录');
        dialog.innerHTML = `<div class="ui-dialog-shell">${close}<h2>项目名录</h2><p class="ui-note">项目标识保持稳定，显示名可随时修改。</p>
          <div class="org-actions">${button('registry-reload','重新读取')}${button('registry-ai','AI 建议显示名')}${button('registry-save',`保存显示名${registryDrafts.size ? `（${registryDrafts.size}）` : ''}`,'',!registryDrafts.size)}</div>
          <div class="org-registry-list">${registry.length ? registry.map(project => {
            const draft = registryDrafts.get(project.project);
            return `<div class="org-registry-row"><label class="org-field"><span>${esc(project.project)} <small>${project.active_items || 0} 条 · ${project.status === 'active' ? '活跃' : esc(project.status)}</small></span><input data-org-registry-name="${esc(project.project)}" aria-label="${esc(project.project)} 的显示名" maxlength="32" placeholder="显示名（可留空）" value="${esc(draft?.value ?? project.display_name ?? '')}"${busy ? ' disabled' : ''}></label>${draft?.error ? `<div class="org-error" role="alert">${esc(draft.error)}${draft.conflict ? button('registry-rebase','核对最新版本',project.project) : ''}</div>` : ''}</div>`;
          }).join('') : '<p class="ui-note">尚无注册项目。</p>'}</div>
          <section class="ui-detail-section"><h3>注册项目</h3><div class="org-form-grid"><label class="org-field">项目标识<input data-org-new-project="name" maxlength="64" placeholder="例如 my-project" value="${esc(newProject.name)}"${busy ? ' disabled' : ''}></label><label class="org-field">显示名<input data-org-new-project="display" maxlength="32" placeholder="例如 我的项目" value="${esc(newProject.display)}"${busy ? ' disabled' : ''}></label></div>${button('register','注册项目')}</section>
          <div class="org-message${messageError ? ' error' : ''}" role="status" data-org-registry-message>${esc(message)}</div></div>`;
      }
    }
    async function saveMetadata() {
      const id = editId, draft = metadata.get(id); if (!draft?.dirty) return;
      const importance = Number(draft.values.importance);
      if (!draft.values.importance.trim() || !Number.isFinite(importance) || importance < 0 || importance > 10) { draft.error = '重要性必须是 0–10 的数字。'; renderDialog(); return; }
      await run(async () => {
        try {
          const tags = [...new Set([...draft.categories, ...draft.values.tags.split(/[,，]/).map(t => t.trim()).filter(Boolean)])];
          await post(`/api/memory/${id}/update`, {importance, tier:draft.values.tier, attribute:draft.values.attribute.trim(), tags});
          metadata.delete(id); dialogKind = ''; dialog.close(); notice(`记忆 #${id} 的信息已保存。`);
          await refreshAfterWrite();
        } catch (error) { draft.error = errorText(error); throw error; }
      });
    }
    async function saveRegistry() {
      if (!registryDrafts.size) return;
      await run(async () => {
        const entries = [...registryDrafts].filter(([, draft]) => !draft.conflict);
        let saved = 0, failed = registryDrafts.size - entries.length;
        for (let offset = 0; offset < entries.length; offset += 100) {
          const chunk = entries.slice(offset,offset + 100);
          try {
            const result = await post('/api/projects/display_names', {items:chunk.map(([project,draft]) => ({project, display_name:draft.value.trim(), expected_revision:draft.revision}))});
            chunk.forEach(([project,draft]) => {
              const item = result.results?.find(r => r.project === project);
              if (item?.ok) { registryDrafts.delete(project); saved++; }
              else { draft.error = errorText(item?.error || '未收到保存结果，请核对后重试。'); draft.conflict = item?.error === 'revision_conflict'; failed++; }
            });
          } catch (error) { chunk.forEach(([,draft]) => { draft.error = errorText(error); failed++; }); }
        }
        notice(`显示名保存：成功 ${saved} 项，失败 ${failed} 项。${failed ? '失败项仍保留在输入框中。' : ''}`, !!failed);
        try { await refreshRegistry(); } catch { notice(`${message} 名录暂未刷新。`, true); }
        await changed();
      });
    }
    async function registryNames() {
      await run(async () => {
        const result = await post('/api/projects/suggest_display_names'); let count = 0;
        registry.forEach(project => {
          const value = result.names?.[project.project];
          if (typeof value !== 'string' || value === project.display_name || registryDrafts.has(project.project)) return;
          registryDrafts.set(project.project, {value, revision:project.revision, error:'', conflict:false}); count++;
        });
        notice(count ? `已填写 ${count} 个显示名建议，检查后点击保存。` : '没有新的显示名建议，已有编辑均已保留。');
      });
    }

    async function action(event) {
      const target = event.target.closest('[data-org-action]');
      if (!target || target.disabled) return;
      const name = target.dataset.orgAction, id = target.dataset.orgId, row = rowById(id);
      if (busy) return;
      if (name === 'detail') { helpers.openMemory(id); return; }
      if (name === 'advanced') { advanced = !advanced; c.draw(); return; }
      if (name === 'reload') { c.load(); return; }
      if (name === 'previous' || name === 'next') { c.page += name === 'previous' ? -1 : 1; c.load(); return; }
      if (name === 'clear-selection') { selected.clear(); c.draw(); return; }
      if (name === 'batch-project') {
        let count = 0;
        selectedRows().forEach(item => { if (eligible(item)) { setDraft(item,batchProject); count++; } });
        notice(`已暂存 ${count} 条归属${selectedRows().length > count ? `，跳过 ${selectedRows().length - count} 条不可编辑记录` : ''}。点击保存后生效。`); c.draw(); return;
      }
      if (name === 'save-row') return saveDrafts([id]);
      if (name === 'save-all') return saveDrafts([...drafts.keys()]);
      if (name === 'discard-row') { drafts.delete(id); rowErrors.delete(id); c.draw(); return; }
      if (name === 'discard-all') { if (window.confirm(`放弃 ${drafts.size} 条尚未保存的归属改动？`)) { drafts.clear(); c.draw(); } return; }
      if (name === 'ai') return organize();
      if (name === 'use-suggestion') { const suggestion = suggestions.get(id); if (suggestion) { setDraft(suggestion.row,suggestion.project,'suggestion'); suggestions.delete(id); notice('建议已采用为草稿，保存后生效。'); c.draw(); } return; }
      if (name === 'dismiss-suggestion') { suggestions.delete(id); c.draw(); return; }
      if (name === 'review-accept' && eligible(row)) { const project = drafts.get(id)?.project || row.proposed_project; if (project) { setDraft(row,project,'review'); return saveDrafts([id]); } return; }
      if (name === 'review-reject') return rejectReview(row);
      if (['archive','restore','delete','hard_delete'].includes(name) && row) return memoryAction(name,[row]);
      if (name === 'batch-archive' || name === 'batch-restore') return memoryAction(name.slice(6),selectedRows().filter(item => item.status === (name === 'batch-archive' ? 'active' : 'archived')));
      if (name === 'edit') { openEdit(row); return; }
      if (name === 'save-metadata') return saveMetadata();
      if (name === 'close-dialog') { closeDialog(); return; }
      if (name === 'registry') {
        dialogKind = 'registry'; renderDialog(); dialog.showModal();
        await run(async () => { await refreshRegistry(); }); return;
      }
      if (name === 'registry-reload') return run(async () => { await refreshRegistry(); notice('已读取最新名录，未保存的显示名已保留。'); });
      if (name === 'registry-save') return saveRegistry();
      if (name === 'registry-ai') return registryNames();
      if (name === 'register') {
        const project = newProject.name.trim(), display = newProject.display.trim();
        if (!project) { notice('请填写项目标识。',true); renderDialog(); return; }
        if (registry.some(p => p.project === project)) { notice('项目标识已存在，可直接编辑其显示名。',true); renderDialog(); return; }
        return run(async () => { await post('/api/projects/register',{name:project,display_name:display}); newProject = {name:'',display:''}; notice(`项目 ${project} 已注册。`); await refreshRegistry(); await changed(); });
      }
      if (name === 'rebase') {
        const draft = drafts.get(id); if (!draft) return;
        return run(async () => {
          const data = await helpers.get(`/api/memories?status=all&q=${encodeURIComponent(draft.row.key)}&page_size=200`);
          const latest = data.rows?.find(item => String(item.id) === id);
          if (!eligible(latest)) throw new Error('找不到可编辑的最新记录，请检查记忆是否已删除。');
          if (!window.confirm(`当前保存的项目：${projectName(latest.project)}\n您的草稿项目：${projectName(draft.project)}\n按最新版本继续保留这项改动？`)) return;
          draft.row = {...latest}; draft.revision = latest.resolution_revision; draft.conflict = false; draft.error = '';
          notice('已核对最新版本。再次点击保存归属后生效。'); helpers.onRows?.([latest]);
        });
      }
      if (name === 'registry-rebase') {
        const draft = registryDrafts.get(id); if (!draft) return;
        return run(async () => {
          await refreshRegistry(); const latest = registry.find(p => p.project === id);
          if (!latest) throw new Error('项目已不存在。');
          if (!window.confirm(`当前显示名：${latest.display_name || '（空）'}\n您的修改：${draft.value || '（空）'}\n按最新版本保留修改并等待再次保存？`)) return;
          draft.revision = latest.revision; draft.conflict = false; draft.error = ''; notice('已核对最新版本，点击保存显示名后生效。');
        });
      }
    }
    mount.addEventListener('click', action); dialog.addEventListener('click', event => { if (event.target === dialog) closeDialog(); else action(event); });
    dialog.addEventListener('cancel', event => { event.preventDefault(); closeDialog(); });
    mount.addEventListener('input', event => {
      const field = event.target;
      if (field.dataset.orgFilter !== 'query' || busy) return;
      c.query = field.value; c.page = 1; ++request; clearTimeout(timer); timer = setTimeout(() => c.load(), 250);
    });
    mount.addEventListener('change', event => {
      if (busy) return;
      const field = event.target;
      if (field.dataset.orgFilter && field.dataset.orgFilter !== 'query') {
        c[field.dataset.orgFilter] = field.dataset.orgFilter === 'pageSize' ? Number(field.value) : field.value;
        if (field.dataset.orgFilter === 'review' && field.value) c.status = 'all';
        c.page = 1; c.load();
      } else if (field.hasAttribute('data-org-select-all')) { rows.forEach(row => field.checked ? selected.add(String(row.id)) : selected.delete(String(row.id))); c.draw(); }
      else if (field.dataset.orgSelect) { field.checked ? selected.add(field.dataset.orgSelect) : selected.delete(field.dataset.orgSelect); c.draw(); }
      else if (field.dataset.orgProject) { const row = rowById(field.dataset.orgProject); if (row) { setDraft(row,field.value); suggestions.delete(String(row.id)); } c.draw(); }
      else if (field.hasAttribute('data-org-batch-project')) { batchProject = field.value; c.draw(); }
    });
    function dialogInput(event) {
      if (busy) return;
      const field = event.target;
      if (field.dataset.orgField) {
        const draft = metadata.get(editId); if (!draft) return;
        draft.values[field.dataset.orgField] = field.value;
        draft.dirty = Object.keys(draft.values).some(key => draft.values[key] !== draft.original[key]); draft.error = '';
        const save = $('[data-org-action="save-metadata"]',dialog); if (save) save.disabled = !draft.dirty;
        const error = $('[data-org-edit-error]',dialog); if (error) error.hidden = true;
      } else if (field.hasAttribute('data-org-registry-name')) {
        const slug = field.dataset.orgRegistryName, project = registry.find(p => p.project === slug); if (!project) return;
        const previous = registryDrafts.get(slug);
        if (field.value.trim() === (project.display_name || '')) registryDrafts.delete(slug);
        else registryDrafts.set(slug,{value:field.value,revision:previous?.revision ?? project.revision,error:previous?.error || '',conflict:previous?.conflict || false});
        const save = $('[data-org-action="registry-save"]',dialog); if (save) { save.disabled = !registryDrafts.size; save.textContent = `保存显示名${registryDrafts.size ? `（${registryDrafts.size}）` : ''}`; }
      } else if (field.dataset.orgNewProject) newProject[field.dataset.orgNewProject] = field.value;
    }
    dialog.addEventListener('input',dialogInput); dialog.addEventListener('change',dialogInput);
    window.addEventListener('beforeunload',event => { if (busy || pendingCount() || suggestions.size) { event.preventDefault(); event.returnValue = ''; } });
    return c;
  }
  window.EvolvOrganizer = {create};
})();
