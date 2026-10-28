(() => {
  'use strict';
  window.EvolvCapabilities = {async render({mount, esc, toast}) {
    const request = async (path, body) => {
      const response = await EvolvAuth.fetch(path, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {});
      const result = await response.json();
      if (!response.ok || result.ok === false) throw new Error(result.error || '操作失败');
      return result;
    };
    const load = async () => {
      mount.setAttribute('aria-busy', 'true');
      try {
        const data = await request('/api/capabilities');
        const labels = Object.fromEntries(data.categories.map(c => [c.id, c.label]));
        const options = data.categories.map(c => `<option value="${esc(c.id)}">${esc(c.label)}</option>`).join('');
        mount.innerHTML = `<header class="data-page-header"><h1>开发能力记忆</h1><p>按能力保存和检索；来源项目只用于追溯。</p></header>
          <section class="panel"><div class="section-head"><h2>能力分类</h2><span class="hint">${data.categories.length} 类</span></div><div class="flow-tags">${data.categories.map(c => `<span>${esc(c.label)}</span>`).join('')}</div></section>
          <section class="panel"><div class="section-head"><h2>正式记忆</h2><span class="hint">${data.total} 条</span></div>${data.items.length ? data.items.map(memory => `<article data-capability-item="${memory.memory_id}" class="panel"><h3>${esc(memory.title)}</h3><p>${esc(memory.content)}</p><dl><dt>能力分类</dt><dd>${esc(labels[memory.category] || memory.category)}</dd><dt>来源项目</dt><dd>${esc(memory.source_project || '未记录')}</dd><dt>来源记录</dt><dd>${esc(memory.source_ref || '未记录')}</dd></dl></article>`).join('') : '<div class="empty">暂无正式能力记忆。</div>'}</section>
          <section class="panel"><div class="section-head"><h2>待分类暂存</h2><span class="hint">${data.intake.length} 条</span></div>${data.intake.map(item => `<article data-capability-intake="${item.intake_id}"><h3>${esc(item.title)}</h3><p>${esc(item.content)}</p><p class="hint">来源项目：${esc(item.source_project || '未记录')} · 建议：${esc(item.suggested_category || '无')}</p><select aria-label="选择能力分类">${options}</select><button data-capability-assign="${item.intake_id}" data-revision="${item.revision}">确认归类</button></article>`).join('') || '<div class="empty">暂无待分类内容。</div>'}</section>
          <section class="panel"><div class="section-head"><h2>新增分类建议</h2><span class="hint">${data.suggestions.length} 条</span></div>${data.suggestions.map(item => `<article data-category-suggestion="${item.suggestion_id}"><strong>${esc(item.label)}</strong><p>${esc(item.reason)}</p><button data-suggestion-action="accept" data-id="${item.suggestion_id}">接受并创建</button><button data-suggestion-action="reject" data-id="${item.suggestion_id}">拒绝</button></article>`).join('') || '<div class="empty">暂无新增分类建议。</div>'}</section>`;
      } catch (error) {
        mount.innerHTML = `<div class="empty">读取失败：${esc(error.message)}</div>`;
      } finally { mount.setAttribute('aria-busy', 'false'); }
    };
    mount.onclick = async event => {
      const assign = event.target.closest('[data-capability-assign]');
      const suggestion = event.target.closest('[data-suggestion-action]');
      try {
        if (assign) {
          const item = assign.closest('[data-capability-intake]');
          await request(`/api/capabilities/intake/${assign.dataset.capabilityAssign}/assign`, {category:item.querySelector('select').value, expected_revision:Number(assign.dataset.revision)});
          toast?.('已归入正式能力记忆');
          await load();
        } else if (suggestion) {
          await request(`/api/capability-categories/suggestions/${suggestion.dataset.id}/${suggestion.dataset.suggestionAction}`, {});
          toast?.('分类建议已处理');
          await load();
        }
      } catch (error) { toast?.(error.message); }
    };
    await load();
  }};
})();
