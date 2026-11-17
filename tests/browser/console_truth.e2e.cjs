// Isolated HTTP frontend fixture. This script never points mutations at production.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http'), assert = require('node:assert/strict');
const assets = process.env.CONSOLE_V2_ROOT || path.resolve(__dirname, '../../evolvmem/web_static/v2');
const output = process.env.EVOLVMEM_SCREENSHOTS || '/tmp/console-truth-browser';
const me = { ok: true, current_user: { id: 'owner' }, libraries: [{ id: 'owner', display_name: '本人库', can_manage_history: true, can_manage_experience: true }], policy: { experience_gate_mode: 'review' } };
let mode = 'normal', posts = 0;
function api(url) {
  if (url.pathname === '/api/auth/me' || url.pathname === '/auth/ops/status') return { central_sso: true, authenticated: true, user_scope: 'fixture-owner', native_status: 'ready', name: '合成验收账号', csrf_token: 'fixture-csrf' };
  if (url.pathname.endsWith('/me')) return me;
  if (url.pathname.endsWith('/overview')) return { ok: true, metrics: { active_notes: 0, pending_live: 0, pending_backfill: 0 }, recent_events: [] };
  if (url.pathname.endsWith('/experiences')) return { ok: true, items: [], interception_funnel: { layers: {}, total_intercepted: 0, rescued_total: 0 } };
  if (url.pathname.endsWith('/review-inbox')) return { ok: true, items: [], track_agreement: { live: { evaluable_total: 0 }, backfill: { evaluable_total: 0 } }, shadow_mode: true };
  if (url.pathname.endsWith('/evolution-and-skills')) return { ok: true, skills_catalog: { rows: [{ id: 'real-skill', name: 'actual-catalog-entry', description: '真实目录说明', file_count: 3, source: 'fixture' }] } };
  if (url.pathname.endsWith('/pipeline')) return { ok: true, circuits: [], segments: [], llm_usage: [], backfill_cursors: [], backfill_job_stats: [], legacy_cleanups: [] };
  if (url.pathname.endsWith('/control')) return { ok: true, users: [], grants: [], model_reputation: [] };
  if (url.pathname.endsWith('/history')) return { ok: true, projects: [], items: [], blocks: [] };
  return { ok: false, error: 'not_found' };
}
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname.startsWith('/api/') || url.pathname === '/auth/ops/status') {
    if (req.method === 'POST') posts++;
    res.setHeader('Content-Type', 'application/json');
    if (mode === 'failed') { res.statusCode = 503; return res.end(JSON.stringify({ ok: false, error: 'fixture_unavailable' })); }
    return res.end(JSON.stringify(api(url)));
  }
  const rel = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/v2\/?/, '') || 'index.html';
  const file = path.resolve(assets, rel);
  if (!file.startsWith(path.resolve(assets) + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
  res.end(fs.readFileSync(file));
});
(async () => {
  fs.mkdirSync(output, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE, headless: true, args: ['--no-sandbox', '--no-proxy-server'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [], checks = [];
  page.on('pageerror', err => errors.push(err.message));
  try {
    await page.goto(base + '/v2/');
    await page.waitForFunction(() => EvolvMemV2.state.apiMode === 'live' && !!EvolvMemV2.state.liveMetrics);
    assert.match(await page.locator('#workspace-main').innerText(), /0 条/);
    await page.screenshot({ path: path.join(output, '01-overview.png'), fullPage: true });
    checks.push('real zero overview; static process explanation instead of fabricated telemetry');
    for (const route of ['review-inbox', 'experiences', 'history', 'evolution', 'skills', 'control']) {
      await page.locator(`[data-act="goto-tab"][data-route="${route}"]`).first().click();
      await page.waitForFunction(r => EvolvMemV2.state.route === r, route);
      await page.waitForTimeout(60);
      const text = await page.locator('#workspace-main').innerText();
      assert.doesNotMatch(text, /68\.5%|87\.5%|6\.2%|74\/74|2\/2|全量在线|在线健康/);
    }
    await page.locator('[data-act="goto-tab"][data-route="evolution"]').first().click();
    await page.locator('[data-act="evo-subtab"][data-sub="handbook"]').click();
    assert.equal(await page.getByRole('button', { name: '生成与发布手册 · 待开发', exact: true }).isDisabled(), true);
    await page.screenshot({ path: path.join(output, '02-handbook-planned.png'), fullPage: true });
    await page.locator('[data-act="goto-tab"][data-route="skills"]').first().click();
    await page.locator('[data-act="open-skill-detail"]').first().click();
    assert.equal(await page.getByRole('button', { name: '反事实评测 · 待开发', exact: true }).isDisabled(), true);
    assert.doesNotMatch(await page.locator('#workspace-main').innerText(), /已写入本地|Token 节省.*31|3 个达标/);
    await page.screenshot({ path: path.join(output, '03-skill-details.png'), fullPage: true });
    checks.push('all primary routes render; planned handbook and Skill actions disabled');
    await page.setViewportSize({ width: 390, height: 900 });
    await page.locator('[data-act="goto-tab"][data-route="overview"]').first().click();
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2), true);
    await page.screenshot({ path: path.join(output, '04-mobile.png'), fullPage: true });
    checks.push('390px overview without horizontal overflow');
    mode = 'failed'; await page.reload();
    await page.getByRole('heading', { name: /页面数据加载失败|暂时无法核对访问权限/ }).waitFor();
    assert.equal(await page.evaluate(() => EvolvMemV2.state.experiences.length), 0);
    assert.equal(await page.evaluate(() => EvolvMemV2.canWrite()), false);
    await page.screenshot({ path: path.join(output, '05-api-failure.png'), fullPage: true });
    mode = 'normal'; await page.getByRole('button', { name: /重新加载|重新核对/ }).click();
    await page.getByRole('heading', { name: '记忆与经验总览', exact: true }).waitFor();
    checks.push('HTTP failure shows error, never demo; retry recovers current page');
    assert.equal(posts, 0); assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'browser-results.json'), JSON.stringify({ checks, posts, errors }, null, 2));
    console.log(JSON.stringify({ checks, posts, errors }));
  } finally { await browser.close(); server.close(); }
})().catch(err => { console.error(err); server.close(); process.exitCode = 1; });
