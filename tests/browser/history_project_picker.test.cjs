const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const repoAssets = path.resolve(__dirname, '../../evolvmem/web_static/v2');
const assets = process.env.CONSOLE_V2_ROOT || (fs.existsSync(repoAssets) ? repoAssets : path.resolve(__dirname, '../candidate'));
function client(fetch) {
  const context = vm.createContext({ console, setTimeout, clearTimeout, fetch, location: { protocol: 'http:', hash: '' } });
  const html = fs.readFileSync(path.join(assets, 'index.html'), 'utf8');
  for (const [, file] of html.matchAll(/<script src="([^"]+)"/g)) {
    if (file.endsWith('sso-session.js')) continue;
    vm.runInContext(fs.readFileSync(path.join(assets, file), 'utf8'), context, { filename: file });
  }
  return context.EvolvMemV2;
}
const projects = [
  { key: 'linux-repair', title: 'linux 修复', blocks: 4 },
  { key: 'finance-daily', title: '资金日报自动化', blocks: 2 },
  { key: 'tk-dispatch', title: 'TK每日发货同步', blocks: 4 },
  { key: 'temu', title: 'Temu 日发货', blocks: 1 },
  { key: 'deleted', title: '资金自动旧项目', blocks: 6, deleted: true },
];
test('production history does not offer an unconnected Windows device switch', () => {
  const E = client(); E.state.apiMode = 'live';
  const html = E.views.history();
  assert.match(html, /设备库切换未接入/);
  assert.doesNotMatch(html, /<select[^>]*id="hist-db-sel"(?![^>]*disabled)/);
});
test('project finder supports Chinese fragments, separated words, case and full-width input', () => {
  const E = client();
  assert.equal(typeof E.filterHistoryProjects, 'function');
  for (const [q, key] of [['资金自动', 'finance-daily'], ['发货 TK', 'tk-dispatch'], ['ＴＥＭＵ', 'temu'], ['LINUX修复', 'linux-repair'], ['finance', 'finance-daily']]) {
    assert.equal(E.filterHistoryProjects(projects, q)[0]?.key, key, q);
  }
});
test('finder handles empty and unmatched queries without exposing trash or limiting the searched list', () => {
  const E = client();
  assert.equal(E.filterHistoryProjects(projects, '').length, 4);
  assert.equal(E.filterHistoryProjects(projects, '[没有这个项目]').length, 0);
  const many = Array.from({ length: 250 }, (_, i) => ({ key: 'p-' + i, title: '项目' + i, blocks: i }));
  many.push({ key: 'last-project', title: '最后的目标项目', blocks: 3 });
  assert.equal(E.filterHistoryProjects(many, '最后目标')[0].key, 'last-project');
});
test('project names are escaped and fuzzy search does not rename the project or change its count', () => {
  const E = client();
  E.state.historyProjects = [{ key: 'original-key', title: 'A<B" 项目', blocks: 7 }];
  E.state.selectedProjectKey = 'original-key'; E.state.histProjectOpen = true;
  E.state.histProjectQuery = 'ab';
  const html = E.renderHistoryProjectPicker();
  assert.match(html, /A&lt;B&quot; 项目/);
  assert.match(html, /7/);
  assert.match(html, /original-key/);
  assert.equal(E.state.historyProjects[0].title, 'A<B" 项目');
  assert.equal(E.state.selectedProjectKey, 'original-key');
});
test('history renders a dedicated project finder and retains separate conversation search', () => {
  const E = client(); E.state.apiMode = 'live'; E.state.histScope = 'projects';
  E.state.historyProjects = projects; E.state.selectedProjectKey = 'linux-repair';
  const html = E.views.history();
  assert.match(html, /hist-proj-search/);
  assert.match(html, /role="combobox"/);
  assert.match(html, /id="hist-q"/);
  assert.doesNotMatch(html, /id="hist-proj-sel"/);
});

test('folder picker searches full workspace paths and distinguishes identical leaf names', () => {
  const E = client();
  const folders = [
    { key: 'workspace-a', title: '日发货', workspacePath: 'C:\\业务\\TK\\日发货', blocks: 2 },
    { key: 'workspace-b', title: '日发货', workspacePath: 'D:\\业务\\TEMU\\日发货', blocks: 3 },
  ];
  assert.equal(E.filterHistoryProjects(folders, 'temu 日发货')[0]?.key, 'workspace-b');
  E.state.historyProjects = folders; E.state.histProjectOpen = true;
  const html = E.renderHistoryProjectPicker();
  for (const folder of folders) assert.ok(html.includes(folder.workspacePath));
  E.state.historyProjects = [{ key: 'unsafe', title: '目录', workspacePath: 'C:\\<目录>\\"名称"', blocks: 1 }];
  assert.match(E.renderHistoryProjectPicker(), /C:\\&lt;目录&gt;\\&quot;名称&quot;/);
});

test('workspace history requests use opaque keys, retain paths, and leave turns and trash on their existing contract', async () => {
  const requests = [];
  const E = client(async (url, opts) => {
    const parsed = new URL(url, 'http://fixture'); requests.push({ url: parsed, method: opts.method });
    const view = parsed.searchParams.get('view');
    const data = { ok: true };
    if (view === 'projects') data.projects = [{ project: 'workspace-opaque', display_name: '日发货', workspace_path: 'C:\\业务\\TK\\日发货', block_count: 2 }];
    if (view === 'project') data.blocks = [{ block_id: 'block-a', title: '真实对话', turn_count: 1 }];
    if (view === 'unassigned') data.blocks = [{ block_id: 'block-u', title: '目录不明', projects: [] }];
    if (view === 'search') data.items = parsed.searchParams.get('scope') === 'unassigned' ? [{ block_id: 'block-u', title: '目录不明', projects: [], workspace_path: null }] : [{ block_id: 'block-a', title: '真实对话', projects: ['workspace-opaque'], workspace_path: 'C:\\业务\\TK\\日发货' }];
    if (view === 'trash') data.items = [{ kind: 'project', key: 'legacy-project', title: '旧项目' }, { kind: 'workspace', key: 'workspace-deleted', title: '旧目录', workspace_path: 'D:\\旧目录' }];
    if (view === 'turns') data.messages = [];
    return { status: 200, json: async () => data };
  });
  E.state.apiMode = 'live'; E.state.ownerId = E.state.libraryCtx = 'owner'; E.canWrite = () => true;
  E.state.histScope = 'projects'; E.state.histQuery = '';
  await E.api.loadHistory();
  assert.equal(E.state.historyProjects[0].workspacePath, 'C:\\业务\\TK\\日发货');
  assert.equal(E.state.historyProjects[0].kind, 'workspace');
  assert.equal(E.state.historyBlocks[0].project, 'workspace-opaque');
  assert.equal(E.state.historyBlocks[0].workspacePath, 'C:\\业务\\TK\\日发货');
  assert.equal(E.state.historyProjects.find(p => p.key === 'legacy-project')?.kind, 'project');
  assert.equal(E.state.historyProjects.find(p => p.key === 'workspace-deleted')?.kind, 'workspace');
  E.state.histScope = 'unassigned'; await E.api.loadHistory();
  assert.equal(E.state.historyBlocks[0].project, null);
  assert.equal(E.state.historyBlocks[0].workspacePath, '');
  E.state.histScope = 'projects'; E.state.histQuery = '真实'; await E.api.loadHistory();
  assert.equal(E.state.historyBlocks[0].workspacePath, 'C:\\业务\\TK\\日发货');
  E.state.histScope = 'unassigned'; E.state.histQuery = '目录'; await E.api.loadHistory();
  assert.equal(requests.some(({ url }) => url.searchParams.get('view') === 'search' && url.searchParams.get('q') === '目录' && url.searchParams.get('scope') === 'unassigned'), true);
  assert.equal(E.state.historyBlocks[0].project, null); assert.equal(E.state.historyBlocks[0].workspacePath, '');
  for (const { url, method } of requests) {
    assert.equal(method, 'GET');
    const view = url.searchParams.get('view');
    assert.equal(url.searchParams.get('grouping'), ['projects', 'project', 'unassigned', 'search'].includes(view) ? 'workspace' : null, view);
    if (view === 'project') assert.equal(url.searchParams.get('project_id'), 'workspace-opaque');
  }
});

test('folder management keeps legacy project restores separate from workspace actions', async () => {
  const bodies = [];
  const E = client(async (_url, opts) => { bodies.push(JSON.parse(opts.body)); return { status: 200, json: async () => ({ ok: true }) }; });
  E.state.apiMode = 'live';
  E.state.historyProjects = [{ key: 'folder', title: '目录', kind: 'workspace', workspacePath: 'C:\\目录' }, { key: 'old', title: '旧项目', kind: 'project', deleted: true }];
  E.api.loadHistory = async () => {};
  await E.api.manageHistoryProject('delete', 'folder', '目录');
  await E.api.manageHistoryProject('restore', 'old', '旧项目');
  assert.equal(bodies[0].kind, 'workspace'); assert.equal(bodies[0].key, 'folder');
  assert.equal(bodies[1].kind, 'project'); assert.equal(bodies[1].key, 'old');
});

test('live history explains Codex folders and renders the selected transcript workspace path', () => {
  const E = client(); E.state.apiMode = 'live'; E.state.histScope = 'projects'; E.state.histQuery = '';
  E.state.historyProjects = [{ key: 'folder', title: '日发货', workspacePath: 'C:\\业务\\TK\\日发货', blocks: 1, kind: 'workspace' }];
  E.state.historyBlocks = [{ id: 'block', project: 'folder', workspacePath: 'C:\\业务\\TK\\日发货', title: '对话', transcript: [], turns: 0 }];
  E.state.selectedBlockId = 'block'; E.state.selectedProjectKey = 'folder';
  const html = E.views.history();
  assert.match(html, /Codex 实际工作文件夹/);
  assert.match(html, /无可靠工作文件夹.*未归属/);
  assert.ok(html.includes('C:\\业务\\TK\\日发货'));
  let detail; E.openModal=(_title,body)=>{detail=body;}; E.applyAction('history-source-info',{id:'block'}); assert.match(detail,/工作文件夹/); assert.ok(detail.includes('C:\\业务\\TK\\日发货'));
});
