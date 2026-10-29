// Synthetic browser race check: late library reads must not replace a newer view.
// Start a disposable fixture and set EVOLVMEM_TEST_URL; no production data/writes.
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({
    headless:true,
    ...(process.env.PLAYWRIGHT_CHANNEL ? {channel:process.env.PLAYWRIGHT_CHANNEL} : {}),
    ...(process.env.CHROMIUM_EXECUTABLE ? {executablePath:process.env.CHROMIUM_EXECUTABLE} : {}),
    args:['--no-sandbox'],
  });
  const checks = [];
  try {
    const page = await browser.newPage();
    await page.goto((process.env.EVOLVMEM_TEST_URL || 'http://127.0.0.1:39480') + '/#knowledge/capabilities');
    await page.waitForSelector('[data-memory-results]');
    for (const kind of ['global-qa', 'project-memory']) {
      const result = await page.evaluate(async kind => {
        const originalFetch = EvolvAuth.fetch;
        let release, signalStarted;
        const started = new Promise(resolve => {signalStarted=resolve;});
        const held = new Promise(resolve => {release=resolve;});
        const response = value => ({ok:true,json:async()=>value});
        const project = kind === 'global-qa' ? '__global__' : 'race-fixture';
        const mount = document.createElement('div');
        document.body.append(mount);
        try {
          EvolvAuth.fetch = async path => {
            if (path === '/api/knowledge/projects') return response({
              projects: kind === 'global-qa' ? [] : [{project,status:'active',display_name:'Synthetic race',aliases:[],total:0,pending:0}],
              pending:0,
            });
            if (path.startsWith('/api/knowledge/items?')) return response({items:[],total:0,page_size:20});
            if (path === '/api/knowledge/qa?project=__global__&state=active' || path.startsWith('/api/knowledge/project-memory?')) {
              signalStarted(); await held;
              return response({project,total:0,items:[],name:'Late synthetic result',summary:'late',body:'',source_ids:[],sessions:[],qa_count:0});
            }
            if (path.startsWith('/api/capabilities?')) return response({
              items:[],categories:[],intake:[],suggestions:[],source_projects:[],counts:{},total:0,page:1,pages:1,
            });
            return originalFetch(path);
          };
          const controller = EvolvKnowledge.create(mount,{navigate(){}});
          controller.project = project;
          const originalRead = controller.load();
          await started;
          controller.view = 'capabilities';
          await controller.load();
          const panel = mount.querySelector('.capability-workspace');
          release();
          await originalRead;
          return {kind,panelExisted:!!panel,samePanel:mount.querySelector('.capability-workspace')===panel};
        } finally {
          release(); EvolvAuth.fetch=originalFetch; mount.remove();
        }
      },kind);
      assert.equal(result.panelExisted,true,kind+': new capabilities view must render');
      assert.equal(result.samePanel,true,kind+': old library read overwrote newer capabilities view');
      checks.push(result);
    }
    console.log(JSON.stringify({checks},null,2));
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error);process.exitCode=1;});
