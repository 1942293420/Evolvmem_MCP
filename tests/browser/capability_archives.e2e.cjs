// Real Chromium against a disposable remote SQLite workbench.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {
  const remote = spawn('ssh', ['-L', '39480:127.0.0.1:39479', 'codex-linux',
    'cd /home/jiangli/.local/share/evolvmem-worktrees/knowledge-restructure && PYTHONPATH=.tools/site exec python3 tests/browser/capability_archives_fixture.py'],
    {stdio: ['ignore', 'pipe', 'pipe']});
  remote.stdout.on('data', data => console.log(String(data).trim()));
  remote.stderr.on('data', data => console.error(String(data).trim()));
  let browser;
  const errors = [];
  try {
    let ready = false;
    for (let i = 0; i < 50 && !ready; i++) {
      try { ready = (await fetch('http://127.0.0.1:39480/api/capabilities')).ok; }
      catch (error) { if (i === 49) console.error(error); await sleep(200); }
    }
    assert.ok(ready, 'disposable web fixture did not start');
    browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    page.on('pageerror', error => errors.push(error.message));
    page.setDefaultTimeout(15000);

    await page.goto('http://127.0.0.1:39480/#knowledge/capabilities');
    await page.waitForSelector('[data-capability-item]');
    assert.equal(await page.locator('.flow-tags span').count(), 7);
    assert.match(await page.locator('[data-capability-item]').innerText(), /bluewhale/);
    assert.equal(await page.locator('[data-capability-intake]').count(), 1);
    assert.equal(await page.locator('[data-category-suggestion]').count(), 1);

    await page.locator('[data-suggestion-action="accept"]').click();
    await page.waitForFunction(() => document.querySelectorAll('.flow-tags span').length === 8);
    await page.locator('[data-capability-intake] select').selectOption('frontend');
    await page.locator('[data-capability-assign]').click();
    await page.waitForFunction(() => document.querySelectorAll('[data-capability-item]').length === 2);
    assert.equal(await page.locator('[data-capability-intake]').count(), 0);

    await page.goto('http://127.0.0.1:39480/#knowledge/archives');
    await page.locator('[name=project]').fill('蓝鲸');
    await page.locator('#project-archive-form button').click();
    await page.waitForSelector('[data-project-archive]');
    const archive = await page.locator('[data-project-archive]').innerText();
    assert.match(archive, /新增权限管理/);
    assert.doesNotMatch(archive, /Vue表单校验/);
    assert.equal(await page.locator('[data-project-event]').count(), 1);

    await page.goto('http://127.0.0.1:39480/#principles');
    await page.locator('[data-principle="restructure"]').click();
    await page.waitForFunction(() => document.body.innerText.includes('开发记录分成能力记忆与项目存档'));
    for (const width of [390, 768, 1440]) {
      await page.setViewportSize({width, height: 1000});
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2));
      await page.screenshot({path: `evo-capability-${width}.png`, fullPage: true});
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({checks: ['7类与来源项目', '建议确认后建类', '暂存确认后入正式记忆', '项目摘要与事件隔离', '双分流原理图', '三种宽度无溢出'], errors}));
  } finally {
    if (browser) await browser.close();
    remote.kill('SIGTERM');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
