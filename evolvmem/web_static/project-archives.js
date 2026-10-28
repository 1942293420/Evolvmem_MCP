(() => {
  'use strict';
  window.EvolvProjectArchives = {async render({mount, esc}) {
    mount.innerHTML = `<header class="data-page-header"><h1>项目存档</h1><p>查询开发时间、周期、重大改动、功能更新和已解决问题。</p></header>
      <form id="project-archive-form" class="toolbar"><input name="project" required placeholder="输入项目名称或别名" aria-label="项目名称"><button class="primary">查询项目</button></form><div id="project-archive-result" aria-live="polite"></div>`;
    const result = mount.querySelector('#project-archive-result');
    mount.querySelector('form').onsubmit = async event => {
      event.preventDefault();
      const project = event.target.elements.project.value.trim();
      if (!project) return;
      result.innerHTML = '<div class="ui-loading">正在读取项目存档…</div>';
      try {
        const response = await EvolvAuth.fetch('/api/project-archives/' + encodeURIComponent(project));
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || '项目存档读取失败');
        result.innerHTML = `<article data-project-archive="${esc(data.project)}" class="panel"><h2>${esc(data.display_name || data.project)}</h2><p>开始时间：${esc(data.started_at || '未确认')} · 开发周期：${data.development_days} 天 · 修订：${data.revision}</p><pre class="project-archive-summary">${esc(data.summary)}</pre></article>
          <section class="panel"><h2>历史事件</h2>${data.events.map(item => `<article data-project-event="${item.event_id}"><h3>${esc(item.title)}</h3><p>${esc(item.content)}</p><p class="hint">${esc(item.event_type)} · ${esc(item.occurred_at || item.recorded_at)} · ${esc(item.source_ref)}</p></article>`).join('')}</section>`;
      } catch (error) { result.innerHTML = `<div class="empty">${esc(error.message)}</div>`; }
    };
  }};
})();
