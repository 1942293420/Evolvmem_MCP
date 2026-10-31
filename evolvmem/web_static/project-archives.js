(() => {
  'use strict';
  const instances = new WeakMap();
  const messages = {
    service_busy: '服务正在处理其他资料，请稍后重试。',
    project_not_registered: '未找到已登记的项目，请返回目录选择。',
    ambiguous_project: '名称对应多个项目，请返回目录使用项目标识选择。',
    project_ambiguous: '名称对应多个项目，请返回目录使用项目标识选择。',
    read_timeout: '读取超时，请重试。',
  };
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  window.EvolvProjectArchives = {render({mount, esc, initialProject='', onProject=()=>{}}) {
    instances.get(mount)?.dispose();
    const root = document.createElement('section');
    root.className = 'project-library';
    root.dataset.projectLibrary = '';
    mount.replaceChildren(root);
    const state = {
      view: 'catalog', filters: {q:'', status:'active', archive:'all', sort:'recent', page:1, page_size:20},
      catalog: null, catalogError: '', catalogLoading: false,
      detail: null, detailError: '', detailLoading: false, project: '', eventsPage: 1,
      expanded: new Set(), focusProject: '', returnScroll: 0,
    };
    let disposed = false, active = true, handle;
    const sequence = {catalog:0, detail:0}, controllers = {}, interrupted = new Set();
    const $ = selector => root.querySelector(selector);
    const alive = () => !disposed && active && instances.get(mount) === handle && root.isConnected && mount.contains(root);
    const current = (kind, id) => alive() && sequence[kind] === id;
    const cancel = kind => { sequence[kind]++; controllers[kind]?.abort(); delete controllers[kind]; };
    const errorText = (error, fallback) => messages[error.code] || fallback;
    async function read(url, kind, id) {
      const controller = new AbortController(); controllers[kind] = controller;
      let timedOut = false;
      const timer = setTimeout(() => {timedOut = true; controller.abort();}, 15000);
      try {
        const response = await EvolvAuth.fetch(url, {signal:controller.signal});
        const data = await response.json();
        if (!response.ok || data.ok === false) throw Object.assign(new Error('read failed'), {code:data.error});
        return data;
      } catch (error) {
        if (timedOut) error.code = 'read_timeout';
        throw error;
      } finally {
        clearTimeout(timer);
        if (sequence[kind] === id) delete controllers[kind];
      }
    }
    root.innerHTML = `<header class="data-page-header project-library-heading"><h1>项目库</h1><p>先从目录找到项目，再查看摘要和历史事件。记录日期不等于项目实际开始时间。</p></header>
      <div class="project-library-stats" data-project-stats aria-label="全目录统计"></div>
      <section data-project-directory>
        <form id="project-archive-form" class="panel project-library-filters" role="search" aria-label="搜索项目目录">
          <label class="project-library-search"><span>项目名称、标识或别名</span><input class="input" name="project" autocomplete="off" placeholder="搜索项目名称、标识或别名" aria-label="项目名称"></label>
          <button class="btn primary" type="submit">搜索</button>
          <label><span>项目状态</span><select class="select" data-project-status><option value="active">当前项目</option><option value="archived">已归档项目</option><option value="all">全部项目</option></select></label>
          <label><span>摘要情况</span><select class="select" data-project-archive-filter><option value="all">全部摘要情况</option><option value="available">已有项目摘要</option><option value="missing">尚无项目摘要</option></select></label>
          <label><span>排序</span><select class="select" data-project-sort><option value="recent">记录更新优先</option><option value="name">项目名称</option></select></label>
        </form>
        <p class="project-library-count" data-project-count role="status"></p>
        <div data-project-catalog aria-live="polite"></div>
      </section>
      <section data-project-detail hidden>
        <button type="button" class="btn project-library-back" data-project-back>← 返回项目目录</button>
        <div id="project-archive-result" aria-live="polite"></div>
      </section>`;

    function syncInputs() {
      $('#project-archive-form').elements.project.value = state.filters.q;
      $('[data-project-status]').value = state.filters.status;
      $('[data-project-archive-filter]').value = state.filters.archive;
      $('[data-project-sort]').value = state.filters.sort;
    }
    function showView() {
      $('[data-project-directory]').hidden = state.view !== 'catalog';
      $('[data-project-detail]').hidden = state.view !== 'detail';
    }
    function renderStats() {
      const stats = state.catalog?.stats;
      $('[data-project-stats]').innerHTML = stats ? `<span>全目录 <strong>${number(stats.registered)}</strong> 个已登记项目</span><span>当前 ${number(stats.active)} · 已归档 ${number(stats.archived)}</span><span>已有摘要 ${number(stats.with_archive)} · 尚无摘要 ${number(stats.without_archive)}</span>` : '';
    }
    const pager = (kind, page, pages, total) => `<nav class="project-library-pager" aria-label="${kind === 'catalog' ? '项目目录' : '历史事件'}分页">
      <button type="button" class="btn small" data-${kind}-page="prev" ${page <= 1 ? 'disabled' : ''}>上一页</button>
      <span data-${kind}-page-status>第 ${page} / ${Math.max(1,pages)} 页 · 共 ${total} 项</span>
      <button type="button" class="btn small" data-${kind}-page="next" ${page >= pages ? 'disabled' : ''}>下一页</button></nav>`;
    function renderCatalog() {
      if (!alive() || state.view !== 'catalog') return;
      showView(); renderStats();
      const target = $('[data-project-catalog]');
      target.setAttribute('aria-busy', String(state.catalogLoading));
      if (state.catalogLoading) {
        $('[data-project-count]').textContent = '正在读取项目目录…';
        target.innerHTML = `<div class="project-library-grid" data-catalog-loading role="status" aria-label="正在读取项目目录">${Array.from({length:6},()=>'<div class="project-library-skeleton" aria-hidden="true"></div>').join('')}</div>`;
        return;
      }
      if (state.catalogError) {
        $('[data-project-count]').textContent = '项目目录暂未读出';
        target.innerHTML = `<div class="panel project-library-state" data-catalog-error role="alert"><h2>读取项目目录失败</h2><p>${esc(state.catalogError)}</p><button type="button" class="btn" data-catalog-retry>重新读取</button></div>`;
        $('[data-catalog-retry]').onclick = loadCatalog;
        return;
      }
      if (!state.catalog) return;
      const {items, total, page, pages} = state.catalog;
      $('[data-project-count]').textContent = `当前筛选共 ${number(total)} 个项目`;
      if (!items.length) {
        target.innerHTML = `<div class="panel project-library-state" data-catalog-empty><h2>${number(state.catalog.stats?.registered) ? '没有符合筛选条件的项目' : '还没有登记项目'}</h2><p>${number(state.catalog.stats?.registered) ? '可以更换关键词、项目状态或摘要筛选。' : '登记后的项目会显示在这里。'}</p><button type="button" class="btn" data-clear-project-filters>清除筛选</button></div>`;
        $('[data-clear-project-filters]').onclick = () => {Object.assign(state.filters,{q:'',status:'active',archive:'all',sort:'recent',page:1});syncInputs();loadCatalog();};
        return;
      }
      target.innerHTML = `<div class="project-library-grid">${items.map(item => `<button type="button" class="panel project-library-card" data-project-card="${esc(item.project)}">
        <span class="project-library-card-heading"><span class="project-library-name">${esc(item.display_name || item.project)}</span><span class="tag">${item.status === 'archived' ? '已归档' : '当前'}</span></span>
        <span class="project-library-id">${esc(item.project)}</span>
        ${item.aliases?.length ? `<span class="project-library-alias">别名：${esc(item.aliases.join('、'))}</span>` : ''}
        <span class="project-library-card-meta"><span>${item.historical_event_count == null ? number(item.event_count) + ' 项历史事件' : '当前有效 ' + number(item.event_count) + ' · 历史保留 ' + number(item.historical_event_count)}${item.has_archive ? '' : ' · 尚无项目摘要'}</span>${item.has_legacy_document ? '<span>含旧项目资料</span>' : ''}</span>
        <span class="project-library-updated">记录更新：${esc(item.updated_at || '未确认')}</span>
        <span class="project-library-open">查看项目 <span aria-hidden="true">→</span></span></button>`).join('')}</div>${pager('catalog',number(page),number(pages),number(total))}`;
      target.querySelectorAll('[data-project-card]').forEach(button => button.onclick = () => {
        state.focusProject = button.dataset.projectCard; state.returnScroll = window.scrollY;
        openProject(button.dataset.projectCard);
      });
      target.querySelectorAll('[data-catalog-page]').forEach(button => button.onclick = () => {
        state.filters.page += button.dataset.catalogPage === 'next' ? 1 : -1; loadCatalog();
      });
    }
    async function loadCatalog() {
      cancel('catalog'); const id = sequence.catalog;
      state.catalogLoading = true; state.catalogError = ''; renderCatalog();
      const params = new URLSearchParams(state.filters);
      try {
        const data = await read('/api/project-archives?' + params, 'catalog', id);
        if (!current('catalog',id)) return;
        if (!Array.isArray(data.items)) throw new Error('invalid directory');
        state.catalog = data; state.filters.page = Math.max(1,number(data.page));
      } catch (error) {
        if (!current('catalog',id)) return;
        state.catalogError = errorText(error, '项目目录暂时无法读取，请重试。');
      } finally {
        if (current('catalog',id)) {state.catalogLoading = false; renderCatalog();}
      }
    }
    const summaryLine = line => {
      const text = line.trim();
      if (!text) return {text:'', abbreviated:false};
      if (/[{}\[\]]/.test(text) && /":/.test(text)) return {text:'〔结构化任务记录请展开完整摘要查看〕', abbreviated:true};
      return {text:text.length > 420 ? text.slice(0,417) + '…' : text, abbreviated:text.length > 420};
    };
    function renderSummary(archive) {
      const sections = []; let currentSection = {title:'项目概述',lines:[]}, abbreviated=false;
      for (const raw of String(archive.summary || '').split(/\r?\n/)) {
        const heading = raw.match(/^##+\s+(.+)$/);
        if (heading) {if (currentSection.lines.length) sections.push(currentSection);currentSection = {title:heading[1],lines:[]};}
        else {const line = summaryLine(raw.replace(/^#\s+/, '').replace(/^[-*]\s+/, ''));if (line.text) currentSection.lines.push(line.text);abbreviated ||= line.abbreviated;}
      }
      if (currentSection.lines.length) sections.push(currentSection);
      return sections.map((section,index) => `<section class="refresh-summary-section" data-summary-section="${index}"><h3>${esc(section.title)}</h3>${section.lines.map(line=>`<p>${esc(line)}</p>`).join('')}</section>`).join('') + (abbreviated ? `<details class="project-library-full-summary"><summary data-project-full-summary-toggle>查看完整摘要</summary><pre data-project-full-summary>${esc(archive.summary)}</pre></details>` : '');
    }
    const eventDate = event => {
      const precision = event.occurred_at_precision || (event.occurred_at ? (/^\d{4}-\d{2}-\d{2}$/.test(event.occurred_at) ? 'date' : 'datetime') : 'unknown');
      if (precision === 'unknown') return `发生时间未确认 · 入库时间：${event.recorded_at || '未记录'}`;
      return precision === 'date' ? `日期证据：${event.occurred_at}（仅到日，时区未确认）` : `发生时间：${event.occurred_at}`;
    };
    const sourceStatus = event => {
      const reasons = {archive_unavailable:'原文已不可读取',source_body_unverifiable:'原文暂不可核验',context_not_active:'来源资料已变更',task_superseded:'来源任务已有新版本',source_revision_changed:'来源内容已有新版本',project_mismatch:'项目归属已变更'};
      return [...new Set((event.source_diagnostics || []).filter(row=>row.current_support === false || row.valid === false).map(row=>reasons[row.reason] || '来源已变更，需重新核对'))].join('；') || '暂未找到当前有效来源，历史内容仍予保留。';
    };
    function renderDetail() {
      if (!alive() || state.view !== 'detail') return;
      showView(); const target = $('#project-archive-result');
      target.setAttribute('aria-busy', String(state.detailLoading));
      if (state.detailLoading) {target.innerHTML = '<div class="panel project-library-state" data-archive-loading role="status">正在读取项目存档…</div>';return;}
      if (state.detailError) {
        target.innerHTML = `<div class="panel project-library-state" data-archive-error role="alert"><h2>读取项目存档失败</h2><p>${esc(state.detailError)}</p><button type="button" class="btn" data-archive-retry>重新读取</button></div>`;
        $('[data-archive-retry]').onclick = () => loadDetail(state.project,state.eventsPage);
        return;
      }
      const archive = state.detail; if (!archive) return;
      const totalKnown = archive.events_total != null;
      const total = totalKnown ? number(archive.events_total) : archive.events.length;
      target.innerHTML = `<article class="panel" data-archive-summary data-project-archive><div class="summary-header"><div><h2 tabindex="-1">${esc(archive.display_name || archive.project)}</h2><p class="project-library-id">${esc(archive.project)}${archive.project_status === 'archived' ? ' · 已归档项目' : ''}</p><p class="subtitle">最早有日期记录 ${esc(archive.started_at || '未确认')} · 距最早记录 ${archive.development_days == null ? '未确认' : esc(archive.development_days) + ' 天'} · 修订 ${esc(archive.revision ?? '未形成')}</p></div><span class="tag" data-events-total>${archive.current_events_total == null ? (totalKnown ? '' : '已加载 ') + total + ' 项历史事件' : '当前有效 ' + number(archive.current_events_total) + ' · 历史保留 ' + total}</span></div>
        <p class="project-library-date-note">日期证据不等于项目实际开始时间；未确认的发生日期不会以入库时间替代。</p>
        ${archive.current_events_total == null ? '' : '<p class="project-history-explanation">当前摘要只采用有效来源；来源已变更的事件仍保留在历史列表中。</p>'}
        <p><a class="project-library-legacy" data-project-legacy href="#knowledge/library?project=${esc(encodeURIComponent(archive.project))}">查看此项目的旧版资料</a></p>
        ${archive.has_archive === false ? '<div class="project-library-state" data-archive-empty><h3>项目已登记，尚无项目摘要</h3><p>摘要生成后会显示在这里；已有历史事件仍可在下方查看。</p></div>' : renderSummary(archive)}</article>
        <section class="panel" data-project-modules></section>
        <section class="panel"><h3 class="panel-title">历史事件</h3>${archive.events.length ? `<div class="timeline">${archive.events.map(event=>`<article class="event" data-project-event="${esc(event.event_id)}" tabindex="0" role="button" aria-expanded="${state.expanded.has(event.event_id)}"><h4>${esc(event.title)}</h4><div class="meta">${esc(event.event_type)} · ${esc(eventDate(event))}</div>${event.current_support === false ? '<p class="project-history-warning">历史来源已变更，不计入当前摘要</p>' : ''}${state.expanded.has(event.event_id) ? `<div class="event-detail" data-event-source>${esc(event.content || '')}<br><code>${esc(event.source_ref || '来源未记录')}</code>${event.current_support === false ? `<p class="project-history-warning">${esc(sourceStatus(event))}</p>` : ''}</div>` : '<div class="meta project-library-expand">展开详情与来源</div>'}</article>`).join('')}</div>` : '<div class="project-library-state" data-events-empty>暂无历史事件。</div>'}
        ${totalKnown && total > 0 ? pager('events',number(archive.events_page || 1),number(archive.events_pages || 1),total) : ''}</section>`;
      window.EvolvMemModules?.mountProject(target.querySelector('[data-project-modules]'), archive.project, {readOnly:archive.project_status === 'archived'});
      target.querySelectorAll('[data-project-event]').forEach(card => {
        const toggle = () => {
          const id = number(card.dataset.projectEvent);
          state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);
          renderDetail(); root.querySelector(`[data-project-event="${id}"]`)?.focus({preventScroll:true});
        };
        card.onclick = toggle;
        card.onkeydown = event => {if (event.key === 'Enter' || event.key === ' ') {event.preventDefault();toggle();}};
      });
      target.querySelectorAll('[data-events-page]').forEach(button => button.onclick = () => loadDetail(state.project,state.eventsPage + (button.dataset.eventsPage === 'next' ? 1 : -1)));
    }
    async function loadDetail(project, page=1) {
      cancel('detail'); const id = sequence.detail;
      state.project = project; state.eventsPage = page; state.detail = null;
      state.detailLoading = true; state.detailError = ''; state.expanded.clear();renderDetail();
      try {
        const data = await read('/api/project-archives/' + encodeURIComponent(project) + '?' + new URLSearchParams({events_page:page,events_page_size:100}), 'detail', id);
        if (!current('detail',id)) return;
        if (!Array.isArray(data.events)) throw new Error('invalid archive');
        state.detail = data; state.eventsPage = Math.max(1,number(data.events_page || page));
      } catch (error) {
        if (!current('detail',id)) return;
        state.detailError = errorText(error,'项目存档暂时无法读取，请重试。');
      } finally {
        if (current('detail',id)) {state.detailLoading = false;renderDetail();}
      }
    }
    function openProject(project) {
      if (!project || disposed) return;
      const sameProject = state.view === 'detail' && state.project === String(project);
      active = true; cancel('catalog'); state.catalogLoading = false;
      state.view = 'detail';showView();$('[data-project-back]').focus({preventScroll:true});
      if (sameProject && (state.detailLoading || state.detail)) {if(state.detail)renderDetail();return;}
      interrupted.clear();onProject(String(project));
      return loadDetail(String(project),1);
    }
    function showCatalog() {
      if (disposed) return;
      const wasDetail = state.view === 'detail';
      const reloadCatalog = interrupted.has('catalog');
      active = true;cancel('detail');state.detailLoading = false;state.view = 'catalog';syncInputs();showView();
      interrupted.clear();if(wasDetail)onProject('');
      if (state.catalogLoading) return;
      if (reloadCatalog || !state.catalog) return loadCatalog();
      renderCatalog();
      const button = [...root.querySelectorAll('[data-project-card]')].find(item => item.dataset.projectCard === state.focusProject);
      (button || $('#project-archive-form').elements.project).focus({preventScroll:true});
      window.scrollTo(0,state.returnScroll);
    }
    $('#project-archive-form').onsubmit = event => {event.preventDefault();state.filters.q=event.target.elements.project.value.trim();state.filters.page=1;loadCatalog();};
    for (const [selector,key] of [['[data-project-status]','status'],['[data-project-archive-filter]','archive'],['[data-project-sort]','sort']]) {
      $(selector).onchange = event => {state.filters.q=$('#project-archive-form').elements.project.value.trim();state.filters[key]=event.target.value;state.filters.page=1;loadCatalog();};
    }
    $('[data-project-back]').onclick = showCatalog;
    handle = {
      openProject, showCatalog,
      suspend() {active=false;for(const kind of ['catalog','detail']){if(state[kind+'Loading'])interrupted.add(kind);cancel(kind);state[kind+'Loading']=false;}},
      resume(project) {
        if(disposed)return;active=true;
        if(arguments.length && (!project || state.project !== String(project) || state.view !== 'detail')) {
          return project ? openProject(project) : showCatalog();
        }
        if(state.view==='detail') {
          if(interrupted.has('detail')||(!state.detail&&!state.detailLoading))loadDetail(state.project,state.eventsPage);
          else renderDetail();
        } else if(interrupted.has('catalog')||(!state.catalog&&!state.catalogLoading))loadCatalog();
        else renderCatalog();
        interrupted.clear();
      },
      dispose() {disposed=true;active=false;cancel('catalog');cancel('detail');if(instances.get(mount)===handle)instances.delete(mount);},
    };
    instances.set(mount,handle);syncInputs();if(initialProject)openProject(initialProject);else loadCatalog();return handle;
  }};
})();

// Shared by project details and expanded capability cards. State stays in memory
// across route mounts; only explicit user actions may send writes.
(() => {
  'use strict';
  const projects = new Map(), memories = new Map(), owners = new WeakMap();
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const canWrite = () => window.EvolvAuth?.canWrite === true;
  const statusName = status => ({active:'使用中',pending:'待确认建议',inactive:'已停用'}[status] || '状态待核对');
  const reasonName = reason => ({archive_unavailable:'原文已不可读取',source_revision_changed:'来源内容已有新版本',task_superseded:'来源任务已有新版本',context_not_active:'来源资料已变更',project_mismatch:'项目归属已变更'}[reason] || '来源暂不可用于确认模块');
  const failure = error => ({revision_conflict:'内容已被更新，已读取最新版本。草稿已保留，请核对后再保存。',module_revision_conflict:'内容已被更新，已读取最新版本。草稿已保留，请核对后再保存。',module_name_conflict:'此项目已有同名模块，请使用另一个名称。',quote_not_verifiable:'选中的原话已无法核验，请重新选择来源原话。',module_project_mismatch:'来源的当前项目归属已变化，请重新核对。',module_source_memory_mismatch:'来源与这条资料不一致，请重新读取。',forbidden:'当前为只读模式，无法保存。',read_only:'当前为只读模式，无法保存。'}[error.code] || '暂时无法完成操作，请重试。');
  const conflict = error => error.status === 409 || String(error.code).includes('revision');
  async function request(url, body) {
    if (body !== undefined && !canWrite()) throw Object.assign(new Error('read_only'),{code:'read_only'});
    const controller = new AbortController(), timer = setTimeout(()=>controller.abort(),15000);
    try {
      const send = window.EvolvAuth?.fetch ? window.EvolvAuth.fetch.bind(window.EvolvAuth) : window.fetch.bind(window);
      const response = await send(url, {signal:controller.signal,...(body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
      const data = await response.json();
      if (!response.ok || data.ok === false) throw Object.assign(new Error('request_failed'),{code:data.error,status:response.status});
      return data;
    } finally {clearTimeout(timer);}
  }
  function stateFor(map, key) {
    if (!map.has(key)) map.set(key,{key,data:null,loading:false,promise:null,busy:false,notice:'',error:'',draft:null,views:new Set()});
    // Detached, clean records can be reloaded; never evict a draft or live view.
    if (map.size > 80) for (const [id,state] of map) {
      if (id !== key && !state.draft && !state.busy && !state.loading && ![...state.views].some(el=>el.isConnected)) {map.delete(id);break;}
    }
    return map.get(key);
  }
  function attach(element, state, render, options={}) {
    const prior = owners.get(element);if(prior && prior.state !== state)prior.state.views.delete(element);
    owners.set(element,{state,render,options});state.views.add(element);render(element,state,options);
  }
  function notify(state) {
    for (const element of state.views) {
      if (!element.isConnected || owners.get(element)?.state !== state) {state.views.delete(element);continue;}
      const view=owners.get(element);view.render(element,state,view.options);
    }
  }
  async function loadProject(state, force=false) {
    if(state.promise)return state.promise;if(state.data && !force)return state.data;
    state.loading=true;state.error='';notify(state);
    state.promise=request('/api/project-modules?'+new URLSearchParams({project:state.key,status:'all'}))
      .then(data=>{if(!Array.isArray(data.items))throw new Error('invalid_modules');state.data=data;return data;})
      .catch(error=>{state.error='业务模块暂时无法读取，请重试。';throw error;})
      .finally(()=>{state.loading=false;state.promise=null;notify(state);for(const memory of memories.values())notify(memory);});
    return state.promise;
  }
  function notice(state, attr='data-module-notice') {
    return state.notice ? `<p class="module-notice" ${attr} role="status" tabindex="-1">${esc(state.notice)}</p>` : '';
  }
  function focusNotice(state, selector) {for(const el of state.views)if(el.isConnected){el.querySelector(selector)?.focus({preventScroll:true});break;}}
  async function changeProject(state, body, moduleId) {
    if(!canWrite() || state.busy)return;state.busy=true;state.notice='';notify(state);
    try {
      await request('/api/project-modules'+(moduleId?'/'+encodeURIComponent(moduleId):''),body);
      state.draft=null;await loadProject(state,true);
      await Promise.allSettled([...memories.values()].filter(memory=>memory.data?.sources.some(source=>source.projects?.includes(state.key))&&[...memory.views].some(el=>el.isConnected)).map(memory=>loadMemory(memory,true)));
      state.notice='已保存。';
    } catch(error) {
      if(conflict(error)) {try{await loadProject(state,true);}catch(_){state.notice='版本已变化，但最新内容暂未读到。草稿已保留，请重试读取。';}}
      state.notice=state.notice||(conflict(error)?'内容已被更新，已读取最新版本。草稿已保留，请核对后再保存。':failure(error));
    } finally {state.busy=false;notify(state);focusNotice(state,'[data-module-notice]');}
  }
  function renderProject(element,state,options) {
    const writable=canWrite()&&!options.readOnly, items=state.data?.items||[];
    element.classList.add('module-workbench');
    element.innerHTML=`<div class="module-heading"><div><h3 class="panel-title">业务模块</h3><p class="module-help">按项目业务划分；前端、后端等技术经验在能力库中单独分类。</p></div>${writable?`<button type="button" class="btn" data-module-new ${state.busy?'disabled':''}>新增模块</button>`:''}</div>
      ${notice(state)}${state.error?`<p class="module-help" role="status">${esc(state.error)} <button class="btn" type="button" data-module-retry>重试</button></p>`:''}
      ${state.draft&&writable?`<form data-module-form class="module-editor"><h4>${state.draft.id?'编辑业务模块':'新增业务模块'}</h4><label>模块名称<input name="name" maxlength="100" required value="${esc(state.draft.name)}" autocomplete="off"></label><label>别名<span class="module-help">多个别名用逗号分隔</span><input name="aliases" maxlength="600" value="${esc(state.draft.aliases)}"></label><label class="module-wide">业务范围<textarea name="description" rows="3" maxlength="2000">${esc(state.draft.description)}</textarea></label><div class="module-actions module-wide"><button class="btn primary" type="submit" ${state.busy?'disabled':''}>${state.busy?'正在保存…':'保存模块'}</button><button class="btn" type="button" data-module-cancel ${state.busy?'disabled':''}>取消</button></div></form>`:''}
      ${state.loading&&!state.data?'<p class="module-help" role="status">正在读取业务模块…</p>':state.data?(items.length?`<div class="module-grid">${items.map(item=>`<article class="module-card" data-module-card="${esc(item.module_id)}"><div class="module-heading"><h4>${esc(item.name)}</h4><span class="module-status ${item.status==='pending'?'module-pending':''}">${esc(statusName(item.status))}</span></div>${item.aliases?.length?`<p class="module-help">别名：${esc(item.aliases.join('、'))}</p>`:''}<p>${esc(item.description||'尚未填写业务范围。')}</p>${writable?`<div class="module-actions"><button class="btn" type="button" data-module-edit="${esc(item.module_id)}" ${state.busy?'disabled':''}>编辑</button><button class="btn" type="button" data-module-state="${esc(item.module_id)}" ${state.busy?'disabled':''}>${item.status==='active'?'停用':item.status==='pending'?'确认使用':'重新启用'}</button></div>`:''}</article>`).join('')}</div>`:'<div class="module-empty">尚未划分业务模块。可以按真实业务新增，未划分的资料会继续保留。</div>'):''}`;
    element.querySelector('[data-module-retry]')?.addEventListener('click',()=>loadProject(state,true).catch(()=>{}));
    if(!writable)return;
    element.querySelector('[data-module-new]').onclick=()=>{if(!canWrite()||state.busy)return;state.draft={id:'',name:'',aliases:'',description:''};state.notice='';notify(state);element.querySelector('[name=name]')?.focus();};
    element.querySelector('[data-module-cancel]')?.addEventListener('click',()=>{if(state.busy)return;state.draft=null;notify(state);});
    const form=element.querySelector('[data-module-form]');
    if(form) {
      form.oninput=()=>{for(const key of ['name','aliases','description'])state.draft[key]=form.elements[key].value;};
      form.onsubmit=event=>{event.preventDefault();if(!canWrite()||state.busy||!form.reportValidity())return;form.oninput();const draft=state.draft,item=items.find(row=>row.module_id===draft.id);
        if(draft.id&&!item){state.notice='该模块已变化，请重新读取后再编辑。';notify(state);return;}
        changeProject(state,{...(item?{action:'update',expected_revision:item.revision}:{project:state.key}),name:draft.name.trim(),aliases:draft.aliases.split(/[,，、\n]/).map(x=>x.trim()).filter(Boolean),description:draft.description.trim()},draft.id);};
    }
    element.querySelectorAll('[data-module-edit]').forEach(button=>button.onclick=()=>{if(!canWrite()||state.busy)return;const item=items.find(row=>row.module_id===button.dataset.moduleEdit);state.draft={id:item.module_id,name:item.name,aliases:(item.aliases||[]).join('，'),description:item.description||''};state.notice='';notify(state);element.querySelector('[name=name]')?.focus();});
    element.querySelectorAll('[data-module-state]').forEach(button=>button.onclick=()=>{if(!canWrite()||state.busy)return;const item=items.find(row=>row.module_id===button.dataset.moduleState);changeProject(state,{action:item.status==='active'?'deactivate':'activate',expected_revision:item.revision},item.module_id);});
  }
  async function loadMemory(state,force=false) {
    if(state.promise)return state.promise;if(state.data&&!force)return state.data;
    state.loading=true;state.error='';notify(state);
    state.promise=request('/api/project-modules/memory/'+encodeURIComponent(state.key)).then(async data=>{
      if(!Array.isArray(data.modules)||!Array.isArray(data.sources))throw new Error('invalid_sources');state.data=data;
      const source=data.sources.find(row=>row.valid&&row.projects?.length&&row.quote_text);
      if(!state.draft)state.draft={sourceId:source?.source_id??'',project:source?.projects[0]||'',moduleId:'',quote:''};
      const projectNames=[...new Set(data.sources.filter(row=>row.valid).flatMap(row=>row.projects||[]))];
      await Promise.allSettled(projectNames.map(project=>loadProject(stateFor(projects,project))));return data;
    }).catch(error=>{state.error='模块关联暂时无法读取，请重试。';throw error;}).finally(()=>{state.loading=false;state.promise=null;notify(state);});
    return state.promise;
  }
  async function changeMemory(state,url,body) {
    if(!canWrite()||state.busy)return;state.busy=true;state.notice='';notify(state);
    try{await request(url,body);state.draft.quote='';await loadMemory(state,true);state.notice='模块归属已保存。';}
    catch(error){if(conflict(error)){try{await loadMemory(state,true);}catch(_){state.notice='版本已变化，但最新来源暂未读到。草稿已保留，请重试读取。';}}state.notice=state.notice||(conflict(error)?'内容已被更新，已读取最新版本。草稿已保留，请核对后再保存。':failure(error));}
    finally{state.busy=false;notify(state);focusNotice(state,'[data-memory-module-notice]');}
  }
  function renderMemory(element,state) {
    element.classList.add('module-memory');const data=state.data;
    if(!data){element.innerHTML=`<h4>业务模块</h4><p class="module-help" role="status">${esc(state.error||'正在读取模块关联…')}</p>${state.error?'<button class="btn" type="button" data-memory-retry>重试</button>':''}`;element.querySelector('[data-memory-retry]')?.addEventListener('click',()=>loadMemory(state,true).catch(()=>{}));return;}
    const writable=canWrite()&&!['archived','ignored'].includes(data.status),draft=state.draft||{},sources=data.sources.filter(row=>row.valid&&row.projects?.length&&row.quote_text),source=sources.find(row=>String(row.source_id)===String(draft.sourceId));
    const projectValid=source?.projects.includes(draft.project), projectState=projectValid?stateFor(projects,draft.project):null, items=(projectState?.data?.items||[]).filter(row=>row.status==='active');
    element.innerHTML=`<div class="module-heading"><h4>业务模块</h4>${data.status==='pending'?'<span class="module-status module-pending">待审核资料 · 仅作归类</span>':''}</div><p class="module-help">模块归属依据当前有效来源；原文或项目归属变化后会重新核验。</p>${notice(state,'data-memory-module-notice')}
      ${data.modules.length?`<div class="module-memory-list">${data.modules.map(item=>{const bindings=(data.bindings||[]).filter(row=>item.binding_ids?.includes(row.binding_id)&&row.status==='active');return `<article data-memory-module="${esc(item.module_id)}" class="module-memory-item"><div><strong>${esc(item.name)}</strong><span class="module-help"> · ${esc(item.project)}</span></div>${writable?bindings.map(binding=>`<button class="btn" type="button" data-module-remove="${esc(binding.binding_id)}" ${state.busy?'disabled':''}>移除此归属${bindings.length>1?' '+esc(bindings.indexOf(binding)+1):''}</button>`).join(''):''}</article>`;}).join('')}</div>`:'<p class="module-empty">尚未分配业务模块。</p>'}
      ${data.sources.some(row=>!row.valid)?`<p class="module-source-warning">${esc([...new Set(data.sources.filter(row=>!row.valid).map(row=>reasonName(row.reason)))].join('；'))}。这些来源保留供查看，不用于确认模块。</p>`:''}
      ${writable&&sources.length?`<form data-memory-module-form class="module-binding-form"><label>依据来源<select data-module-source ${state.busy?'disabled':''}>${sources.map((row,index)=>`<option value="${esc(row.source_id)}" ${String(row.source_id)===String(draft.sourceId)?'selected':''}>来源 ${index+1} · ${esc((row.projects||[]).join('、'))}</option>`).join('')}</select></label><label>所属项目<select data-module-project ${state.busy?'disabled':''}>${(source?.projects||[]).map(project=>`<option value="${esc(project)}" ${project===draft.project?'selected':''}>${esc(project)}</option>`).join('')}</select></label>
        <label class="module-wide">来源原文<span class="module-help">选中能说明业务模块的原话，再点击“使用选中原话”。</span><textarea data-module-original rows="4" readonly>${esc(source?.quote_text||'')}</textarea></label><div class="module-wide"><button class="btn" type="button" data-use-module-quote ${state.busy?'disabled':''}>使用选中原话</button></div><label class="module-wide">所选原话<textarea data-module-quote rows="2" readonly aria-label="所选原话">${esc(draft.quote||'')}</textarea></label>
        <label class="module-wide">业务模块<select data-module-target required ${state.busy?'disabled':''}><option value="">选择已登记的业务模块</option>${items.map(item=>`<option value="${esc(item.module_id)}" ${item.module_id===draft.moduleId?'selected':''}>${esc(item.name)}</option>`).join('')}</select></label>${projectState?.error?'<p class="module-help module-wide">模块列表暂不可读取，请重新读取来源后再试。</p>':!items.length?'<p class="module-help module-wide">此项目尚无使用中的业务模块，可先到项目详情登记。</p>':''}<div class="module-actions module-wide"><button class="btn primary" type="submit" ${state.busy||!items.length?'disabled':''}>${state.busy?'正在保存…':'确认模块归属'}</button></div></form>`:writable?'<p class="module-help">当前没有可核验且已确认项目的来源原话，暂不能确认模块归属。</p>':''}`;
    if(!writable)return;
    element.querySelectorAll('[data-module-remove]').forEach(button=>button.onclick=()=>{if(!canWrite()||state.busy)return;const binding=(data.bindings||[]).find(row=>String(row.binding_id)===button.dataset.moduleRemove);if(binding&&Number.isInteger(binding.revision))changeMemory(state,'/api/project-modules/bindings/'+binding.binding_id+'/remove',{expected_revision:binding.revision});});
    const form=element.querySelector('[data-memory-module-form]');if(!form)return;
    form.querySelector('[data-module-source]').onchange=event=>{const selected=sources.find(row=>String(row.source_id)===event.target.value);state.draft={sourceId:selected.source_id,project:selected.projects[0],moduleId:'',quote:''};state.notice='';notify(state);};
    form.querySelector('[data-module-project]').onchange=event=>{draft.project=event.target.value;draft.moduleId='';state.notice='';notify(state);};
    form.querySelector('[data-module-target]').onchange=event=>{draft.moduleId=event.target.value;};
    form.querySelector('[data-use-module-quote]').onclick=()=>{const original=form.querySelector('[data-module-original]'),quote=original.value.slice(original.selectionStart,original.selectionEnd);if(!quote.trim()){state.notice='请先在来源原文中选中一段原话。';}else{draft.quote=quote;state.notice='';}notify(state);};
    form.onsubmit=event=>{event.preventDefault();if(!canWrite()||state.busy||!form.reportValidity())return;
      if(!source||!projectValid||!draft.quote?.trim()||!source.quote_text.includes(draft.quote)||!items.some(row=>row.module_id===draft.moduleId)){state.notice='请选择已登记模块，并从当前来源选取可核验的原话。';notify(state);focusNotice(state,'[data-memory-module-notice]');return;}
      const binding=(data.bindings||[]).find(row=>row.module_id===draft.moduleId&&row.capability_source_id===source.source_id);
      if(binding&&!Number.isInteger(binding.revision)){state.notice='归属版本暂不可核验，请重新读取。';notify(state);return;}
      changeMemory(state,'/api/project-modules/'+encodeURIComponent(draft.moduleId)+'/bindings',{memory_id:Number(state.key),source_id:source.source_id,quote:draft.quote,expected_revision:binding?binding.revision:0});};
  }
  window.EvolvMemModules={
    mountProject(element,project,options={}) {if(!element||!project)return Promise.resolve();const state=stateFor(projects,String(project));attach(element,state,renderProject,options);return loadProject(state).catch(()=>{});},
    mountMemory(element,memoryId) {if(!element||!Number.isSafeInteger(Number(memoryId))||Number(memoryId)<1)return Promise.reject(new Error('invalid_memory'));const state=stateFor(memories,String(memoryId));attach(element,state,renderMemory);return loadMemory(state);},
  };
})();
