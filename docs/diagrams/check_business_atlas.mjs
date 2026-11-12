import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import assert from 'node:assert/strict';

const root=path.dirname(fileURLToPath(import.meta.url));
const profile=process.argv[2];
if(!profile) throw new Error('Pass the temporary Chrome user-data directory');
const [port,wsPath]=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').trim().split('\n');
const ws=new WebSocket(`ws://127.0.0.1:${port}${wsPath}`);
await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject});
let seq=0;const waiting=new Map(),errors=[],requests=[];
ws.onmessage=ev=>{const m=JSON.parse(ev.data);if(m.id){const p=waiting.get(m.id);if(!p)return;waiting.delete(m.id);clearTimeout(p.timeout);m.error?p.reject(new Error(JSON.stringify(m.error))):p.resolve(m.result)}
  if(m.method==='Runtime.exceptionThrown')errors.push(m.params.exceptionDetails);
  if(m.method==='Network.requestWillBeSent')requests.push(m.params.request.url);
};
function call(method,params={},sessionId){return new Promise((resolve,reject)=>{const id=++seq,timeout=setTimeout(()=>{waiting.delete(id);reject(new Error('Timeout: '+method))},15000);waiting.set(id,{resolve,reject,timeout});ws.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}))})}
const {targetId}=await call('Target.createTarget',{url:'about:blank'});
const {sessionId}=await call('Target.attachToTarget',{targetId,flatten:true});
const send=(m,p={})=>call(m,p,sessionId);
async function evaluate(expression){const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result.value}
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const previews=path.join(root,'previews');fs.mkdirSync(previews,{recursive:true});
const downloads=fs.mkdtempSync('/tmp/evo-atlas-svg-');
await call('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:downloads});
await send('Page.enable');await send('Runtime.enable');await send('Network.enable');
const viewport=async(width,height)=>send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
async function shot(name){const r=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(previews,name+'.png'),Buffer.from(r.data,'base64'))}
const report={charts:[],interactions:[],errors,externalRequests:[]};
try {
  await viewport(1600,1120);
  await send('Page.addScriptToEvaluateOnNewDocument',{source:"try{localStorage.removeItem('ev-business-atlas')}catch{}"});
  await send('Page.navigate',{url:pathToFileURL(path.join(root,'evolvmem-business-atlas.html')).href});
  for(let i=0;i<40;i++){if(await evaluate('Boolean(window.__atlas)'))break;await wait(100)}
  assert.equal(await evaluate('window.__atlas.charts.length'),8);
  await evaluate('localStorage.clear();document.documentElement.classList.remove("dark");document.getElementById("theme").textContent="深色模式"');
  await evaluate('document.fonts.ready.then(()=>true)');
  const ids=await evaluate('window.__atlas.charts.map(c=>c.id)');
  for(const id of ids){
    await evaluate(`document.querySelector('[data-chart="${id}"]').click();document.getElementById('close').click();document.getElementById('fit').click()`);
    await wait(100);await evaluate("document.getElementById('toast').textContent=''");
    const checks=await evaluate(`(()=>{
      const svg=document.querySelector('#canvas svg'),view=svg.viewBox.baseVal,c=window.__atlas.charts.find(c=>c.id===window.__atlas.getActive()),outside=[],textOverflow=[],labelOverlaps=[],edgeOverlaps=[];
      for(const t of svg.querySelectorAll('text')){const b=t.getBBox();if(b.x<0||b.y<0||b.x+b.width>view.width+1||b.y+b.height>view.height+1)outside.push(t.textContent)}
      for(const g of svg.querySelectorAll('.node')){const t=g.querySelector('.node-label'),shape=g.querySelector('.shape');if(t&&shape){const b=t.getBBox(),a=shape.getBBox();if(b.width>a.width-8||b.height>a.height-8)textOverflow.push(g.dataset.node)}}
      const nodes=c.nodes.filter(n=>!['actor','start','end'].includes(n.shape));
      for(const t of svg.querySelectorAll('.edge-label')){const b=t.getBBox();for(const n of nodes){if(b.x<n.x+n.w-5&&b.x+b.width>n.x+5&&b.y<n.y+n.h-5&&b.y+b.height>n.y+5)labelOverlaps.push([t.textContent,n.id])}}
      for(const e of c.edges)for(let i=1;i<e.points.length;i++){const [x1,y1]=e.points[i-1],[x2,y2]=e.points[i];for(const n of nodes){if(n.id===e.source||n.id===e.target)continue;const x=n.x+5,y=n.y+5,w=n.w-10,h=n.h-10;if(x1===x2&&x1>x&&x1<x+w&&Math.max(y1,y2)>y&&Math.min(y1,y2)<y+h||y1===y2&&y1>y&&y1<y+h&&Math.max(x1,x2)>x&&Math.min(x1,x2)<x+w)edgeOverlaps.push([e.id,n.id])}}
      return {id:c.id,nodeCount:c.nodes.length,outside,textOverflow,labelOverlaps,edgeOverlaps,bodyOverflow:document.documentElement.scrollWidth>innerWidth+2,svgVisible:svg.getBoundingClientRect().width>100};
    })()`);
    report.charts.push(checks);
    await shot(id);
    // Every graph node opens its own readable detail panel.
    const nodes=await evaluate('window.__atlas.charts.find(c=>c.id===window.__atlas.getActive()).nodes.map(n=>n.id)');
    for(const n of nodes){assert.equal(await evaluate(`(()=>{document.querySelector('[data-node="${n}"]').dispatchEvent(new MouseEvent('click',{bubbles:true}));return !document.getElementById('details').hidden&&document.getElementById('detail-title').textContent.length>0})()`),true)}
    await evaluate("document.getElementById('close').click();document.getElementById('export').click()");
    const svgFile=path.join(downloads,'evolvmem-'+id+'.svg');
    for(let i=0;i<30&&!fs.existsSync(svgFile);i++)await wait(50);
    assert.ok(fs.existsSync(svgFile),'export '+id);
    const exported=fs.readFileSync(svgFile,'utf8');
    assert.equal(await evaluate(`new DOMParser().parseFromString(${JSON.stringify(exported)},'image/svg+xml').querySelector('parsererror')===null`),true,'SVG syntax '+id);
  }
  report.interactions.push('8 张图的导航、全部节点详情及各图 SVG 导出通过');
  await evaluate("window.__atlas.select('intake');document.getElementById('close').click();document.getElementById('fit').click()");
  const baseZoom=await evaluate('window.__atlas.getState().zoom');
  await evaluate("document.getElementById('plus').click()");assert.ok(await evaluate('window.__atlas.getState().zoom')>baseZoom);
  await evaluate("document.getElementById('minus').click()");assert.ok(Math.abs(await evaluate('window.__atlas.getState().zoom')-baseZoom)<.001);
  await evaluate("document.getElementById('plus').click();window.__atlas.select('trial');window.__atlas.select('intake')");assert.ok(await evaluate('window.__atlas.getState().zoom')>baseZoom);
  const routeCount=await evaluate("document.getElementById('routes').options.length");
  for(let r=0;r<routeCount;r++){
    await evaluate(`(()=>{const el=document.getElementById('routes');el.value='${r}';el.dispatchEvent(new Event('change'));document.getElementById('next').click()})()`);
    assert.equal(await evaluate('window.__atlas.getState().step'),0);
    await evaluate("document.getElementById('next').click();document.getElementById('prev').click()");assert.equal(await evaluate('window.__atlas.getState().step'),0);
  }
  await evaluate("document.getElementById('play').click()");await wait(1500);assert.ok(await evaluate('window.__atlas.getState().step')>=1);await evaluate("document.getElementById('play').click()");
  report.interactions.push('缩放、适合窗口、切图保留缩放、三条入库路径、前后步进与播放暂停通过');
  await evaluate("window.__atlas.select('continuity');document.getElementById('next').click()");
  for(let i=0;i<7;i++){assert.equal(await evaluate("document.querySelector('.edge.current')?.dataset.edge"),'e'+i);if(i<6)await evaluate("document.getElementById('next').click()")}
  report.interactions.push('时序图按 7 条消息的时间顺序播放通过');
  await evaluate("window.__atlas.select('intake');document.getElementById('close').click();document.getElementById('theme').click();document.getElementById('fit').click()");
  assert.equal(await evaluate("document.documentElement.classList.contains('dark')"),true);await shot('intake-dark');
  await evaluate("document.getElementById('theme').click()");
  await send('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
  await evaluate("document.getElementById('routes').value='0';document.getElementById('routes').dispatchEvent(new Event('change'));document.getElementById('play').click()");assert.equal(await evaluate('window.__atlas.getState().step'),0);
  await wait(1600);assert.equal(await evaluate('window.__atlas.getState().step'),0);
  await send('Emulation.setEmulatedMedia',{features:[]});
  report.interactions.push('深浅色模式及减少动画设置通过');
  await evaluate("document.getElementById('toast').textContent=''");
  for(const size of [[1280,900],[390,1000]]){
    await viewport(...size);await evaluate("document.getElementById('close').click();document.getElementById('fit').click()");await wait(80);
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth+2'),true,'page width '+size[0]);
    assert.equal(await evaluate("document.getElementById('viewport').scrollWidth>=document.getElementById('viewport').clientWidth"),true);
    await shot('intake-'+size[0]);
  }
  report.interactions.push('1280 桌面及 390 窄屏无页面横向溢出，图纸可独立滚动查看');
  report.externalRequests=requests.filter(url=>/^https?:/.test(url));
  assert.deepEqual(report.externalRequests,[]);assert.deepEqual(errors,[]);
  assert.ok(report.charts.every(c=>c.svgVisible&&!c.bodyOverflow&&!c.outside.length&&!c.textOverflow.length&&!c.labelOverlaps.length&&!c.edgeOverlaps.length),'visible text bounds and unobstructed routes');
  fs.writeFileSync(path.join(root,'visual-checks.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report,null,2));
} finally {
  fs.writeFileSync(path.join(root,'visual-checks.json'),JSON.stringify(report,null,2));
  await call('Target.closeTarget',{targetId});ws.close();
}
