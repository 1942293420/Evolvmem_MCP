"""Opt-in synthetic governance data for real HTTP browser acceptance."""
import json

from evolvmem.capability_pipeline import CapabilityPipeline
from evolvmem.capability_store import CapabilityStore
from evolvmem.context_store import _now_iso
from evolvmem.project_modules import ProjectModuleStore
from tests.test_capability_pipeline import service_with_task


TEXT = '发生并发写入时，SQLite 写入应放在同一事务中，失败时整体回滚。'
# Titles are claims under assessment v2. Keep them grounded in the same real
# synthetic source; scenario names belong in the test, not in unsupported titles.
TITLES = {
    'current': 'SQLite 写入应放在同一事务中',
    'pending': '发生并发写入时',
    'invalid': '失败时整体回滚',
    'partial': '并发写入',
}


def seed(service):
    store = service.store
    technical = seed_technical_review(service)
    modules = ProjectModuleStore(store)
    module = modules.create('evo', '订单核对', description='在订单写入时核对记录一致性。', explicit=True)
    modules.suggest('evo', '售后处理', description='等待用户确认的业务模块。')

    def memory(title, *, pending=False, project='evo', unavailable=False):
        runner, task = service_with_task(service.config, store, TEXT)
        with store.transaction():
            store._connection().execute('UPDATE organization_units SET project=?,projects=?,decision=\'manual\' WHERE task_id=?',
                                        (project, json.dumps([project]), task['id']))
        result = CapabilityPipeline(runner).process_task(task, provider=lambda _: {
            'action':'keep', 'category':'database', 'title':title,
            'content':'SQLite 写入应放在同一事务中', 'keywords':['治理验收', 'SQLite'],
            'kind':'procedure', 'trigger':'发生并发写入时', 'conditions':['SQLite'],
            'action_steps':['SQLite 写入应放在同一事务中'], 'result':'',
            'evidence_quotes':[] if pending else [TEXT], 'value_reason':'说明并发写入下可执行的事务步骤。'})
        identity = result['memory_ids'][0]
        source = [row for row in CapabilityStore(store).sources(identity) if row['task_id'] == task['id']][0]
        if unavailable:
            with store.transaction():
                store._connection().execute("UPDATE session_archives SET state='purged' WHERE id=?", (int(task['source_key'].split(':')[1]),))
        return identity, source

    current, current_source = memory(TITLES['current'])
    pending, pending_source = memory(TITLES['pending'], pending=True)
    invalid, _ = memory(TITLES['invalid'], unavailable=True)
    partial, _ = memory(TITLES['partial'])
    memory(TITLES['partial'], project='dsh-a', unavailable=True)
    for source in (current_source, pending_source):
        modules.bind_source(module['module_id'], source['source_id'], quote=TEXT, role='applicable',
                            decision='manual', explicit=True, expected_revision=0)
    now = _now_iso()
    with store.transaction():
        store._connection().execute(
            "INSERT INTO capability_intake(title,content,keywords,suggested_category,source_project,source_ref,fingerprint,created_at,updated_at) "
            "VALUES(?,?,'[]','database','evo','fixture:unanchored','governance-browser-intake',?,?)",
            ('治理验收 · 待分类弱依据', '这条合成资料需要核对原始来源。', now, now))
    enrolled_task = seed_enrolled_review(service)
    return {'technical':technical, 'enrolled_task':enrolled_task, 'current':current, 'pending':pending, 'invalid':invalid, 'partial':partial, 'module_id':module['module_id']}


def seed_technical_review(service):
    from evolvmem import capability_assessment_migration as migration
    from evolvmem.capability_store import CapabilityMemoryDraft
    from tests.test_capability_assessment_migration import manual_source, TEXT as ORIGINAL
    memories = CapabilityStore(service.store)
    identity = memories.upsert(CapabilityMemoryDraft(category="database",
        title="SQLite 写入需要使用事务", content=ORIGINAL),
        source=manual_source(service, title="评估返回异常的合成原件"))
    plan = migration.build_plan(service.store._connection(),
        rule_revision=service.knowledge().rules.read()["revision"])
    calls = []
    result = migration.apply_plan(service, plan, provider=lambda prompt: calls.append(prompt) or {"kind": "invalid"})
    assert result["applied"] == 1 and len(calls) == 2
    memories.set_status(identity, "pending")
    return identity


def seed_enrolled_review(service):
    from evolvmem import governance_sources, conversation_tasks, auto_organization as org
    from evolvmem.context_models import ContextMode
    from evolvmem.session_archive import SessionArchiver
    archive = SessionArchiver(service.config, service.store).archive_session("evo", "kimi",
        "enrolled-browser-source", json.dumps({"messages": [{"role": "user",
        "content": "登记的旧来源仍需核对业务模块归属。"}]}, ensure_ascii=False))
    plan = governance_sources.build_plan(service.store._connection(), keys=[f"archive:{archive.id}"])
    assert governance_sources.apply_plan(service, plan)["applied"] == 1
    task = conversation_tasks.enqueue(service, [f"archive:{archive.id}"])["items"][0]
    current = org._task_row(service, task["id"]); text = current["source_snapshot"]
    org.OrganizationWorker(service.config, mode=ContextMode.SHADOW)._store_units(service, current, [{
        "source_start": 0, "source_end": len(text), "text": text, "title": "登记来源业务模块待核对",
        "category": "reference", "project_hint": "", "evidence_quote": ""}])
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_tasks SET status='review',stage='done' WHERE id=?", (task["id"],))
        service.store._connection().execute("UPDATE organization_units SET decision='review' WHERE task_id=?", (task["id"],))
    assert org.task_view(service, task["id"])["source_current"]
    return task["id"]
