// A real final-page deletion must leave the reader on a usable page.
const assert=require('node:assert/strict');const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});const page=await browser.newPage(),base=process.env.EVOLVMEM_TEST_URL,errors=[];page.on('pageerror',e=>errors.push(e.message));
 const idle=()=>page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
 try{
  const r=await page.request.get(base+'/api/knowledge/items');const size=(await r.json()).page_size;
  for(let i=0;i<=size;i++){const r=await page.request.post(base+'/api/knowledge/items',{data:{title:'末页验收 '+i,body:'保存后页面应显示当前有效资料，并更新分页。',project:'evo',action:'draft'}});assert.ok(r.ok());}
  await page.goto(base+'/#knowledge/library?project=__all__');await idle();await page.locator('#search').fill('末页验收');await page.locator('[data-action=search]').click();await idle();
  await page.locator('[data-action=next]').click();await idle();assert.equal(await page.locator('#content tbody tr').count(),1);
  await page.locator('#content [data-action=detail]').click();page.once('dialog',d=>d.accept());await page.locator('#dialog [data-action=item-delete]').click();await idle();
  assert.equal(await page.locator('#dialog').evaluate(d=>d.open),false);
  assert.equal(await page.locator('#content tbody tr').count(),size,'after deleting the final row, the previous page must be visible');
  assert.match(await page.locator('.pagination').innerText(),/第 1 \/ 1 页/);assert.equal(await page.locator('#search').inputValue(),'末页验收');assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks:['删除末页唯一资料后关闭弹窗','自动回到有效页，数量和搜索条件保持一致'],errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
