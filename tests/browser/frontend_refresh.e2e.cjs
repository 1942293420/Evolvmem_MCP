const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const task = process.argv.includes('--task') ? process.argv[process.argv.indexOf('--task') + 1] : 'all';

(async () => {
  const remote = spawn('ssh', ['-L','39480:127.0.0.1:39479','codex-linux','sleep 30'],
    {stdio:['ignore','pipe','pipe']});
  let browser;
  const errors = [];
  try {
    let ready = false;
    for (let i = 0; i < 50 && !ready; i++) {
      try { ready = (await fetch('http://127.0.0.1:39480/api/capabilities')).ok; }
      catch { await sleep(200); }
    }
    assert.ok(ready, 'disposable web fixture did not start');
    browser = await chromium.launch({headless:true,args:['--no-sandbox']});
    const page = await browser.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:39480/#knowledge/capabilities');
    await page.waitForTimeout(1500);
    const base = 'http://127.0.0.1:39480';

    if (task === 'navigation' || task === 'all') {
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

    if (task === 'capabilities' || task === 'all') {
      await page.goto(base + '/#knowledge/capabilities');
      await page.waitForTimeout(500);
      await page.locator('[data-memory-query]').fill('Vue表单');
      await page.locator('[data-memory-search]').click();
      await page.waitForTimeout(1500);
      assert.equal(await page.locator('[data-memory-card]').count(), 1);
      await page.locator('[data-memory-card]').click();
      assert.ok(await page.locator('[data-memory-source-detail]').count());

      await page.locator('[data-memory-query]').fill('');
      await page.locator('[data-memory-search]').click();
      await page.waitForTimeout(1500);
      assert.equal(await page.locator('[data-memory-card]').count(), 20);
      await page.locator('[data-page="next"]').click();
      await page.waitForTimeout(500);
      assert.match(await page.locator('[data-page-status]').innerText(), /第 2 \/ 2 页/);

      await page.locator('[data-memory-source]').selectOption('integration');
      await page.waitForTimeout(1000);
      const cards = await page.locator('[data-memory-card]').allTextContents();
      assert.ok(cards.length > 0 && cards.every(text => text.includes('integration')));

      await page.locator('[data-tab="intake"]').click();
      await page.locator('[data-assign="1"]').click();
      await page.waitForTimeout(700);
      assert.match(await page.locator('.refresh-notice').innerText(), /正式能力记忆/);
      await page.locator('[data-tab="suggestions"]').click();
      await page.locator('[data-suggestion="accept"]').click();
      await page.waitForTimeout(700);
      assert.match(await page.locator('.refresh-notice').innerText(), /创建新分类/);
      for (const width of [390,768,1024,1440]) {
        await page.setViewportSize({width,height:1000});
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2));
      }
    }
    if (task === 'archives' || task === 'all') {
      await page.goto(base + '/#knowledge/archives');
      await page.locator('[name=project]').fill('蓝鲸');
      await page.locator('#project-archive-form button').click();
      await page.waitForTimeout(1500);
      console.log('ARCHIVE_BODY', (await page.locator('body').innerText()).slice(0,1500));
      assert.equal(await page.locator('pre.project-archive-summary').count(), 0);
      assert.ok(await page.locator('[data-project-event]').count() > 0);
      await page.locator('[data-project-event]').first().click();
      assert.ok(await page.locator('[data-event-source]').count());
      await page.locator('[name=project]').fill('不存在的项目');
      await page.locator('#project-archive-form button').click();
      await page.waitForTimeout(1000);
      assert.match(await page.locator('#project-archive-result').innerText(), /project_not_registered|未找到/);
      assert.equal(await page.locator('[name=project]').inputValue(), '不存在的项目');
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
