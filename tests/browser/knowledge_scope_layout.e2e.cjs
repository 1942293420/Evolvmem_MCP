// Project counts, long labels and nested bounds: page scrollWidth alone misses clipped controls.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(15000);
 const base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39527';
 try{
  await page.goto(base+'/#knowledge/organization');await page.waitForSelector('[data-rg-list]');
  await page.evaluate(async()=>{for(let i=0;i<45;i++){
   const r=await fetch('/api/knowledge/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:'bounds-demo-'+i,display_name:i===44?'LongProjectNameWithoutWordSeparators'.repeat(3).slice(0,100):i%2?'项目适用范围布局演示 '+i:'document-layout-check-'+i})});if(!r.ok)throw Error('fixture project creation failed');
  }});
  await page.reload();await page.locator('[data-rg-list] > *').filter({hasText:'集中审核演示'}).locator('button').click();
  const form=page.locator('[data-rg-form]');await form.waitFor({state:'visible'});
  await form.locator('[name=selection]').selectOption('group');await form.locator('[name=condition]').fill('资料实际用于改进文档归类和审核流程，单纯提到项目名称不能作为依据。');
  const states=[];
  for(const zoom of [1,1.25,1.5])for(const width of [320,390,768,1024,1150,1151,1440,1920]){
   await page.setViewportSize({width,height:1000});await page.evaluate(z=>document.body.style.zoom=String(z),zoom);
   const state=await page.evaluate(()=>{
    const editor=document.querySelector('.review-editor'),box=editor.getBoundingClientRect();
    const selectors=['[data-rg-form]','[data-rg-scope]','.knowledge-scope-picker','[data-scope-projects]','.scope-options','input','select','textarea','[data-rg=preview]','[data-rg=apply]'];
    const outside=[...editor.querySelectorAll(selectors.join(','))].filter(e=>{const r=e.getBoundingClientRect();return r.width&&r.height&&(r.right>box.right+1||r.left<box.left-1||e.scrollWidth>e.clientWidth+2)&&!e.matches('input,textarea,select');}).map(e=>e.className||e.tagName);
    const controlsOutside=[...editor.querySelectorAll('input,select,textarea,button')].filter(e=>{const r=e.getBoundingClientRect();return r.width&&r.height&&(r.right>box.right+1||r.left<box.left-1);}).map(e=>e.name||e.dataset.rg);
    const list=editor.querySelector('.scope-options'),label=list.querySelector('label'),check=label.querySelector('input').getBoundingClientRect(),name=label.querySelector('span').getBoundingClientRect();
    return {pageOverflow:document.documentElement.scrollWidth>innerWidth+2,outside,controlsOutside,listOverflow:list.scrollWidth>list.clientWidth+2,inlineCheckbox:check.right<=name.left+1&&check.top<name.bottom&&check.bottom>name.top};
   });
   assert.deepEqual(state,{pageOverflow:false,outside:[],controlsOutside:[],listOverflow:false,inlineCheckbox:true},`scope layout width=${width}, zoom=${zoom}`);states.push({width,zoom});
  }
  await page.evaluate(()=>document.body.style.zoom='1');await page.setViewportSize({width:1440,height:1000});
  await form.locator('[data-scope-search]').fill('项目适用范围布局演示 1');await form.locator('[name=projects][value=bounds-demo-1]').check();await form.locator('[data-scope-search]').fill('document-layout-check-2');await form.locator('[name=projects][value=bounds-demo-2]').check();
  assert.equal(await form.locator('[name=projects]:checked').count(),2);
  await form.locator('[data-scope-search]').fill('');await form.locator('[name=knowledge_scope]').selectOption('global');await form.locator('[name=knowledge_scope]').selectOption('project');assert.equal(await form.locator('[name=projects]:checked').count(),2);
  await form.locator('[data-rg-scope]').scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/evo-scope-layout-desktop.png'});
  await page.setViewportSize({width:390,height:1000});await form.locator('[data-rg-scope]').scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/evo-scope-layout-mobile.png'});
  assert.deepEqual(errors,[]);console.log(JSON.stringify({checkedLayouts:states.length,syntheticProjects:45,checks:['表单和每个控件都在编辑区内','项目列表内部没有横向溢出，长名称可读','复选框与名称并排','搜索、多选、通用切换保留选择'],errors},null,2));
 }catch(e){await page.screenshot({path:'/tmp/evo-scope-layout-failed.png'});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
