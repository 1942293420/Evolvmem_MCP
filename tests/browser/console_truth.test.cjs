const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const assets = process.env.CONSOLE_V2_ROOT || (fs.existsSync(path.resolve(__dirname, '../../evolvmem/web_static/v2')) ? path.resolve(__dirname, '../../evolvmem/web_static/v2') : path.resolve(__dirname, '../candidate'));
function client(fetch, protocol = 'http:') {
  const context = vm.createContext({ console, setTimeout, clearTimeout,
    location: { protocol, hash: '', pathname: '/v2/' }, fetch });
  const html = fs.readFileSync(path.join(assets, 'index.html'), 'utf8');
  for (const [, file] of html.matchAll(/<script src="([^"]+)"/g)) {
    if (file.endsWith('sso-session.js')) continue; // Session transport has its own suite and the full-browser fixture.
    vm.runInContext(fs.readFileSync(path.join(assets, file), 'utf8'), context, { filename: file });
  }
  return context.EvolvMemV2;
}
const response = (status, data) => ({ status, json: async () => data });
const me = { ok: true, current_user: { id: 'owner' }, libraries: [], policy: { experience_gate_mode: 'review' } };

test('HTTP boot does not expose prototype records or grant writes before authentication', () => {
  const E = client();
  assert.equal(E.state.apiMode, 'loading');
  assert.equal(E.state.experiences.length, 0);
  assert.equal(E.state.skills.length, 0);
  assert.equal(E.state.proposals.length, 0);
  assert.equal(E.canWrite(), false);
  assert.match(E.renderMain(), /正在连接/);
});

test('offline preview is visibly identified as a demonstration', () => {
  const E = client(undefined, 'file:');
  assert.match(E.renderMain(), /离线演示/);
});

for (const [name, fetch] of [
  ['HTTP 503', async () => response(503, { ok: false, error: 'pg_control_unavailable' })],
  ['network error', async () => { throw Error('offline'); }],
  ['invalid JSON', async () => ({ status: 200, json: async () => { throw Error('bad JSON'); } })],
]) test(`bootstrap ${name} displays an error and never falls back to demonstration data`, async () => {
  const E = client(fetch);
  await E.api.bootstrap();
  assert.notEqual(E.state.apiMode, 'sandbox');
  assert.equal(E.state.experiences.length, 0);
  assert.equal(E.canWrite(), false);
  assert.match(E.renderMain(), /加载失败/);
  assert.match(E.renderMain(), /retry-api/);
});

test('expired login stays at the real login entry', async () => {
  const E = client(async () => response(401, { ok: false, error: 'login_required' }));
  await E.api.bootstrap();
  assert.match(E.renderMain(), /\/auth\/(?:feishu\/)?login/);
  assert.doesNotMatch(E.renderMain(), /授权登录 \(个人主库管理员\)/);
});

test('unimplemented actions cannot mutate live state or claim success even when invoked directly', () => {
  const E = client();
  E.state.apiMode = 'live'; E.state.ownerId = E.state.libraryCtx = 'owner';
  E.state.categoriesEvo = [{ category: '业务经验', mode: 'review' }];
  E.state.skills = [{ id: 'skill', lifecycle: 'draft' }];
  E.state.experiences = [{ id: 'note', status: 'active' }];
  for (const action of ['promote-cat', 'demote-cat', 'toggle-dormant', 'publish-handbook',
    'habit-confirm', 'habit-dismiss', 'eval-skill', 'skill-stage', 'simulate-sync', 'reset-demo', 'restore-exp']) {
    const before = JSON.stringify(E.state);
    E.applyAction(action, { id: 'skill', cat: '业务经验', next: 'active' });
    assert.equal(JSON.stringify(E.state), before, action);
    assert.doesNotMatch(E.lastToast?.msg || '', /已发布|评测完成|已推进|已恢复/);
  }
});

test('planned evolution pages contain no fabricated outcome rates or enabled write controls', () => {
  const E = client(); E.state.apiMode = 'live';
  for (const sub of ['metrics', 'habits', 'handbook', 'errors']) {
    E.state.evoSubtab = sub;
    const html = E.views.evolution();
    assert.match(html, /待开发|尚未接入/);
    assert.doesNotMatch(html, /6\.2%|68\.5%|87\.5%|持续下降|已推进/);
    for (const button of html.matchAll(/<button\b[^>]*data-act="(?:promote-cat|publish-handbook|toggle-dormant|habit-confirm)"[^>]*>/g)) {
      assert.match(button[0], /disabled/);
    }
  }
});

test('successful empty skill catalog clears previous records; catalog metadata is not a generated active Skill', async () => {
  let rows = [{ id: 'real', name: 'actual-skill', description: 'Real catalog description', file_count: 8, source: 'codex' }];
  const E = client(async () => response(200, { ok: true, skills_catalog: { rows } }));
  E.state.apiMode = 'live';
  await E.api.loadEvolutionAndSkills();
  assert.equal(E.state.skills[0].lifecycle, 'catalog');
  assert.equal(E.state.skills[0].tokenCost, null);
  assert.equal(E.state.skills[0].linuxSynced, null);
  assert.equal(E.state.skills[0].mdPreview, null);
  const html = E.views.skills();
  assert.match(html, /actual-skill|Real catalog description/);
  assert.doesNotMatch(html, /3 个达标|正式生效 1|已写入本地 Skills/);
  rows = [];
  await E.api.loadEvolutionAndSkills();
  assert.equal(E.state.skills.length, 0);
  assert.match(E.views.skills(), /暂无/);
});

test('zero overview values remain zero and do not use prototype totals or live health claims', async () => {
  const E = client(async (_url) => response(200, _url.endsWith('/me') ? me : {
    ok: true, metrics: { active_notes: 0, pending_live: 0, pending_backfill: 0 }, recent_events: [],
  }));
  await E.api.bootstrap();
  const html = E.views.overview();
  assert.match(html, /0 条/);
  assert.doesNotMatch(html, /739|全量在线|在线健康|系统环境经验已进入半自动/);
});

test('a failed page read cannot display previous library records and a fresh retry restores real data', async () => {
  let fail = true;
  const E = client(async url => {
    if (url.endsWith('/me')) return response(200, me);
    if (url.includes('/overview')) return response(200, { ok: true, metrics: { active_notes: 1, pending_live: 0, pending_backfill: 0 } });
    return fail ? response(503, { ok: false, error: 'unavailable' }) : response(200, { ok: true, items: [{ id: 'actual', problem: '真实经验', solution: '真实解决办法' }] });
  });
  E.state.apiMode = 'live'; E.state.route = 'experiences';
  E.state.experiences = [{ id: 'old-library', problem: 'previous library secret' }];
  await E.api.loadTab('experiences');
  assert.match(E.renderMain(), /加载失败/);
  assert.doesNotMatch(E.renderMain(), /previous library secret/);
  fail = false;
  await E.api.bootstrap();
  assert.equal(E.state.experiences[0].id, 'actual');
  assert.doesNotMatch(E.renderMain(), /加载失败/);
});

test('switching to a readonly library clears private inbox and pipeline state before requesting data', async () => {
  const E = client(async () => response(200, { ok: true, metrics: { active_notes: 0, pending_live: 0, pending_backfill: 0 } }));
  E.state.apiMode = 'live'; E.state.ownerId = E.state.libraryCtx = 'A'; E.state.route = 'review-inbox';
  E.state.proposals = [{ uid: 'private', track: 'live' }]; E.state.livePipeline = { private: true };
  const loading = E.api.switchLibrary('B');
  assert.equal(E.state.proposals.length, 0);
  assert.equal(E.state.livePipeline, null);
  await loading;
  assert.equal(E.canWrite(), false);
});

test('a history chain delayed at trash cannot write or issue more old-library requests after switching', async () => {
  let release, started;
  const waiting = new Promise(resolve => { started = resolve; });
  const calls = [];
  const E = client(async url => {
    calls.push(url);
    if (url.includes('view=projects')) return response(200, { ok: true, projects: [{ project: 'A-private', block_count: 1 }] });
    if (url.includes('view=trash')) { started(); return new Promise(resolve => { release = resolve; }); }
    return response(200, { ok: true, blocks: [{ block_id: 'A-block' }], messages: [] });
  });
  E.state.apiMode = 'live'; E.state.ownerId = E.state.libraryCtx = 'A';
  const task = E.api.loadHistory(); await waiting;
  E.state.libraryCtx = 'B'; E.state.libraryEpoch += 1; E.clearLiveData();
  release(response(200, { ok: true, items: [] })); await task;
  assert.equal(E.state.historyProjects.length, 0);
  assert.equal(E.state.historyBlocks.length, 0);
  assert.equal(calls.some(url => url.includes('project_id=A-private')), false);
});

test('same-library return after A to B to A also discards the earlier request', async () => {
  let release;
  const E = client(() => new Promise(resolve => { release = resolve; }));
  E.state.apiMode = 'live'; E.state.ownerId = E.state.libraryCtx = 'A';
  const task = E.api.loadExperiences();
  E.state.libraryEpoch += 2;
  release(response(200, { ok: true, items: [{ id: 'stale-A' }] })); await task;
  assert.equal(E.state.experiences.length, 0);
});

test('readonly history actions do not call a write API when invoked directly', () => {
  const E = client(); E.state.apiMode = 'live'; E.state.ownerId = 'A'; E.state.libraryCtx = 'B';
  E.state.historyProjects = [{ key: 'project' }];
  let writes = 0; E.api.manageHistoryProject = () => { writes++; };
  E.applyAction('toggle-proj-del', { key: 'project' });
  assert.equal(writes, 0);
});
