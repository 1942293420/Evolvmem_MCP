const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const repoAssets = path.resolve(__dirname, '../../evolvmem/web_static/v2');
const assets = process.env.CONSOLE_V2_ROOT || (fs.existsSync(repoAssets) ? repoAssets : path.resolve(__dirname, '../candidate'));
function client() {
  const context = vm.createContext({ console, setTimeout, clearTimeout, location: { protocol: 'http:', hash: '' } });
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
