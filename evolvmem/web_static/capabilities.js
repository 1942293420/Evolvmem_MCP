(() => {
  'use strict';
  // One controller belongs to one retained workspace node. No persistent cache
  // is shared between users, and browsing never implicitly starts a write.
  const controllers = new WeakMap();
  window.EvolvCapabilities = {render({mount, esc, toast}) {
    if (controllers.has(mount)) {
      const existing = controllers.get(mount);
      existing.resume();
      return existing;
    }
    const state = {
      query:'', draft:'', category:'all', source:'all', page:1, tab:'formal',
      data:null, dataKey:'', expanded:new Set(), intakeChoices:new Map(),
      notice:'', error:false, loading:false, sequence:0, read:null,
      suspended:false, needsLoad:true, writing:false,
    };
    const cache = new Map();
    const MAX_CACHE_ENTRIES = 12, MAX_CACHE_BYTES = 4 * 1024 * 1024;
    let cacheBytes = 0;
    const renderedPanels = new Map();
    const $ = selector => mount.querySelector(selector);
    const icon = path => `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">${path}</svg>`;
    const refreshIcon = icon('<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1"/>');
    const searchIcon = icon('<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>');
    const params = () => new URLSearchParams({q:state.query,category:state.category === 'all' ? '' : state.category,source_project:state.source === 'all' ? '' : state.source,page:state.page,page_size:20}).toString();
    const labels = () => Object.fromEntries((state.data?.categories || []).map(item => [item.id,item.label]));
    const canWrite = () => window.EvolvAuth?.canWrite === true;
    const total = name => Number(state.data?.[name + '_total'] ?? state.data?.[name]?.length ?? 0);
    const notify = message => {state.notice = message; state.error = false; updateNotice(); toast?.(message);};
    function clearCache() {cache.clear(); cacheBytes = 0;}
    function remember(key, data) {
      const size = JSON.stringify(data).length * 2;
      if (cache.has(key)) {cacheBytes -= cache.get(key).size; cache.delete(key);}
      if (size > MAX_CACHE_BYTES) return;
      cache.set(key, {data, size}); cacheBytes += size;
      while (cache.size > MAX_CACHE_ENTRIES || cacheBytes > MAX_CACHE_BYTES) {
        const oldest = cache.keys().next().value;
        cacheBytes -= cache.get(oldest).size; cache.delete(oldest);
      }
    }
    function cached(key) {
      const entry = cache.get(key);
      if (!entry) return null;
      cache.delete(key); cache.set(key, entry);
      return entry.data;
    }
    async function request(path, body, signal) {
      const response = await EvolvAuth.fetch(path, body === undefined ? {signal} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const result = await response.json();
      if (!response.ok || result.ok === false) {
        const messages = {revision_conflict:'这条资料已更新，请刷新后重新核对。',forbidden:'当前账号没有编辑权限。',service_busy:'正在处理其他资料，请稍后重试。'};
        throw new Error(messages[result.error] || result.error || '操作失败，请重试。');
      }
      return result;
    }
    function stopRead() {
      ++state.sequence;
      if (state.read) {clearTimeout(state.read.timer); state.read.controller.abort(); state.read = null;}
      state.loading = false;
    }
    async function load({force = false} = {}) {
      stopRead();
      const key = params(), requestId = state.sequence;
      state.needsLoad = true;
      const hit = !force && cached(key);
      if (hit) {
        state.data = hit; state.page = Number(hit.page) || 1;
        state.dataKey = params(); state.needsLoad = false;
        state.error = false; state.notice = ''; update(); return;
      }
      if (state.suspended) return;
      const controller = new AbortController();
      const read = {controller, timedOut:false, timer:null};
      read.timer = setTimeout(() => {read.timedOut = true; controller.abort();}, 15000);
      state.read = read; state.loading = true; state.error = false; state.notice = '';
      update();
      try {
        const data = await request('/api/capabilities?' + key, undefined, controller.signal);
        if (requestId !== state.sequence || state.suspended) return;
        state.data = data; state.page = Number(data.page) || 1;
        state.dataKey = params();
        // The server may clamp a page after deletions. Cache its canonical
        // page so the next/previous controls always use the displayed page.
        remember(state.dataKey, data);
        state.needsLoad = false;
      } catch (error) {
        if (requestId !== state.sequence || state.suspended) return;
        state.error = true;
        state.notice = read.timedOut ? '读取超时，请重试。' : '读取失败：' + error.message;
        if (state.data) state.notice += ' 已保留上次读取的结果。';
      } finally {
        clearTimeout(read.timer);
        if (requestId === state.sequence) {state.read = null; state.loading = false; update();}
      }
    }
    function memoryCard(item) {
      const id = String(item.memory_id), expanded = state.expanded.has(id), categoryLabels = labels();
      return `<article class="memory-card ${expanded ? 'is-expanded' : ''}" data-memory-card="${esc(id)}" data-memory="${esc(id)}" tabindex="0" aria-label="${esc(item.title)}" aria-expanded="${expanded}">
        <div class="meta memory-card-meta"><span class="tag memory-category">${esc(categoryLabels[item.category] || item.category)}</span>${(item.keywords || []).map(keyword => `<span class="tag">${esc(keyword)}</span>`).join('')}</div>
        <h3>${esc(item.title)}</h3><p class="memory-content memory-body">${esc(item.content)}</p>
        <div class="memory-card-footer"><span>来源项目：${esc(item.source_project || '未记录')}</span><button class="memory-expand btn small" type="button" data-memory-toggle aria-expanded="${expanded}" aria-controls="memory-source-${esc(id)}">${expanded ? '收起内容' : '展开全文与来源'}</button></div>
        <div id="memory-source-${esc(id)}" class="source-detail" data-memory-source-detail ${expanded ? '' : 'hidden'}>原始来源：${esc(item.source_ref || '未记录')}<br>记忆编号：${esc(id)}</div>
      </article>`;
    }
    function skeleton() {
      return Array.from({length:6}, () => '<div class="memory-card capability-skeleton" aria-hidden="true"><span class="skeleton-line short"></span><span class="skeleton-line"></span><span class="skeleton-line"></span><span class="skeleton-line short"></span></div>').join('');
    }
    function intakeCards() {
      return (state.data?.intake || []).map(item => {
        const id = String(item.intake_id), categories = state.data.categories || [];
        const selected = state.intakeChoices.get(id) || (categories.some(c => c.id === item.suggested_category) ? item.suggested_category : categories[0]?.id || '');
        return `<article class="memory-card" data-intake-card="${esc(id)}"><h3>${esc(item.title)}</h3><p class="intake-content">${esc(item.content)}</p><div class="meta">来源项目：${esc(item.source_project || '未记录')} · 建议：${esc(labels()[item.suggested_category] || item.suggested_category || '无')}</div><div class="toolbar"><label class="intake-category-label">归入分类<select class="select" data-intake-category="${esc(id)}" aria-label="${esc(item.title)}的分类">${categories.map(category => `<option value="${esc(category.id)}" ${selected === category.id ? 'selected' : ''}>${esc(category.label)}</option>`).join('')}</select></label><button class="btn primary small" type="button" data-assign="${esc(id)}" data-revision="${esc(item.revision)}">确认归类</button></div></article>`;
      }).join('') || '<div class="empty capability-empty">待分类已清空。</div>';
    }
    function suggestionCards() {
      return (state.data?.suggestions || []).map(item => `<article class="memory-card"><h3>${esc(item.label)}</h3><p class="intake-content">${esc(item.reason)}</p><div class="toolbar"><button class="btn primary small" type="button" data-suggestion="accept" data-id="${esc(item.suggestion_id)}">接受并创建</button><button class="btn small" type="button" data-suggestion="reject" data-id="${esc(item.suggestion_id)}">拒绝</button></div></article>`).join('') || '<div class="empty capability-empty">暂无新增分类建议。</div>';
    }
    function updateNotice() {
      const notice = $('[data-capability-notice]');
      notice.hidden = !state.notice;
      notice.classList.toggle('is-error', state.error);
      $('[data-notice-text]').textContent = state.notice;
      $('[data-memory-retry]').hidden = !state.error;
    }
    function updateWriteControls() {
      $('[data-capability-readonly]').hidden = canWrite();
      mount.querySelectorAll('[data-assign],[data-suggestion]').forEach(button => {
        button.disabled = !canWrite() || state.writing || state.loading;
        button.setAttribute('aria-disabled', String(button.disabled));
        button.title = !canWrite() ? '当前为只读访问' : '';
      });
    }
    function update() {
      const data = state.data;
      mount.setAttribute('aria-busy', String(state.loading));
      $('[data-memory-refresh]').disabled = state.loading;
      $('[data-memory-refresh]').setAttribute('aria-label', state.loading ? '正在刷新记忆' : '刷新记忆');
      updateNotice();
      mount.querySelectorAll('[data-tab]').forEach(button => {
        const active = button.dataset.tab === state.tab;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', String(active));
        button.tabIndex = active ? 0 : -1;
      });
      $('[data-intake-count]').textContent = total('intake');
      $('[data-suggestions-count]').textContent = total('suggestions');
      const categories = data?.categories || [], counts = data?.counts || {};
      const categoryMarkup = `<button class="chip capability-category ${state.category === 'all' ? 'active' : ''}" type="button" data-category="all" aria-pressed="${state.category === 'all'}"><span>全部能力</span><b>${Object.values(counts).reduce((sum,count) => sum + Number(count || 0),0)}</b></button>${categories.map(item => `<button class="chip capability-category ${state.category === item.id ? 'active' : ''}" type="button" data-category="${esc(item.id)}" aria-pressed="${state.category === item.id}"><span>${esc(item.label)}</span><b>${Number(counts[item.id]) || 0}</b></button>`).join('')}`;
      if ($('[data-category-list]').innerHTML !== categoryMarkup) $('[data-category-list]').innerHTML = categoryMarkup;
      const sources = [...new Set([...(data?.source_projects || []), ...(data?.items || []).map(item => item.source_project), ...(state.source === 'all' ? [] : [state.source])])].filter(value => typeof value === 'string' && value);
      const sourceMarkup = '<option value="all">全部来源项目</option>' + sources.map(source => `<option value="${esc(source)}" ${state.source === source ? 'selected' : ''}>${esc(source)}</option>`).join('');
      if ($('[data-memory-source]').innerHTML !== sourceMarkup) $('[data-memory-source]').innerHTML = sourceMarkup;
      $('[data-memory-source]').value = state.source;
      // The input is a draft, separate from the last submitted query. Responses
      // never restore its value or replace the input node.
      $('[data-memory-clear]').disabled = !state.draft && !state.query && state.source === 'all' && state.category === 'all';
      $('[data-formal-tools]').hidden = state.tab !== 'formal';
      mount.querySelectorAll('[data-capability-panel]').forEach(panel => {panel.hidden = panel.dataset.capabilityPanel !== state.tab;});
      const currentLabel = labels()[state.category] || '全部能力';
      $('[data-result-title]').textContent = state.tab === 'formal' ? currentLabel : state.tab === 'intake' ? '待分类暂存' : '新增分类建议';
      $('[data-result-count]').textContent = state.loading ? (data ? '正在读取，暂时显示上次结果…' : '正在读取记忆…') : state.error ? '读取未完成' : state.tab === 'formal' ? `共 ${data?.total || 0} 条记忆` : `共 ${total(state.tab)} 条`;
      // Render only the visible lane. Queue rows can be numerous and should
      // not delay the first screen of formal memories or every filter change.
      const panelKey = data || (state.loading ? 'loading' : 'empty');
      if (renderedPanels.get(state.tab) !== panelKey) {
        const target = state.tab === 'formal' ? $('[data-memory-results]') : state.tab === 'intake' ? $('[data-intake-results]') : $('[data-suggestion-results]');
        target.innerHTML = !data ? (state.loading ? skeleton() : '<div class="empty capability-empty">暂时无法读取记忆，请点击上方“重试”。</div>') : state.tab === 'formal' ? ((data.items || []).map(memoryCard).join('') || '<div class="empty capability-empty">没有符合条件的记忆，请调整筛选。</div>') : state.tab === 'intake' ? intakeCards() : suggestionCards();
        renderedPanels.set(state.tab, panelKey);
      }
      const page = Number(data?.page) || 1, pages = Math.max(1, Number(data?.pages) || 1);
      $('[data-page-status]').textContent = `第 ${page} / ${pages} 页 · 共 ${data?.total || 0} 条`;
      $('[data-page="prev"]').disabled = !data || state.loading || state.error || page <= 1;
      $('[data-page="next"]').disabled = !data || state.loading || state.error || page >= pages;
      updateWriteControls();
    }
    function toggleCard(card) {
      const id = card.dataset.memoryCard, expanded = !state.expanded.has(id);
      if (expanded) {
        state.expanded.add(id);
        if (state.expanded.size > 400) state.expanded.delete(state.expanded.values().next().value);
      } else state.expanded.delete(id);
      card.classList.toggle('is-expanded', expanded);
      card.setAttribute('aria-expanded', String(expanded));
      const button = card.querySelector('[data-memory-toggle]');
      button.setAttribute('aria-expanded', String(expanded));
      button.textContent = expanded ? '收起内容' : '展开全文与来源';
      card.querySelector('[data-memory-source-detail]').hidden = !expanded;
    }
    function selectTab(tab, focus = false) {
      if (!['formal','intake','suggestions'].includes(tab)) return;
      state.tab = tab; update();
      if (focus) $(`[data-tab="${tab}"]`).focus();
    }
    async function write(button) {
      if (state.writing) return;
      if (!canWrite()) {notify('当前账号没有编辑权限。'); updateWriteControls(); return;}
      const intake = button.hasAttribute('data-assign'), id = intake ? button.dataset.assign : button.dataset.id;
      const category = intake ? mount.querySelector(`[data-intake-category="${CSS.escape(id)}"]`)?.value : '';
      if (intake && !category) {notify('请选择要归入的分类。'); return;}
      state.writing = true; updateWriteControls();
      const originalLabel = button.textContent;
      button.textContent = '正在提交…';
      try {
        const path = intake ? `/api/capabilities/intake/${encodeURIComponent(id)}/assign` : `/api/capability-categories/suggestions/${encodeURIComponent(id)}/${button.dataset.suggestion}`;
        await request(path, intake ? {category,expected_revision:Number(button.dataset.revision)} : {});
        if (intake) state.intakeChoices.delete(id);
        clearCache(); state.needsLoad = true;
        const message = intake ? '已归入正式能力记忆' : button.dataset.suggestion === 'accept' ? '已接受建议并创建新分类' : '已拒绝分类建议';
        // Keep the tab the user is currently browsing, even when this write
        // finishes after they have navigated elsewhere.
        if (!state.suspended) {await load({force:true}); if (!state.error) notify(message); else toast?.(message);}
        else notify(message);
      } catch (error) {
        notify(error.message || '提交结果未确认，请刷新核对后再操作。');
      } finally {
        state.writing = false;
        button.textContent = originalLabel;
        updateWriteControls();
      }
    }
    mount.classList.add('capability-workspace');
    mount.innerHTML = `<header class="capability-heading"><div><span class="capability-eyebrow">经验知识</span><h1>开发能力记忆</h1></div><button class="btn" type="button" data-memory-refresh>${refreshIcon}<span>刷新</span></button></header>
      <div class="capability-tabs" role="tablist" aria-label="记忆处理状态"><button class="chip active" type="button" role="tab" id="capability-tab-formal" aria-controls="capability-panel-formal" aria-selected="true" data-tab="formal">正式记忆</button><button class="chip" type="button" role="tab" id="capability-tab-intake" aria-controls="capability-panel-intake" aria-selected="false" tabindex="-1" data-tab="intake">待分类 <b data-intake-count>0</b></button><button class="chip" type="button" role="tab" id="capability-tab-suggestions" aria-controls="capability-panel-suggestions" aria-selected="false" tabindex="-1" data-tab="suggestions">分类建议 <b data-suggestions-count>0</b></button></div>
      <div class="capability-layout"><aside class="capability-sidebar" aria-label="能力分类"><h2>能力分类</h2><div data-category-list></div></aside><div class="capability-main">
      <form class="toolbar capability-toolbar" data-formal-tools role="search" aria-label="搜索开发能力记忆"><div class="capability-search">${searchIcon}<input class="input" type="search" data-memory-query aria-label="搜索记忆、技术栈或来源项目" placeholder="搜索记忆、技术栈或来源项目" autocomplete="off"></div><select class="select" data-memory-source aria-label="来源项目"><option value="all">全部来源项目</option></select><button class="btn primary" type="submit" data-memory-search>搜索</button><button class="btn" type="button" data-memory-clear>清空筛选</button></form>
      <div class="capability-notice refresh-notice" data-capability-notice role="status" hidden><span data-notice-text></span><button class="btn small" type="button" data-memory-retry hidden>重试</button></div><div class="capability-readonly" data-capability-readonly hidden>当前为只读访问</div>
      <div class="capability-result-summary"><h2 data-result-title>全部能力</h2><span data-result-count role="status" aria-live="polite">正在读取记忆…</span></div>
      <div class="capability-content"><section id="capability-panel-formal" role="tabpanel" aria-labelledby="capability-tab-formal" data-capability-panel="formal"><div class="memory-grid" data-memory-results></div><div class="pager"><button class="btn small" type="button" data-page="prev" disabled>上一页</button><span data-page-status></span><button class="btn small" type="button" data-page="next" disabled>下一页</button></div></section><section id="capability-panel-intake" role="tabpanel" aria-labelledby="capability-tab-intake" data-capability-panel="intake" hidden><div class="memory-grid" data-intake-results></div></section><section id="capability-panel-suggestions" role="tabpanel" aria-labelledby="capability-tab-suggestions" data-capability-panel="suggestions" hidden><div class="memory-grid" data-suggestion-results></div></section></div></div></div>`;
    mount.addEventListener('input', event => {
      if (event.target.matches('[data-memory-query]')) {state.draft = event.target.value; $('[data-memory-clear]').disabled = !state.draft && !state.query && state.source === 'all' && state.category === 'all';}
    });
    mount.addEventListener('submit', event => {
      if (!event.target.matches('[data-formal-tools]')) return;
      event.preventDefault(); state.query = state.draft; state.page = 1; load();
    });
    mount.addEventListener('change', event => {
      if (event.target.matches('[data-memory-source]')) {state.source = event.target.value; state.page = 1; load();}
      if (event.target.matches('[data-intake-category]')) {
        state.intakeChoices.set(event.target.dataset.intakeCategory, event.target.value);
        if (state.intakeChoices.size > 400) state.intakeChoices.delete(state.intakeChoices.keys().next().value);
      }
    });
    mount.addEventListener('click', event => {
      const button = event.target.closest('button');
      if (button?.disabled) return;
      if (button?.hasAttribute('data-tab')) {selectTab(button.dataset.tab); return;}
      if (button?.hasAttribute('data-category')) {state.category = button.dataset.category; state.page = 1; state.tab = 'formal'; load(); return;}
      if (button?.hasAttribute('data-memory-refresh') || button?.hasAttribute('data-memory-retry')) {load({force:true}); return;}
      if (button?.hasAttribute('data-memory-clear')) {state.query = ''; state.draft = ''; state.source = 'all'; state.category = 'all'; state.page = 1; $('[data-memory-query]').value = ''; load(); $('[data-memory-query]').focus(); return;}
      if (button?.hasAttribute('data-page')) {state.page += button.dataset.page === 'next' ? 1 : -1; load(); return;}
      if (button?.hasAttribute('data-assign') || button?.hasAttribute('data-suggestion')) {write(button); return;}
      const card = event.target.closest('[data-memory-card]');
      if (card && (!button || button.hasAttribute('data-memory-toggle')) && !event.target.closest('a,input,select,textarea') && !window.getSelection()?.toString()) toggleCard(card);
    });
    mount.addEventListener('keydown', event => {
      const tab = event.target.closest('[data-tab]');
      if (tab && ['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {
        event.preventDefault(); const tabs = ['formal','intake','suggestions'], index = tabs.indexOf(state.tab);
        selectTab(event.key === 'Home' ? tabs[0] : event.key === 'End' ? tabs[2] : tabs[(index + (event.key === 'ArrowRight' ? 1 : 2)) % 3], true); return;
      }
      if (event.target.matches('[data-memory-card]') && (event.key === 'Enter' || event.key === ' ')) {event.preventDefault(); toggleCard(event.target);}
    });
    const handle = {
      suspend() {state.suspended = true; if (state.loading) state.needsLoad = true; stopRead(); update();},
      resume() {state.suspended = false; updateWriteControls(); if (state.needsLoad) load();},
      refresh() {return load({force:true});},
    };
    controllers.set(mount, handle);
    load();
    return handle;
  }};
})();
