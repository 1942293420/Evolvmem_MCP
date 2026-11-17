// Local synthetic HTTP fixture; no production authentication or writes.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assets=path.resolve(__dirname,'../../evolvmem/web_static/v2'),queries=[],errors=[];
const note={id:'semantic',category:'业务经验',kind:'procedure',status:'active',origin:'manual',problem:'数据库连接等候过长',solution:'检查连接池',reusable_experience:'核实来源后采用',scope:{project:'Evolvmem'},evidence:[],updated_at:'2026-11-17T01:00:00Z'};
const server=http.createServer((req,res)=>{
 const u=new URL(req.url,'http://127.0.0.1');
 if(u.pathname.startsWith('/api/') || u.pathname==='/auth/ops/status'){
  assert.equal(req.method,'GET');let data={ok:true};
  if(u.pathname==='/api/auth/me' || u.pathname==='/auth/ops/status')data={central_sso:true,authenticated:true,user_scope:'search-fixture',native_status:'ready',name:'Synthetic reader',csrf_token:'synthetic'};
  else if(u.pathname.endsWith('/me'))data={ok:true,current_user:{id:'owner'},libraries:[{id:'owner',can_manage_experience:true}]};
  else if(u.pathname.endsWith('/overview'))data={ok:true,metrics:{}};
  else if(u.pathname.endsWith('/experiences')){
   const q=u.searchParams.get('q') || '';queries.push(q);
   data={ok:true,total:q?1:0,items:q?[note]:[],category_counts:{'业务经验':1},diagnostics:{mode:'hybrid'}};
   if(q==='old'){res.statusCode=503;res.setHeader('Content-Type','application/json');return setTimeout(()=>res.end(JSON.stringify({error:'late_error'})),1100);}
  }
  res.setHeader('Content-Type','application/json');return res.end(JSON.stringify(data));
 }
 const file=path.resolve(assets,u.pathname==='/v2/'?'index.html':u.pathname.replace(/^\/v2\//,''));
 if(!file.startsWith(assets+path.sep)||!fs.existsSync(file)){res.statusCode=404;return res.end();}
 res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(file));
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox','--no-proxy-server']});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror',e=>errors.push(String(e)));
  await page.goto('http://127.0.0.1:'+server.address().port+'/v2/#experiences');
  await page.waitForSelector('#exp-f-q');
  await page.locator('#exp-f-q').pressSequentially('database connection wait',{delay:12});
  await page.waitForSelector('.list-entry-title');
  assert.equal(queries.filter(q=>q==='database connection wait').length,1);
  assert.ok((await page.locator('.list-entry-title').textContent()).includes(note.problem));
  assert.equal(await page.locator('#exp-f-q').inputValue(),'database connection wait');
  await page.locator('#exp-f-q').fill('old');
  await page.waitForFunction(()=>window.EvolvMemV2.state.expLoading);
  await page.waitForTimeout(350);
  await page.locator('#exp-f-q').fill('new');
  await page.waitForFunction(()=>window.EvolvMemV2.state.expFilters.q==='new'&&!window.EvolvMemV2.state.expLoading);
  await page.waitForTimeout(1000);
  assert.equal(await page.evaluate(()=>window.EvolvMemV2.session.status),'ready');
  assert.equal(await page.locator('#exp-f-q').inputValue(),'new');
  assert.ok(await page.locator('.list-entry-title').isVisible());
  fs.mkdirSync('/tmp/experience-search-browser',{recursive:true});
  await page.screenshot({path:'/tmp/experience-search-browser/desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:900});
  await page.screenshot({path:'/tmp/experience-search-browser/mobile.png',fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),true);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,queries,checks:['debounced real GET','semantic result survives rendering','focus/query retained','late 503 cannot block newer query','SSO remains ready','desktop and mobile screenshots','no page errors']}));
 }finally{await browser.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
