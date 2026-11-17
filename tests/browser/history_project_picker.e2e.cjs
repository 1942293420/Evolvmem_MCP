// Synthetic HTTP fixture only. It never connects to a production API.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict'), http = require('node:http'), fs = require('node:fs'), path = require('node:path');
const assets = process.env.CONSOLE_V2_ROOT || path.resolve(__dirname, '../../evolvmem/web_static/v2');
const out = process.env.EVOLVMEM_SCREENSHOTS || '/tmp/history-project-picker-browser';
const projects = [{ project: 'linux-repair', display_name: 'linux 修复', block_count: 4 }, { project: 'finance-daily', display_name: '资金日报自动化', block_count: 2 }, { project: 'tk-daily', display_name: 'TK每日发货同步', block_count: 4 }, ...Array.from({ length: 250 }, (_, i) => ({ project: 'other-' + i, display_name: '普通项目 ' + i, block_count: 1 })), { project: 'last-target', display_name: '最后的目标项目', block_count: 3 }];
const historyRequests = [], posts = [];
let delayedProject = '';
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname.startsWith('/api/') || url.pathname === '/auth/ops/status') {
    if (req.method === 'POST') posts.push(url.pathname);
    let data = { ok: true };
    if (url.pathname === '/api/auth/me' || url.pathname === '/auth/ops/status') data = { central_sso: true, authenticated: true, user_scope: 'picker-owner', native_status: 'ready', name: '隔离验收', csrf_token: 'test-csrf' };
    else if (url.pathname.endsWith('/me')) data = { ok: true, current_user: { id: 'owner' }, libraries: [{ id: 'owner', display_name: '本人库', can_manage_history: true, can_manage_experience: true }, { id: 'peer', display_name: '他人只读库', can_manage_history: false, can_manage_experience: false }] };
    else if (url.pathname.endsWith('/overview')) data = { ok: true, metrics: { active_notes: 0, pending_live: 0, pending_backfill: 0 }, recent_events: [] };
    else if (url.pathname.endsWith('/history')) {
      historyRequests.push(url.search);
      const view = url.searchParams.get('view');
      if (view === 'projects') data.projects = url.searchParams.get('owner_id') === 'peer' ? [{ project: 'peer-project', display_name: '他人的项目', block_count: 1 }] : projects;
      if (view === 'trash') data.items = [];
      if (view === 'project') data.blocks = [{ block_id: 'block-' + url.searchParams.get('project_id'), title: '项目会话 ' + url.searchParams.get('project_id'), turn_count: 2 }];
      if (view === 'turns') data.messages = [{ role: 'user', content: '会话属于 ' + url.searchParams.get('block_id') }];
    }
    res.setHeader('Content-Type', 'application/json');
    if (url.pathname.endsWith('/history') && url.searchParams.get('view') === 'project' && url.searchParams.get('project_id') === delayedProject) return setTimeout(() => res.end(JSON.stringify(data)), 400);
    return res.end(JSON.stringify(data));
  }
  const rel = url.pathname === '/' || url.pathname === '/v2/' ? 'index.html' : url.pathname.replace(/^\/v2\//, '');
  const file = path.resolve(assets, rel);
  if (!file.startsWith(path.resolve(assets) + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});
(async () => {
  fs.mkdirSync(out, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE, headless: true, args: ['--no-sandbox', '--no-proxy-server'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [], checks = []; page.on('pageerror', e => errors.push(e.message));
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/v2/#history`);
    await page.waitForFunction(() => EvolvMemV2.session.dataReady && EvolvMemV2.state.historyBlocks.length > 0);
    assert.equal(await page.locator('#hist-db-sel').count(), 0);
    assert.match(await page.locator('#workspace-main').innerText(), /设备库切换未接入/);
    const before = historyRequests.length;
    await page.locator('#hist-proj-toggle').click();
    await page.locator('#hist-proj-search').fill('资金自动');
    assert.equal(await page.locator('#hist-proj-options [role="option"]').count(), 1);
    assert.equal(await page.evaluate(() => EvolvMemV2.state.histQuery), '');
    assert.equal(historyRequests.length, before);
    await page.screenshot({ path: path.join(out, '01-fuzzy-projects.png'), fullPage: true });
    await page.locator('[data-project-picker-key="finance-daily"]').click();
    await page.waitForFunction(() => EvolvMemV2.state.historyBlocks[0]?.project === 'finance-daily');
    assert.match(await page.locator('#hist-proj-toggle').innerText(), /资金日报自动化 \(2\)/);
    assert.equal(historyRequests.some(q => new URLSearchParams(q).get('project_id') === 'finance-daily'), true);
    checks.push('Chinese fragment filter is local; selection uses original key and count');
    await page.locator('#hist-proj-toggle').click(); await page.locator('#hist-proj-search').fill('完全不存在的项目');
    assert.equal(await page.locator('#hist-proj-options [role="option"]').count(), 0);
    assert.match(await page.locator('#hist-proj-options').innerText(), /没有找到项目/);
    await page.locator('#hist-proj-search').press('Escape');
    assert.equal(await page.locator('#hist-proj-popover').isHidden(), true);
    assert.equal(await page.evaluate(() => EvolvMemV2.state.selectedProjectKey), 'finance-daily');
    await page.locator('#hist-proj-toggle').click(); await page.locator('#hist-proj-clear').click();
    assert.equal(await page.locator('#hist-proj-options [role="option"]').count(), projects.length);
    await page.locator('#hist-proj-search').press('ArrowUp');
    assert.equal(await page.locator('#hist-proj-search').getAttribute('aria-activedescendant'), 'hist-proj-option-' + (projects.length - 1));
    await page.locator('#hist-proj-search').fill('资金');
    await page.locator('#hist-proj-search').press('Tab');
    assert.equal(await page.locator('#hist-proj-clear').evaluate(button => document.activeElement === button), true);
    await page.locator('#hist-proj-clear').press('Enter');
    assert.equal(await page.locator('#hist-proj-search').inputValue(), '');
    assert.equal(await page.evaluate(() => EvolvMemV2.state.selectedProjectKey), 'finance-daily');
    await page.locator('#hist-proj-search').fill('最后目标'); await page.locator('#hist-proj-search').press('ArrowDown'); await page.locator('#hist-proj-search').press('Enter');
    await page.waitForFunction(() => EvolvMemV2.state.historyBlocks[0]?.project === 'last-target');
    checks.push('no-match preserves selection; clear restores every project; keyboard selects beyond first 200');
    await page.locator('#hist-proj-toggle').click(); await page.locator('#hist-proj-search').fill('tk');
    await page.locator('#hist-proj-search').evaluate(input => input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true })));
    assert.equal(await page.evaluate(() => EvolvMemV2.state.selectedProjectKey), 'last-target');
    await page.locator('#hist-proj-search').press('ArrowDown'); await page.locator('#hist-proj-search').press('Enter');
    await page.waitForFunction(() => EvolvMemV2.state.historyBlocks[0]?.project === 'tk-daily');
    checks.push('case-insensitive match and IME Enter cannot select prematurely');
    delayedProject = 'finance-daily';
    await page.locator('#hist-proj-toggle').click(); await page.locator('#hist-proj-search').fill('资金');
    await page.locator('[data-project-picker-key="finance-daily"]').click();
    await page.locator('#hist-proj-toggle').click(); await page.locator('#hist-proj-search').fill('继续输入');
    await page.waitForFunction(() => EvolvMemV2.state.historyBlocks[0]?.project === 'finance-daily');
    assert.equal(await page.locator('#hist-proj-search').evaluate(input => document.activeElement === input), true);
    assert.equal(await page.locator('#hist-proj-search').inputValue(), '继续输入');
    await page.locator('#hist-proj-search').fill('tk'); await page.locator('#hist-proj-search').press('Enter');
    await page.waitForFunction(() => EvolvMemV2.state.historyBlocks[0]?.project === 'tk-daily');
    await page.locator('#hist-proj-toggle').click(); await page.locator('#hist-proj-search').fill('资金'); await page.locator('#hist-proj-search').press('Enter');
    await page.locator('#hist-proj-toggle').click(); await page.locator('#hist-proj-search').fill('tk'); await page.locator('#hist-proj-search').press('Enter');
    await page.waitForFunction(() => EvolvMemV2.state.historyBlocks[0]?.project === 'tk-daily');
    await page.waitForTimeout(450);
    assert.equal(await page.evaluate(() => EvolvMemV2.state.historyBlocks[0].project), 'tk-daily');
    checks.push('slow project reads preserve typing focus and cannot overwrite a newer selection');
    await page.locator('#hist-proj-toggle').click(); await page.locator('#hist-proj-search').fill('资金');
    await page.selectOption('#ctx-lib-sel', 'peer');
    await page.waitForFunction(() => EvolvMemV2.session.dataReady && EvolvMemV2.state.libraryCtx === 'peer' && EvolvMemV2.state.historyProjects[0]?.key === 'peer-project');
    assert.equal(await page.locator('#ctx-lib-sel option').count(), 2);
    assert.equal(await page.evaluate(() => EvolvMemV2.state.histProjectQuery), '');
    await page.locator('#hist-proj-toggle').click();
    assert.equal(await page.locator('#hist-proj-options [role="option"]').count(), 1);
    assert.doesNotMatch(await page.locator('#hist-proj-options').innerText(), /资金|linux/);
    checks.push('library switch clears project query and excludes previous library options');
    await page.setViewportSize({ width: 390, height: 900 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2), true);
    await page.screenshot({ path: path.join(out, '02-mobile.png'), fullPage: true });
    await page.locator('#hist-proj-search').press('Escape');
    assert.deepEqual(errors, []); assert.deepEqual(posts, []);
    fs.writeFileSync(path.join(out, 'browser-results.json'), JSON.stringify({ checks, errors, posts }, null, 2));
    console.log(JSON.stringify({ checks, errors, posts }));
  } catch (e) { await page.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }); throw e; }
  finally { await browser.close(); server.close(); }
})().catch(e => { console.error(e); server.close(); process.exitCode = 1; });
