const { test } = require('node:test');
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assets = process.env.CONSOLE_V2_ROOT || path.resolve(__dirname, '../../evolvmem/web_static/v2');
function client(fetch) {
  const context = vm.createContext({ console, setTimeout, clearTimeout, fetch, location: { protocol: 'http:', hash: '' } });
  for (const [, file] of fs.readFileSync(path.join(assets, 'index.html'), 'utf8').matchAll(/<script src="([^"]+)"/g)) {
    if (!file.endsWith('sso-session.js')) vm.runInContext(fs.readFileSync(path.join(assets, file), 'utf8'), context, { filename: file });
  }
  const E = context.EvolvMemV2; E.state.apiMode = 'live'; E.state.ownerId = E.state.libraryCtx = 'owner'; E.canWrite = () => false;
  return E;
}
test('history times convert explicit offsets to Beijing independently of host timezone', () => {
  const E = client(); assert.equal(typeof E.formatHistoryTime, 'function');
  for (const value of ['2026-11-17T01:02:03Z', '2026-11-17T09:02:03+08:00', '2026-11-16T20:02:03-05:00', '2026-11-17 09:02:03+0800']) assert.equal(E.formatHistoryTime(value), '2026-11-17 09:02', value);
  assert.equal(E.formatHistoryTime('2026-11-17T16:00:00Z'), '2026-11-18 00:00');
  assert.equal(E.formatHistoryTime('2024-02-29T23:59:59.123Z'), '2024-03-01 07:59');
});
test('legacy timestamps retain their original clock with an unspecified timezone label', () => {
  const E = client();
  assert.equal(E.formatHistoryTime('2026-11-17 09:02:03'), '2026-11-17 09:02:03（来源时区未注明）');
  assert.equal(E.formatHistoryTime('2026-11-17T09:02'), '2026-11-17 09:02（来源时区未注明）');
  for (const value of [null, undefined, '', 0, {}, 'invalid', '2026-02-29T01:02:03Z', '2026-04-31T01:02:03Z', '2026-11-17T24:00:00Z', '2026-11-17T09:02:03+25:00', '2026-02-29 09:02:03']) assert.equal(E.formatHistoryTime(value), '时间未知', String(value));
});
test('folder, unassigned and search blocks retain times and distinguish missing counts from real zero', async () => {
  const raw = { block_id: 'block', title: 'Synthetic record', observed_first_at: '2026-11-17T01:00:00Z', observed_last_at: '2026-11-17T02:00:00Z', turn_count: 0, message_count: 0, time_basis: 'archive_fallback', projects: ['folder'] };
  let omit = false;
  const E = client(async url => {
    const view = new URL(url, 'http://fixture').searchParams.get('view');
    let data = { ok: true };
    if (view === 'projects') data.projects = [{ project: 'folder', display_name: 'Synthetic folder', block_count: 1 }];
    if (view === 'project' || view === 'unassigned') data.blocks = [omit ? { block_id: 'block', title: 'Synthetic unknown' } : raw];
    if (view === 'search') data.items = [omit ? { block_id: 'block', title: 'Synthetic unknown' } : raw];
    if (view === 'turns') data.messages = [];
    return { status: 200, json: async () => data };
  });
  for (const [scope, query] of [['projects', ''], ['unassigned', ''], ['projects', 'query'], ['unassigned', 'query']]) {
    E.state.histScope = scope; E.state.histQuery = query;
    for (const missing of [false, true]) {
      omit = missing; await E.api.loadHistory(); const b = E.state.historyBlocks[0];
      assert.equal(b.firstAt, missing ? null : raw.observed_first_at); assert.equal(b.lastAt, missing ? null : raw.observed_last_at);
      assert.equal(b.turns, missing ? null : 0); assert.equal(b.messageCount, missing ? null : 0);
      assert.equal(b.timeBasis, missing ? null : 'archive_fallback');
    }
  }
});
test('partial message times align with transcript strings and late reads cannot attach times to a newer view', async () => {
  let finish;
  const E = client(async () => new Promise(resolve => { finish = resolve; }));
  E.state.historyBlocks = [{ id: 'block', transcript: [] }];
  const loading = E.api.loadBlockTurns('block');
  finish({ status: 200, json: async () => ({ ok: true, time_basis: 'archive_fallback', messages: [{ role: 'user', content: 'a', recorded_at: '2026-11-17T00:00:00Z', time_basis: 'recorded' }, { role: 'assistant', content: 'b' }, { role: 'user', content: 'c', recorded_at: '2026-11-17T01:00:00Z' }] }) });
  await loading;
  assert.deepEqual(Array.from(E.state.historyBlocks[0].transcript), ['用户：a', '助手：b', '用户：c']);
  assert.deepEqual(Array.from(E.state.historyBlocks[0].transcriptTimes), ['2026-11-17T00:00:00Z', null, '2026-11-17T01:00:00Z']);
  assert.equal(E.state.historyBlocks[0].timeBasis, 'archive_fallback');
  assert.deepEqual(Array.from(E.state.historyBlocks[0].transcriptTimeBases), ['recorded', 'archive_fallback', 'archive_fallback']);
  const late = E.api.loadBlockTurns('block'); E.state.libraryEpoch = (E.state.libraryEpoch || 0) + 1;
  E.state.historyBlocks = [{ id: 'block', transcript: ['新视图'], transcriptTimes: ['2026-11-18T01:00:00Z'] }];
  finish({ status: 200, json: async () => ({ ok: true, messages: [{ role: 'user', content: 'old', recorded_at: '2000-01-01T00:00:00Z' }] }) });
  await late;
  assert.equal(E.state.historyBlocks[0].transcriptTimes[0], '2026-11-18T01:00:00Z');
});
test('history lists, details and individual bubbles show neutral record times and escape untrusted input', () => {
  const E = client(); E.state.histScope = 'unassigned'; E.state.histQuery = '';
  E.state.historyBlocks = [{ id: 'block', project: null, title: 'Synthetic record', firstAt: '2026-11-17T00:00:00Z', lastAt: '2026-11-17T01:00:00Z', turns: 0, messageCount: 3, transcript: ['用户：a', '助手：b', '用户：c'], transcriptTimes: ['2026-11-17T00:00:00Z', null, '<img src=x onerror=alert(1)>'] }];
  E.state.selectedBlockId = 'block';
  let html = E.views.history();
  assert.match(html, /最近记录：2026-11-17 09:00/); let source; E.openModal=(_title, body)=>{source=body;}; E.applyAction('history-source-info',{id:'block'}); assert.match(source,/记录开始<.*2026-11-17 08:00/s);
  assert.match(html, /北京时间.*UTC\+8/); assert.match(html, /0 轮对话/); assert.match(html, /3 条消息/);
  assert.match(html, /chat-time[^>]*>记录时间：2026-11-17 08:00/); assert.equal((html.match(/chat-time[^>]*>记录时间：时间未知/g) || []).length, 2);
  assert.doesNotMatch(html, /<img|onerror=/);
  E.state.historyBlocks[0].turns = null; E.state.historyBlocks[0].lastAt = null;
  html = E.views.history(); assert.match(html, /轮次未记录/); assert.match(html, /最近记录：时间未知/);
  E.state.historyBlocks[0].timeBasis = 'archive_fallback';
  html = E.views.history(); assert.match(html, /入库时间：时间未知/); E.applyAction('history-source-info',{id:'block'}); assert.match(source,/归档入库时间<.*2026-11-17 08:00/s); assert.doesNotMatch(html, /首次入库时间/);
  assert.match(source, /原对话时间未保留/); assert.match(html, /chat-time[^>]*>入库时间：2026-11-17 08:00/);
});
