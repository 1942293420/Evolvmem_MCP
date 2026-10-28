// Real Chromium checks for the reading-first frontend refresh.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const task = process.argv.includes('--task') ? process.argv[process.argv.indexOf('--task') + 1] : 'all';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  const remote = spawn('ssh', ['-L', '39480:127.0.0.1:39479', 'codex-linux',
    'cd /home/jiangli/.local/share/evolvmem-worktrees/frontend-refresh && PYTHONPATH=.tools/site exec python3 tests/browser/capability_archives_fixture.py'],
    {stdio:['ignore','pipe','pipe']});
  let browser;
  const errors = [];
  try {
    let ready = false;
    for (let i=0;i<50&&!ready;i++) {
      try { ready = (await fetch('http://127.0.0.1:39480/api/capabilities')).ok; } catch { await sleep(200); }
    }
    assert.ok(ready, 'disposable web fixture did not start');
    browser = await chromium.launch({headless:true,args:['--no-sandbox']});
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror', error => errors.push(error.message));
    page.setDefaultTimeout(15000);
    const base = 'http://127.0.0.1:39480';

    if (task === 'navigation' || task === 'all') {
      await page.goto(base + '/#knowledge/capabilities');
      await page.waitForSelector('.refresh-topnav');
      const labels = await page.locator('[data-page-link]').allTextContents();
      for (const label of ['总览','开发能力','项目存档','任务断点','自动整理']) {
        assert.ok(labels.some(text => text.includes(label)), `missing navigation: ${label}`);
      }
      assert.match(await page.locator('[data-more-toggle]').innerText(), /更多/);
      await page.locator('[data-more-toggle]').click();
      assert.ok(await page.locator('[data-more-menu]').isVisible());
      await page.locator('[data-more-menu] a').first().click();
      assert.ok(!(await page.locator('[data-more-menu]').isVisible()));
      await page.goto(base + '/#knowledge/projects');
      assert.ok(await page.locator('.refresh-topnav').count());
      assert.equal(await page.locator('.refresh-duplicate-title').count(), 0);
      for (const width of [390,768,1024,1440]) {
        await page.setViewportSize({width,height:1000});
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2));
      }
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({task, errors}));
  } finally {
    if (browser) await browser.close();
    remote.kill('SIGTERM');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
