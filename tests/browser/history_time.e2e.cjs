// Isolated HTTP fixture: source times are synthetic and no production authentication is used.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assets = process.env.CONSOLE_V2_ROOT || path.resolve(__dirname, '../../evolvmem/web_static/v2');
const out = process.env.EVOLVMEM_SCREENSHOTS || '/tmp/history-time-browser';
const blocks = [
  { block_id: 'recorded', title: '原始记录时间示例', turn_count: 21, message_count: 147, observed_first_at: '2026-11-17T01:00:00Z', observed_last_at: '2026-11-17T02:00:00Z', time_basis: 'recorded' },
  { block_id: 'archive', title: '仅保留入库时间示例', turn_count: 2, message_count: 2, observed_first_at: '2026-11-16T16:00:00+00:00', observed_last_at: '2026-11-16T16:01:00+00:00', time_basis: 'archive_fallback' },
  { block_id: 'legacy', title: '未注明时区与时间缺失示例', message_count: 0, observed_first_at: '2026-11-17 12:34:56.123456', observed_last_at: null },
];
const posts = [], errors = [], checks = [];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname.startsWith('/api/') || url.pathname === '/auth/ops/status') {
    if (req.method !== 'GET') posts.push(req.method + ' ' + url.pathname);
    let data = { ok: true };
    if (url.pathname === '/api/auth/me' || url.pathname === '/auth/ops/status') data = { central_sso: true, authenticated: true, user_scope: 'time-fixture', native_status: 'ready', name: 'Synthetic reader', csrf_token: 'synthetic-csrf' };
    else if (url.pathname.endsWith('/me')) data = { ok: true, current_user: { id: 'owner' }, libraries: [{ id: 'owner', display_name: 'Synthetic only', can_manage_history: true }] };
    else if (url.pathname.endsWith('/history')) {
      const view = url.searchParams.get('view');
      if (view === 'projects') data.projects = [{ project: 'workspace-fixture', display_name: '时间验收', workspace_path: '/synthetic/folder', block_count: blocks.length }];
      if (view === 'trash') data.items = [];
      if (view === 'project' || view === 'unassigned') data.blocks = blocks;
      if (view === 'search') data.items = blocks.map(b => ({ ...b, projects: ['workspace-fixture'], workspace_path: '/synthetic/folder' }));
      if (view === 'turns') {
        const id = url.searchParams.get('block_id');
        data.time_basis = blocks.find(b => b.block_id === id)?.time_basis;
        data.messages = id === 'recorded' ? [{ role: 'user', content: '原文消息一', recorded_at: '2026-11-17T01:00:00Z' }, { role: 'assistant', content: '原文消息二', recorded_at: '2026-11-17T09:05:00+08:00' }, { role: 'user', content: '缺少时间的消息' }] : id === 'archive' ? [{ role: 'user', content: '仅有归档时间的消息', recorded_at: '2026-11-16T16:00:00+00:00' }, { role: 'assistant', content: '没有时间的归档消息' }] : [{ role: 'user', content: 'legacy完整原时间', recorded_at: '2026-11-17 12:34:56.123456' }, { role: 'assistant', content: '无效时间不得当HTML执行', recorded_at: '<img src=x onerror=alert(1)>' }];
        if (id === 'archive') { res.setHeader('Content-Type', 'application/json'); return setTimeout(() => res.end(JSON.stringify(data)), 250); }
      }
    }
    res.setHeader('Content-Type', 'application/json'); return res.end(JSON.stringify(data));
  }
  const file = path.resolve(assets, url.pathname === '/v2/' ? 'index.html' : url.pathname.replace(/^\/v2\//, ''));
  if (!file.startsWith(path.resolve(assets) + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'); res.end(fs.readFileSync(file));
});
(async () => {
  fs.mkdirSync(out, { recursive: true }); await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE, headless: true, args: ['--no-sandbox', '--no-proxy-server'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, timezoneId: 'America/New_York' }); page.on('pageerror', e => errors.push(e.message));
  const select = async id => { await page.locator(`[data-act="select-block"][data-id="${id}"]`).click(); await page.waitForFunction(id => EvolvMemV2.state.selectedBlockId === id && EvolvMemV2.state.historyBlocks.find(b => b.id === id)?._turnsLoaded, id); };
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/v2/#history`);
    await page.waitForFunction(() => EvolvMemV2.session.dataReady && EvolvMemV2.state.historyBlocks[0]?._turnsLoaded);
    assert.match(await page.locator('.list-entry[data-id="recorded"]').innerText(), /21 轮对话.*最近记录：2026-11-17 10:00/s);
    assert.match(await page.locator('.detail-panel').innerText(), /记录开始：2026-11-17 09:00.*147 条消息/s);
    assert.deepEqual(await page.locator('.chat-time').allTextContents(), ['记录时间：2026-11-17 09:00', '记录时间：2026-11-17 09:05', '记录时间：时间未知']);
    await page.screenshot({ path: path.join(out, '01-recorded-desktop.png'), fullPage: true }); checks.push('neutral list/detail/message times convert to Beijing despite browser timezone; real counts and missing times are retained');
    await select('archive'); assert.match(await page.locator('.detail-panel').innerText(), /归档入库时间：2026-11-17 00:01.*原对话时间未保留/s); assert.doesNotMatch(await page.locator('.detail-panel').innerText(), /首次入库时间/);
    assert.deepEqual(await page.locator('.chat-time').allTextContents(), ['入库时间：2026-11-17 00:00', '入库时间：时间未知']);
    await page.setViewportSize({ width: 390, height: 900 }); assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2), true);
    await page.evaluate(() => scrollTo({ top: 0, behavior: 'instant' }));
    await page.screenshot({ path: path.join(out, '02-archive-mobile.png'), fullPage: true }); checks.push('archive fallback is explicitly labeled and fits mobile');
    await select('legacy'); assert.match(await page.locator('.list-entry[data-id="legacy"]').innerText(), /轮次未记录.*最近记录：时间未知/s);
    assert.match(await page.locator('.detail-panel').innerText(), /2026-11-17 12:34:56\.123456（来源时区未注明）.*0 条消息/s);
    assert.equal(await page.locator('.chat-bubble img').count(), 0); assert.match((await page.locator('.chat-time').allTextContents())[1], /时间未知/);
    await page.evaluate(() => scrollTo({ top: 0, behavior: 'instant' }));
    await page.screenshot({ path: path.join(out, '03-legacy-mobile.png'), fullPage: true });
    await page.locator('.list-entry[data-id="archive"]').click(); await page.locator('.list-entry[data-id="recorded"]').click(); await page.waitForTimeout(350);
    assert.equal(await page.evaluate(() => EvolvMemV2.state.selectedBlockId), 'recorded');
    assert.match((await page.locator('.chat-time').allTextContents())[0], /记录时间：2026-11-17 09:00/);
    checks.push('legacy timezone remains unqualified, invalid HTML time is unknown, and delayed mouse switches do not mix message times');
    assert.deepEqual(errors, []); assert.deepEqual(posts, []); fs.writeFileSync(path.join(out, 'browser-results.json'), JSON.stringify({ checks, errors, posts }, null, 2)); console.log(JSON.stringify({ checks, errors, posts }));
  } catch (e) { await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }); throw e; }
  finally { await browser.close(); server.close(); }
})().catch(e => { console.error(e); server.close(); process.exitCode = 1; });
