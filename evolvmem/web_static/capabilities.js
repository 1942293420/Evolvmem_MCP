(() => {
  'use strict';
  window.EvolvCapabilities = {async render({mount, esc, toast}) {
    const state = {query:'',category:'all',source:'all',page:1,tab:'formal',data:null,expanded:new Set(),notice:'',sequence:0};
    const request = async (path, body) => {
      const response = await EvolvAuth.fetch(path, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {});
      const result = await response.json();
      if (!response.ok || result.ok === false) throw new Error(result.error || '操作失败');
      return result;
    };
    const labels = () => Object.fromEntries((state.data?.categories || []).map(item => [item.id,item.label]));
    const notify = text => { state.notice = text; toast?.(text); };
    const load = async () => {
      const requestId = ++state.sequence;
      mount.setAttribute('aria-busy','true');
      try {
        const params = new URLSearchParams({q:state.query,category:state.category === 'all' ? '' : state.category,source_project:state.source === 'all' ? '' : state.source,page:state.page,page_size:20});
        const data = await request('/api/capabilities?' + params);
        if (requestId !== state.sequence) return;
        state.data = data;
        render();
      } catch (error) {
        if (requestId !== state.sequence) return;
        state.notice = '读取失败：' + error.message;
        render();
      } finally {
        if (requestId === state.sequence) mount.setAttribute('aria-busy','false');
      }
    };
    const tabs = () => `<div class="chips">${[['formal','正式记忆'],['intake',`待分类 ${state.data?.intake.length || 0}`],['suggestions',`分类建议 ${state.data?.suggestions.length || 0}`]].map(([id,label]) => `<button class="chip ${state.tab === id ? 'active' : ''}" data-tab="${id}">${label}</button>`).join('')}</div>`;
    const memoryCard = item => {
      const expanded = state.expanded.has(item.memory_id);
      return `<article class="memory-card" data-memory-card="${item.memory_id}" data-memory="${item.memory_id}" tabindex="0"><h3>${esc(item.title)}</h3><p>${esc(item.content)}</p><div class="meta"><span class="tag">${esc(labels()[item.category] || item.category)}</span>${(item.keywords || []).map(keyword => `<span class="tag">${esc(keyword)}</span>`).join('')}</div><div class="meta" style="margin-top:8px">来源项目：${esc(item.source_project || '未记录')}</div>${expanded ? `<div class="source-detail" data-memory-source-detail>原始来源：${esc(item.source_ref || '未记录')}<br>记忆编号：${item.memory_id}</div>` : '<div class="meta" style="margin-top:8px">点击卡片展开原始来源</div>'}</article>`;
    };
    const renderFormal = () => {
      const data = state.data;
      const sources = [...new Set(data.items.map(item => item.source_project).concat(['bluewhale','integration']))].filter(Boolean);
      return `<section class="panel"><div class="toolbar"><input class="input" data-memory-query value="${esc(state.query)}" placeholder="搜索记忆、技术栈或来源项目"><select class="select" data-memory-source><option value="all">全部来源项目</option>${sources.map(source => `<option value="${esc(source)}" ${state.source === source ? 'selected' : ''}>${esc(source)}</option>`).join('')}</select><button class="btn primary" data-memory-search>搜索</button></div></section>
        <div class="chips"><button class="chip ${state.category === 'all' ? 'active' : ''}" data-category="all">全部 ${Object.values(data.counts).reduce((a,b) => a + b,0)}</button>${data.categories.map(item => `<button class="chip ${state.category === item.id ? 'active' : ''}" data-category="${item.id}">${esc(item.label)} ${data.counts[item.id] || 0}</button>`).join('')}</div>
        <section class="panel" style="margin-top:14px"><div class="memory-grid">${data.items.map(memoryCard).join('') || '<div class="empty">没有符合条件的记忆，请调整筛选。</div>'}</div><div class="pager"><button class="btn small" data-page="prev" ${data.page <= 1 ? 'disabled' : ''}>上一页</button><span data-page-status>第 ${data.page} / ${data.pages} 页 · 共 ${data.total} 条</span><button class="btn small" data-page="next" ${data.page >= data.pages ? 'disabled' : ''}>下一页</button></div></section>`;
    };
    const renderIntake = () => `<section class="panel"><h3 class="panel-title">待分类暂存</h3>${state.data.intake.map(item => `<article class="memory-card"><h3>${esc(item.title)}</h3><p>${esc(item.content)}</p><div class="meta">来源项目：${esc(item.source_project || '未记录')} · 建议：${esc(item.suggested_category || '无')}</div><div class="toolbar" style="margin-top:12px"><select class="select" data-intake-category="${item.intake_id}">${state.data.categories.map(category => `<option value="${category.id}">${esc(category.label)}</option>`).join('')}</select><button class="btn primary small" data-assign="${item.intake_id}" data-revision="${item.revision}">确认归类</button></div></article>`).join('') || '<div class="empty">待分类已清空。</div>'}</section>`;
    const renderSuggestions = () => `<section class="panel"><h3 class="panel-title">新增分类建议</h3>${state.data.suggestions.map(item => `<article class="memory-card"><h3>${esc(item.label)}</h3><p>${esc(item.reason)}</p><div class="toolbar"><button class="btn primary small" data-suggestion="accept" data-id="${item.suggestion_id}">接受并创建</button><button class="btn small" data-suggestion="reject" data-id="${item.suggestion_id}">拒绝</button></div></article>`).join('') || '<div class="empty">暂无新增分类建议。</div>'}</section>`;
    const bind = () => {
      mount.querySelectorAll('[data-tab]').forEach(button => button.onclick = () => {state.tab = button.dataset.tab;render();});
      mount.querySelectorAll('[data-category]').forEach(button => button.onclick = () => {state.category = button.dataset.category;state.page = 1;load();});
      mount.querySelector('[data-memory-search]')?.addEventListener('click', () => {state.query = mount.querySelector('[data-memory-query]').value;state.page = 1;load();});
      mount.querySelector('[data-memory-query]')?.addEventListener('keydown', event => {if (event.key === 'Enter') {event.preventDefault();mount.querySelector('[data-memory-search]').click();}});
      mount.querySelector('[data-memory-source]')?.addEventListener('change', event => {state.source = event.target.value;state.page = 1;load();});
      mount.querySelectorAll('[data-page]').forEach(button => button.onclick = () => {state.page += button.dataset.page === 'next' ? 1 : -1;load();});
      mount.querySelectorAll('[data-memory-card]').forEach(card => {
        const toggle = () => {const id = Number(card.dataset.memoryCard);state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);render();};
        card.onclick = toggle;
        card.onkeydown = event => {if (event.key === 'Enter' || event.key === ' ') {event.preventDefault();toggle();}};
      });
      mount.querySelectorAll('[data-assign]').forEach(button => button.onclick = async () => {
        button.disabled = true;
        try {
          const category = mount.querySelector(`[data-intake-category="${button.dataset.assign}"]`).value;
          await request(`/api/capabilities/intake/${button.dataset.assign}/assign`,{category,expected_revision:Number(button.dataset.revision)});
          notify('已归入正式能力记忆');
          state.tab = 'formal';
          await load();
        } catch (error) {button.disabled = false;notify(error.message);render();}
      });
      mount.querySelectorAll('[data-suggestion]').forEach(button => button.onclick = async () => {
        button.disabled = true;
        try {
          await request(`/api/capability-categories/suggestions/${button.dataset.id}/${button.dataset.suggestion}`,{});
          notify(button.dataset.suggestion === 'accept' ? '已接受建议并创建新分类' : '已拒绝分类建议');
          await load();
        } catch (error) {button.disabled = false;notify(error.message);render();}
      });
    };
    function render() {
      mount.innerHTML = `<header class="data-page-header"><h1>开发能力记忆</h1><p>按能力查找历史经验，来源项目仅用于追溯</p></header>${state.notice ? `<div class="refresh-notice" role="status">${esc(state.notice)}</div>` : ''}${tabs()}${state.tab === 'formal' ? renderFormal() : state.tab === 'intake' ? renderIntake() : renderSuggestions()}`;
      bind();
    }
    await load();
  }};
})();
