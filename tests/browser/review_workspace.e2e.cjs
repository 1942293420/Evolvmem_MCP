// Real HTTP, original-text rendering, preview/apply and stale-rule recovery.
// Run only against the disposable knowledge_fixture with efficiency fixtures.
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const browser = await chromium.launch({headless:true, args:['--no-sandbox']});
  const page = await browser.newPage({viewport:{width:1512,height:1000}});
  const errors=[], checks=[];
  page.on('pageerror', e=>errors.push(e.message));page.setDefaultTimeout(15000);
  const base=process.env.EVOLVMEM_TEST_URL || 'http://127.0.0.1:39512';
  const idle=()=>page.waitForFunction(()=>document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
  try {
    await page.goto(base+'/#knowledge/organization');await page.waitForSelector('[data-rg-list]');await idle();
    const card=page.locator('[data-rg-list] > *').filter({hasText:'审核条目'}).first();
    await card.locator('[data-rg="choose"]').click();
    const form=page.locator('[data-rg-form]');
    await page.waitForSelector('[data-rg-narrative] h4');
    assert.match(await page.locator('[data-rg-narrative]').innerText(),/审核条目00：编号处理必须保留原始编号/);
    assert.equal(await form.locator('[name="selection"]').inputValue(),'item');
    await form.locator('[name="projects"][value="evo"]').check();
    await form.locator('[data-rg="preview"]').click();await idle();await page.locator('[data-rg-preview] h3').waitFor();
    assert.match(await page.locator('[data-rg-preview]').innerText(),/归入「Evo 演示项目」：1 条/);
    await page.locator('[data-rg="refresh"]').click();await idle();
    assert.equal(await form.locator('[name="projects"][value="evo"]').isChecked(),true);
    await page.waitForTimeout(4600); // Cross the actual four-second poll.
    assert.equal(await page.locator('[data-rg-narrative]').isVisible(),true);
    assert.equal(await page.locator('#org-task-region').getAttribute('open'),null);
    checks.push('单条审核原地展示完整片段，不跳页；刷新与后台轮询保留选择和草稿');
    await form.locator('[name="selection"]').selectOption('group');
    await form.locator('[name="condition"]').fill('编号处理');
    await form.locator('[name="exceptions"]').fill('跨平台条件');
    await form.locator('[name="scope"]').selectOption('future');
    await page.locator('.signal-nav [data-knowledge-view="projects"]').click();await page.waitForSelector('.project-list');
    await page.locator('.signal-nav [data-knowledge-view="organization"]').click();await idle();
    assert.equal(await form.locator('[name="condition"]').inputValue(),'编号处理');
    await form.locator('[data-rg="preview"]').click();await idle();await page.locator('[data-rg-preview] h3').waitFor();
    assert.match(await page.locator('[data-rg-preview]').innerText(),/归入「Evo 演示项目」：10 条/);
    assert.match(await page.locator('[data-rg-preview]').innerText(),/本次不归入：2 条/);
    assert.equal(await page.locator('[data-rg-preview] li').count(),12);
    checks.push('批量预览显示目标项目、查找范围、符合与排除数量和每条资料');
    // Make these synthetic tasks stale, just as in the reported screenshot.
    await page.evaluate(async()=>{
      const get=await fetch('/api/knowledge/rules');const rules=await get.json();
      const response=await fetch('/api/knowledge/rules',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({expected_revision:rules.revision,instructions:rules.instructions+'\n浏览器验收：按最新规则核对。'})});
      if(!response.ok)throw Error('fixture rule update failed: '+response.status);
    });
    await form.locator('[data-rg="preview"]').click();await idle();await page.locator('[data-rg-preview] h3').waitFor();
    assert.equal(await form.locator('[data-rg="apply"]').isDisabled(),true);
    assert.match(await page.locator('[data-rg-save-status]').innerText(),/没有可保存的资料/);
    await page.screenshot({path:'/tmp/evo-review-stale.png',fullPage:true});
    await form.locator('[data-rg="reprocess"]').click();await idle();
    await page.waitForFunction(()=>document.querySelector('[data-rg-reprocess-status]')?.textContent.includes('重新整理完成'),{},{timeout:30000});
    assert.equal(await form.locator('[name="projects"][value="evo"]').isChecked(),true);
    assert.equal(await form.locator('[name="condition"]').inputValue(),'编号处理');
    await form.locator('[data-rg="preview"]').click();await idle();await page.locator('[data-rg-preview] h3').waitFor();
    assert.equal(await form.locator('[data-rg="apply"]').isEnabled(),true);
    checks.push('规则过期时给出原因和重整入口，真实重新整理后保留草稿并恢复可保存预览');
    for(const width of [1512,1024,768,390]){
      await page.setViewportSize({width,height:1000});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'overflow '+width);
      const b=await form.locator('[data-rg-preview]').boundingBox(),s=await form.locator('.review-apply-bar').boundingBox();
      assert.ok(s.y>=b.y+b.height-1,'save bar overlaps preview');
      await page.screenshot({path:`/tmp/evo-review-${width}.png`,fullPage:true});
    }
    await form.locator('[data-rg="apply"]').click();await idle();
    assert.match(await page.locator('[data-rg-status]').innerText(),/已归入「Evo 演示项目」10 条/);
    await page.reload();await page.waitForSelector('[data-rg-list]');await idle();
    const remaining=page.locator('[data-rg-list] > *').filter({hasText:'审核条目'}).first();
    assert.equal(await page.locator('[data-rg-list] > *').filter({hasText:'审核条目'}).count(),2);
    checks.push('保存真实生效且刷新后仅剩排除项；四种宽度无溢出或遮挡');
    assert.deepEqual(errors,[]);console.log(JSON.stringify({checks,errors},null,2));
  } catch(error) {console.error('URL',page.url());console.error('VISIBLE',await page.locator('[data-rg-list]').allTextContents());console.error('PAGE ERRORS',errors);await page.screenshot({path:'/tmp/evo-review-failure.png',fullPage:true});throw error;} finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
