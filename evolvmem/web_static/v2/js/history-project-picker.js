/* Filter the authorised project directory; never rename projects or move history. */
(function (root) {
  const E = root.EvolvMemV2, esc = E.esc;
  function normalise(value) { return String(value || '').normalize('NFKC').toLowerCase().replace(/[\s_\-/\\]+/g, ''); }
  function subsequence(text, term) {
    let pos = 0;
    for (const char of text) { if (char === term[pos]) pos++; if (pos === term.length) return true; }
    return false;
  }
  E.filterHistoryProjects = function (projects, query) {
    const terms = String(query || '').normalize('NFKC').trim().split(/\s+/).map(normalise).filter(Boolean);
    return (projects || []).filter(p => !p.deleted).map(function (p, order) {
      const title = normalise(p.title), key = normalise(p.key), text = title + key + normalise(p.workspacePath);
      if (!terms.every(term => text.includes(term) || subsequence(text, term))) return null;
      const joined = terms.join('');
      const rank = !joined ? 0 : title === joined || key === joined ? 0 : title.startsWith(joined) ? 1 : terms.every(term => text.includes(term)) ? 2 : 3;
      return { project: p, rank: rank, order: order };
    }).filter(Boolean).sort((a, b) => a.rank - b.rank || a.order - b.order).map(x => x.project);
  };
  function matches() { return E.filterHistoryProjects(E.state.historyProjects, E.state.histProjectQuery); }
  function optionsHTML() {
    const rows = matches(), s = E.state;
    if (!rows.length) return '<div class="project-picker-empty" role="status">没有找到' + (E.isProduction() ? '文件夹' : '项目') + '，换个关键词试试。</div>';
    return rows.map(function (p, i) {
      return '<button type="button" role="option" tabindex="-1" id="hist-proj-option-' + i + '" aria-selected="' + (p.key === s.selectedProjectKey) + '" class="project-picker-option ' + (i === s.histProjectActiveIndex ? 'keyboard-active' : '') + '" data-project-picker-key="' + esc(p.key) + '"><span>' + esc(p.title) + (p.workspacePath ? '<small class="muted" style="display:block;overflow-wrap:anywhere">' + esc(p.workspacePath) + '</small>' : '') + '</span><span class="muted">' + esc(p.blocks == null ? '未知' : p.blocks) + ' 片段</span></button>';
    }).join('');
  }
  E.renderHistoryProjectPicker = function () {
    const s = E.state, current = (s.historyProjects || []).find(p => !p.deleted && p.key === s.selectedProjectKey), label = E.isProduction() ? '文件夹' : '项目';
    return '<div id="hist-project-picker" class="history-project-picker">' +
      '<button type="button" id="hist-proj-toggle" class="project-picker-toggle" title="' + esc(current?.workspacePath || '') + '" aria-label="选择' + label + '，可输入查找" aria-haspopup="listbox" aria-controls="hist-proj-options" aria-expanded="' + !!s.histProjectOpen + '"><span>' + esc(current ? current.title + ' (' + current.blocks + ')' : '选择' + label) + '</span><span aria-hidden="true">▾</span></button>' +
      '<div id="hist-proj-popover" class="project-picker-popover" ' + (s.histProjectOpen ? '' : 'hidden') + '><div class="project-picker-search"><input type="search" id="hist-proj-search" role="combobox" aria-label="查找' + label + '" aria-autocomplete="list" aria-controls="hist-proj-options" aria-expanded="' + !!s.histProjectOpen + '" autocomplete="off" value="' + esc(s.histProjectQuery || '') + '" placeholder="输入' + label + '名或路径，可模糊查找…" /><button type="button" id="hist-proj-clear" class="btn btn-sm">清空</button></div>' +
      '<div id="hist-proj-match-count" class="project-picker-count" role="status" aria-live="polite">找到 ' + matches().length + ' 个' + label + '</div>' +
      '<div id="hist-proj-options" class="project-picker-options" role="listbox" aria-label="' + label + '匹配结果">' + optionsHTML() + '</div></div></div>';
  };
  const doc = root.document;
  if (!doc) return;
  function sync() {
    const s = E.state, open = !!s.histProjectOpen;
    const popover = doc.getElementById('hist-proj-popover'), toggle = doc.getElementById('hist-proj-toggle');
    const input = doc.getElementById('hist-proj-search'), options = doc.getElementById('hist-proj-options');
    if (!popover || !toggle || !input || !options) return;
    popover.hidden = !open; toggle.setAttribute('aria-expanded', String(open)); input.setAttribute('aria-expanded', String(open));
    if (input.value !== (s.histProjectQuery || '')) input.value = s.histProjectQuery || '';
    options.innerHTML = optionsHTML();
    doc.getElementById('hist-proj-match-count').textContent = '找到 ' + matches().length + ' 个' + (E.isProduction() ? '文件夹' : '项目');
    if (open && s.histProjectActiveIndex >= 0 && s.histProjectActiveIndex < matches().length) {
      input.setAttribute('aria-activedescendant', 'hist-proj-option-' + s.histProjectActiveIndex);
      doc.getElementById('hist-proj-option-' + s.histProjectActiveIndex)?.scrollIntoView({ block: 'nearest' });
    } else input.removeAttribute('aria-activedescendant');
  }
  function choose(key) {
    const s = E.state, project = (s.historyProjects || []).find(p => !p.deleted && p.key === key);
    if (!project || (E.session && (!E.session.isReady() || !E.session.dataReady))) return;
    s.selectedProjectKey = key; s.histProjectOpen = false; s.histProjectQuery = ''; s.histProjectActiveIndex = -1;
    s.historyBlocks = []; s.selectedBlockId = null; s.libraryEpoch = (s.libraryEpoch || 0) + 1;
    const epoch = s.libraryEpoch, ticket = E.session ? E.session.capture() : null;
    E.renderAll(); doc.getElementById('hist-proj-toggle')?.focus();
    if (s.apiMode === 'live' && E.api) E.api.loadHistory().then(function () {
      if (E.state !== s || s.libraryEpoch !== epoch || (ticket && !E.session?.isCurrent(ticket))) return;
      E.renderAll();
    }).catch(function () { if (E.state === s && s.libraryEpoch === epoch) E.toast('项目会话读取失败，请重试。', 'danger'); });
  }
  doc.addEventListener('click', function (ev) {
    if (ev.target.closest('#hist-proj-toggle')) { E.state.histProjectOpen = !E.state.histProjectOpen; E.state.histProjectActiveIndex = -1; sync(); if (E.state.histProjectOpen) doc.getElementById('hist-proj-search')?.focus(); return; }
    if (ev.target.closest('#hist-proj-clear')) { E.state.histProjectQuery = ''; E.state.histProjectActiveIndex = -1; sync(); doc.getElementById('hist-proj-search')?.focus(); return; }
    const option = ev.target.closest('[data-project-picker-key]');
    if (option) { choose(option.getAttribute('data-project-picker-key')); return; }
    if (!ev.target.closest('#hist-project-picker') && E.state.histProjectOpen) { E.state.histProjectOpen = false; sync(); }
  });
  doc.addEventListener('input', function (ev) {
    if (ev.target.id !== 'hist-proj-search') return;
    E.state.histProjectQuery = ev.target.value; E.state.histProjectActiveIndex = -1; sync();
  });
  doc.addEventListener('focusin', function (ev) {
    if (E.state.histProjectOpen && !ev.target.closest('#hist-project-picker')) { E.state.histProjectOpen = false; sync(); }
  });
  doc.addEventListener('keydown', function (ev) {
    if (!E.state.histProjectOpen || ev.isComposing || ev.keyCode === 229 || !ev.target.closest('#hist-project-picker')) return;
    if (ev.key === 'Escape') { ev.preventDefault(); ev.stopImmediatePropagation(); E.state.histProjectOpen = false; sync(); doc.getElementById('hist-proj-toggle')?.focus(); return; }
    if (ev.target.id !== 'hist-proj-search') return;
    const rows = matches();
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault(); const index = E.state.histProjectActiveIndex == null ? -1 : E.state.histProjectActiveIndex;
      E.state.histProjectActiveIndex = !rows.length ? -1 : index < 0 ? (ev.key === 'ArrowDown' ? 0 : rows.length - 1) : (index + (ev.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length; sync();
    } else if (ev.key === 'Enter') { ev.preventDefault(); if (rows.length) choose(rows[Math.max(0, E.state.histProjectActiveIndex || 0)].key); }
  });
})(typeof window !== 'undefined' ? window : globalThis);
