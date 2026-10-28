(() => {
  'use strict';
  window.EvolvProjectArchives = {async render({mount, esc}) {
    const state = {archive:null,expanded:new Set(),error:'',busy:false};
    const summaryLine = line => {
      const text = line.trim();
      if (!text) return '';
      if (/[{}\[\]]/.test(text) && /":/.test(text)) return '〔结构化任务记录已省略，点击事件查看来源〕';
      return text.length > 420 ? text.slice(0,417) + '…' : text;
    };
    const renderSummary = archive => {
      const sections = [];
      let current = {title:'项目概述',lines:[]};
      for (const raw of archive.summary.split(/\r?\n/)) {
        const heading = raw.match(/^##+\s+(.+)$/);
        if (heading) {
          if (current.lines.length) sections.push(current);
          current = {title:heading[1],lines:[]};
        } else {
          const text = summaryLine(raw.replace(/^#\s+/, '').replace(/^[-*]\s+/, ''));
          if (text) current.lines.push(text);
        }
      }
      if (current.lines.length) sections.push(current);
      return sections.map((section,index) => `<section class="refresh-summary-section" data-summary-section="${index}"><h3>${esc(section.title)}</h3>${section.lines.map(line => `<p>${esc(line)}</p>`).join('')}</section>`).join('');
    };
    const renderArchive = archive => `<article class="panel" data-archive-summary><div class="summary-header"><div><h2>${esc(archive.display_name || archive.project)}</h2><p class="subtitle">开始于 ${esc(archive.started_at || '未确认')} · 开发周期 ${archive.development_days} 天 · 修订 ${archive.revision}</p></div><span class="tag">${archive.events.length} 项历史事件</span></div>${renderSummary(archive)}</article>
      <section class="panel"><h3 class="panel-title">历史事件</h3><div class="timeline">${archive.events.map(event => `<article class="event" data-project-event="${event.event_id}" tabindex="0"><h4>${esc(event.title)}</h4><div class="meta">${esc(event.event_type)} · ${esc(event.occurred_at || event.recorded_at)}</div>${state.expanded.has(event.event_id) ? `<div class="event-detail" data-event-source>${esc(event.content)}<br><code>${esc(event.source_ref)}</code></div>` : '<div class="meta" style="margin-top:8px">点击展开详情</div>'}</article>`).join('')}</div></section>`;
    const bind = () => {
      mount.querySelectorAll('[data-project-event]').forEach(card => {
        const toggle = () => {const id = Number(card.dataset.projectEvent);state.expanded.has(id) ? state.expanded.delete(id) : state.expanded.add(id);renderResult();};
        card.onclick = toggle;
        card.onkeydown = event => {if (event.key === 'Enter' || event.key === ' ') {event.preventDefault();toggle();}};
      });
    };
    const renderResult = () => {
      const result = mount.querySelector('#project-archive-result');
      result.innerHTML = state.error ? `<div class="empty">${esc(state.error)}</div>` : state.archive ? renderArchive(state.archive) : '<div class="empty">输入项目名称后查看当前摘要和历史事件。</div>';
      bind();
    };
    mount.innerHTML = `<header class="data-page-header"><h1>项目存档</h1><p>查询开发时间、周期、重大改动、功能更新和已解决问题。</p></header>
      <form id="project-archive-form" class="panel toolbar"><input class="input" name="project" required placeholder="输入项目名称或别名" aria-label="项目名称"><button class="btn primary">查询项目</button></form><div id="project-archive-result" aria-live="polite"></div>`;
    mount.querySelector('form').onsubmit = async event => {
      event.preventDefault();
      const project = event.target.elements.project.value.trim();
      if (!project || state.busy) return;
      state.busy = true;
      state.error = '';
      mount.querySelector('#project-archive-result').innerHTML = '<div class="ui-loading">正在读取项目存档…</div>';
      try {
        const response = await EvolvAuth.fetch('/api/project-archives/' + encodeURIComponent(project));
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || '项目存档读取失败');
        state.archive = data;
        state.expanded.clear();
      } catch (error) {
        state.error = error.message;
      } finally {
        state.busy = false;
        renderResult();
      }
    };
    renderResult();
  }};
})();
