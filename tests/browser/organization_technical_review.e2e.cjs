// Real organization component rendering with synthetic read-only data; no service/model.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assets = process.env.EVOLVMEM_WEB_STATIC || path.resolve(__dirname, '../../evolvmem/web_static');
const output = process.env.EVOLVMEM_SCREENSHOT_DIR || os.tmpdir();
(async () => {
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_EXECUTABLE,
    headless: true, args: ['--no-sandbox']});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.setContent('<!doctype html><html lang="zh-CN"><body><main id="content"></main></body></html>');
    for (const style of ['designs/common.css', 'designs/signal.css', 'knowledge.css']) {
      await page.addStyleTag({path: path.join(assets, style)});
    }
    // Unrelated workspaces are not under test; the actual task cards, status
    // labels and alert renderer below come from the production component.
    await page.evaluate(() => {
      const idle = async () => {};
      window.EvolvAuth = {fetch: async () => ({json: async () => ({configured: false})})};
      window.EvolvKnowledgeFacts = {create: () => ({load: idle})};
      window.EvolvKnowledgeEquivalence = {records: () => ({load: idle})};
      window.EvolvReviewGroups = {render: async () => ({refresh: idle, poll: idle, resume: idle})};
      window.EvolvUI = {busy: () => () => {}};
    });
    await page.addScriptTag({path: path.join(assets, 'organization.js')});
    await page.evaluate(async () => {
      const codes = ['capability_response_invalid', 'module_response_invalid'];
      const items = codes.map((error_code, index) => ({id: index + 1,
        source_title: index ? '业务模块提取演示' : '知识提取演示',
        source_key: 'conversation:synthetic-' + index, status: 'review', stage: 'done',
        unit_count: 1, review_count: 1, attempts: 1, error_code, error_detail: ''}));
      const queue = {items, total: 2, current_task_ids: [1, 2], counts: {review: 2}};
      const api = async (route, payload) => {
        if (payload) throw Error('Unexpected write in read-only render test');
        if (route === 'organization/guidance') return {items: []};
        if (route === 'organization/settings') return {auto_new: false};
        if (route === 'organization' || route.startsWith('organization?')) return queue;
        throw Error('Unexpected route: ' + route);
      };
      const esc = text => String(text ?? '').replace(/[&<>"']/g, char =>
        ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
      await window.EvolvOrganization.render({mount: document.querySelector('#content'), api, esc,
        toast: () => {}, projects: [], onDirty: () => {}, onBusy: () => {}, initialTab: 'all'});
    });
    const messages = [
      '知识提取暂未完成，原文与已有有效知识已保留，等待重新核对。',
      '业务模块提取暂未完成，原文与已有有效归属已保留，等待重新核对。',
    ];
    for (let index = 0; index < messages.length; index++) {
      const card = page.locator(`[data-org-task="${index + 1}"]`);
      assert.equal(await card.locator('[role="alert"]').innerText(), messages[index]);
      assert.equal(await card.locator('.badge').innerText(), '待确认');
      assert.doesNotMatch(await card.innerText(), /(?:capability|module)_response_invalid|低价值|验证成功/);
    }
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2));
    assert.deepEqual(errors, []);
    fs.mkdirSync(output, {recursive: true});
    await page.screenshot({path: path.join(output, 'organization-technical-review-desktop.png'), fullPage: true});
    console.log(JSON.stringify({checks: ['知识技术失败中文提示保留原文与有效知识', '业务模块技术失败中文提示保留有效归属'], errors}));
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exit(1);});
