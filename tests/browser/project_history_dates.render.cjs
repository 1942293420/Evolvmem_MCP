// Pure rendering regression; actual HTTP/Chromium check is project_history_dates.e2e.cjs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const result = {innerHTML: ''}, form = {};
const mount = {innerHTML: '', querySelector: s => s === 'form' ? form : result, querySelectorAll: () => []};
const archive = {project:'ai_purchase',display_name:'AI采购',summary:'## 项目概述\n日期测试',started_at:null,development_days:null,revision:1,
  events:[{event_id:1,title:'无日期',content:'旧内容',source_ref:'context_item:1',event_type:'milestone',occurred_at:null,occurred_at_precision:'unknown',recorded_at:'2026-10-28 12:00:00'},
    {event_id:2,title:'七月记录',content:'日期依据',source_ref:'context_item:2',event_type:'milestone',occurred_at:'2026-07-20',occurred_at_precision:'date',recorded_at:'2026-10-28 12:00:00'}]};
const sandbox = {window:{}, EvolvAuth:{fetch:async()=>({ok:true,json:async()=>archive})}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../evolvmem/web_static/project-archives.js'),'utf8'),sandbox);
(async()=>{
  await sandbox.window.EvolvProjectArchives.render({mount,esc:String});
  await form.onsubmit({preventDefault(){},target:{elements:{project:{value:'AI采购'}}}});
  assert.match(result.innerHTML,/发生时间未确认/);
  assert.match(result.innerHTML,/入库时间：2026-10-28/);
  assert.match(result.innerHTML,/仅到日，时区未确认/);
  assert.match(result.innerHTML,/最早有日期记录 未确认/);
  assert.doesNotMatch(result.innerHTML,/开发周期 null|开始于|发生时间：2026-10-28/);
  console.log('project history date rendering passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
