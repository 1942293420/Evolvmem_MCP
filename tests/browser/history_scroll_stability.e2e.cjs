// Isolated synthetic HTTP regression. Never use production identities or conversations.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assets = process.env.CONSOLE_V2_ROOT || path.resolve(__dirname, '../../evolvmem/web_static/v2');
const out = process.env.EVOLVMEM_SCREENSHOTS || '/tmp/history-scroll-stability-browser';
let authenticated = true, statusReads = 0;
const posts = [], errors = [], checks = [];
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname.startsWith('/api/') || url.pathname === '/auth/ops/status') {
    if (req.method !== 'GET') posts.push(req.method + ' ' + url.pathname);
    let data = { ok: true };
    if (url.pathname === '/api/auth/me' || url.pathname === '/auth/ops/status') {
      statusReads++; res.statusCode = authenticated ? 200 : 401;
      data = { central_sso: true, authenticated, user_scope: 'scroll-fixture', native_status: 'ready', name: 'Synthetic reader', csrf_token: 'synthetic-csrf' };
    } else if (url.pathname.endsWith('/me')) data = { ok: true, current_user: { id: 'owner' }, libraries: [{ id: 'owner', display_name: 'Synthetic only', can_manage_history: true }] };
    else if (url.pathname.endsWith('/history')) {
      const view = url.searchParams.get('view');
      if (view === 'projects') data.projects = [{ project: 'workspace-fixture', display_name: 'Synthetic folder', workspace_path: '/synthetic/folder', block_count: 120 }];
      if (view === 'trash') data.items = [];
      if (view === 'project') data.blocks = Array.from({ length: 120 }, (_, i) => ({ block_id: 'block-' + i, title: 'Synthetic conversation ' + i, turn_count: 120 }));
      if (view === 'turns') data.messages = Array.from({ length: 120 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', content: 'SYNTHETIC MESSAGE ' + i + ' ' + 'Isolated scrolling fixture text. '.repeat(35) }));
    }
    res.setHeader('Content-Type', 'application/json'); return res.end(JSON.stringify(data));
  }
  const rel = url.pathname === '/v2/' ? 'index.html' : url.pathname.replace(/^\/v2\//, '');
  const file = path.resolve(assets, rel);
  if (!file.startsWith(path.resolve(assets) + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});
(async () => {
  fs.mkdirSync(out, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE, headless: true, args: ['--no-sandbox', '--no-proxy-server'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on('pageerror', e => errors.push(e.message));
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/v2/#history`);
    await page.waitForFunction(() => EvolvMemV2.session.dataReady && EvolvMemV2.state.historyBlocks[0]?._turnsLoaded);
    await page.evaluate(() => {
      window.__scrollEvents = [];
      window.__scrollPosition = () => ({ y: scrollY, maxY: document.documentElement.scrollHeight - innerHeight, active: document.activeElement?.id, listY: document.querySelector('.list-scroll')?.scrollTop, t: performance.now() });
      const render = EvolvMemV2.renderAll;
      EvolvMemV2.renderAll = function () { __scrollEvents.push({ event: 'render-before', position: __scrollPosition(), stack: new Error().stack }); const r = render.apply(this, arguments); __scrollEvents.push({ event: 'render-after', position: __scrollPosition() }); return r; };
      const focus = HTMLElement.prototype.focus;
      HTMLElement.prototype.focus = function (opts) { __scrollEvents.push({ event: 'focus', id: this.id, opts, position: __scrollPosition(), stack: new Error().stack }); return focus.call(this, opts); };
    });
    // Clicking ordinary transcript text naturally focuses its tabindex=-1 main ancestor.
    await page.locator('.chat-bubble').last().click();
    assert.equal(await page.evaluate(() => document.activeElement.id), 'workspace-main');
    await page.mouse.move(1100, 450); await page.mouse.wheel(0, -10000000);
    await page.waitForFunction(() => scrollY < 2);
    await page.waitForTimeout(150);
    await page.evaluate(() => { document.querySelector('.list-scroll').scrollTop = 600; });
    const before = await page.evaluate(() => __scrollPosition());
    assert.equal(before.listY, 600);
    const readsBefore = statusReads;
    await page.screenshot({ path: path.join(out, '01-reading-at-top.png') });
    // Cross one genuine 5000ms SSO polling boundary without invoking the check ourselves.
    await page.waitForTimeout(5200);
    const after = await page.evaluate(() => __scrollPosition());
    checks.push({ name: 'real content click and wheel-to-top stay at top across stable SSO poll', passed: statusReads > readsBefore && after.y < 2, before, after, statusReadsBefore: readsBefore, statusReadsAfter: statusReads });
    checks.push({ name: 'stable SSO poll preserves nested conversation list scroll', passed: after.listY === before.listY, before: before.listY, after: after.listY });
    await page.screenshot({ path: path.join(out, '02-after-stable-poll.png') });
    // A data-driven redraw must preserve reading position even when an off-screen input retains focus.
    await page.locator('#hist-q').click();
    await page.evaluate(() => window.scrollTo({ top: 6000, behavior: 'instant' }));
    await page.waitForFunction(() => Math.abs(scrollY - 6000) < 2);
    const manual = await page.evaluate(() => { const before = __scrollPosition(); EvolvMemV2.renderAll(); return { before, after: __scrollPosition() }; });
    checks.push({ name: 'manual redraw restores input focus without moving the reading viewport', passed: Math.abs(manual.after.y - manual.before.y) < 2 && manual.after.active === 'hist-q', ...manual });
    await page.screenshot({ path: path.join(out, '03-after-manual-redraw.png') });
    authenticated = false;
    await page.evaluate(() => EvolvMemV2.session.check());
    await page.waitForFunction(() => EvolvMemV2.session.status === 'login' && EvolvMemV2.state.historyBlocks.length === 0);
    checks.push({ name: 'revoked authentication clears private history immediately', passed: await page.locator('.chat-bubble').count() === 0 && await page.locator('.session-panel').count() === 1 });
    const events = await page.evaluate(() => __scrollEvents);
    fs.writeFileSync(path.join(out, 'browser-results.json'), JSON.stringify({ checks, errors, posts, events }, null, 2));
    console.log(JSON.stringify({ checks, errors, posts }));
    assert.deepEqual(errors, []); assert.deepEqual(posts, []);
    assert.equal(checks.every(check => check.passed), true, checks.filter(check => !check.passed).map(check => check.name).join('; '));
  } catch (e) { await page.screenshot({ path: path.join(out, 'failure.png') }); throw e; }
  finally { await browser.close(); server.close(); }
})().catch(e => { console.error(e); server.close(); process.exitCode = 1; });
