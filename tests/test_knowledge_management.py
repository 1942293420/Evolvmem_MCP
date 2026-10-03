"""Knowledge management behavior through real stores and service boundaries."""
import json

import pytest

from evolvmem.context_models import ContextContentType, ContextItemDraft, ContextLayers, ContextStatus, ContextMode
from evolvmem.project_models import ProjectResolutionDecision
from evolvmem.project_ownership import load_ownership
from tests.test_web_server import _make_service


@pytest.fixture
def service(test_config):
    instance = _make_service(test_config, mode=ContextMode.SHADOW)
    with instance.store.transaction():
        ps = instance._project_store()
        ps.register_project('eva')
        ps.add_alias('智能客服', 'eva')
        ps.register_project('evolvmem')
    yield instance
    instance.close()


def manager(service):
    assert hasattr(service, 'knowledge'), 'Unified knowledge management is not implemented'
    return service.knowledge()


def test_human_confirmation_becomes_trusted_for_actual_recall(service):
    result = service.legacy_facade().add('eva:business:fact:rule', '客服规则：退款需要核对原订单和实际支付金额。')
    cid = service.store.resolve_legacy_mapping(result)
    with service.store.transaction():
        ps = service._project_store()
        ps.record_resolution(cid, ProjectResolutionDecision.unresolved('test', ()))
        revision = service.store._connection().execute('SELECT revision FROM context_project_resolutions WHERE item_id=?', (cid,)).fetchone()[0]
        ps.accept_resolution(cid, 'eva', expected_revision=revision)
    assert load_ownership(service.store, [cid])[cid].confirmed


def test_list_includes_context_only_candidates_and_filters_project(service):
    native = service.store.create_item(ContextItemDraft(
        identity_key='native:experience', content_type=ContextContentType.EXPERIENCE,
        layers=ContextLayers('客服经验', '核对订单可定位退款金额不一致问题。', '合成来源', 'test'),
        project='eva', status=ContextStatus.CANDIDATE))
    kb = manager(service)
    result = kb.list_items({'project': 'eva'})
    assert native.id in [x['id'] for x in result['items']]
    assert native.id not in [x['id'] for x in kb.list_items({'project': 'evolvmem'})['items']]
    assert kb.detail(native.id)['status'] == 'candidate'


def test_create_edit_reassign_archive_and_search(service):
    kb = manager(service)
    row = kb.create({'title': '退款核对规则', 'body': '退款时必须先核对原订单和实际支付金额。', 'project': 'eva', 'content_type': 'reference', 'action': 'publish'})
    assert row['status'] == 'active'
    assert row['ownership']['state'] == 'confirmed'
    assert kb.list_items({'q': '退款核对'})['total'] == 1
    changed = kb.update(row['id'], {'expected_revision': row['revision'], 'title': '退款核对规则', 'body': '退款时先核对实际支付金额，然后确认订单状态。'})
    assert '订单状态' in kb.detail(row['id'])['body']
    moved = kb.assign(row['id'], {'expected_revision': changed['revision'], 'project': 'evolvmem'})
    assert kb.list_items({'project': 'eva'})['total'] == 0
    assert kb.list_items({'project': 'evolvmem'})['total'] == 1
    archived = kb.transition(row['id'], {'expected_revision': moved['revision'], 'action': 'archive'})
    assert archived['status'] == 'archived'
    assert kb.list_items({'status': 'active'})['total'] == 0


def test_stale_edit_cannot_overwrite_newer_content(service):
    kb = manager(service)
    row = kb.create({'title': '客服资料', 'body': '这是一份待核对的客服资料内容。', 'project': 'eva'})
    kb.update(row['id'], {'expected_revision': row['revision'], 'body': '最新版本已经核对来源和金额。'})
    with pytest.raises(ValueError, match='revision_conflict'):
        kb.update(row['id'], {'expected_revision': row['revision'], 'body': '过期页面发来的旧版本内容。'})
    assert kb.detail(row['id'])['body'] == '最新版本已经核对来源和金额。'


def test_edit_legacy_record_updates_projection_and_fts(service):
    legacy_id = service.legacy_facade().add('project:eva:fact:shipping', '旧规则：发货前只检查地址。')
    item_id = service.store.resolve_legacy_mapping(legacy_id)
    kb = manager(service)
    row = kb.detail(item_id)
    kb.update(item_id, {'expected_revision': row['revision'], 'body': '新规则：发货前检查地址和危险品运输限制。'})
    assert '危险品' in service.legacy_facade().get_by_id(legacy_id)['value']
    assert kb.list_items({'q': '危险品'})['total'] == 1


def test_rule_save_changes_future_decisions_and_is_read_back(service):
    kb = manager(service)
    policy = kb.rules.read()
    sample = {'title': 'EVA 退款规则', 'body': 'EVA 客服退款前核对原始订单和金额。', 'confidence': .9, 'source': '测试来源'}
    assert kb.preview(sample)['action'] == 'auto'
    settings = {**policy['settings'], 'auto_min_confidence': .95}
    saved = kb.rules.save({'expected_revision': policy['revision'], 'settings': settings, 'instructions': '核对来源；退款金额资料需要明确支付依据。'})
    assert saved['revision'] != policy['revision']
    assert kb.preview(sample)['action'] == 'review'
    assert '明确支付依据' in kb.rules.prompt()
    with pytest.raises(ValueError, match='revision_conflict'):
        kb.rules.save({'expected_revision': policy['revision'], 'settings': settings})


def test_unique_project_text_resolves_and_conflicting_text_waits(service):
    kb = manager(service)
    clear = kb.preview({'title': 'EVA 客服', 'body': 'EVA 客服的订单退款规则已经确认。', 'confidence': .95, 'source': '用户会话'})
    assert (clear['project'], clear['action']) == ('eva', 'auto')
    conflict = kb.preview({'title': '系统对比', 'body': 'EVA 与 EvolvMem 两个项目的配置比较。', 'confidence': .99, 'source': '用户会话'})
    assert conflict['action'] == 'review'
    assert conflict['project'] == ''


def test_skill_edit_is_persisted_and_used_as_ingestion_prompt(service):
    kb = manager(service)
    current = kb.rules.read()
    content = current['skill'].replace('知识库入库与整理', '知识库入库与整理\n\n业务约定：物流资料优先检查发货日期。')
    saved = kb.rules.save({'expected_revision': current['revision'], 'skill': content})
    assert '优先检查发货日期' in kb.rules.prompt()
    assert saved['skill'] == kb.rules.path.read_text()
    with pytest.raises(ValueError):
        kb.rules.save({'expected_revision': saved['revision'], 'skill': '无格式的内容'})


def test_organize_clear_records_applies_but_leaves_difficult_ones(service):
    kb = manager(service)
    clear = kb.create({'title': 'EVA 运费说明', 'body': 'EVA 客服运费需要根据订单所在地区确定。', 'source': '用户说明', 'confidence': .95})
    unclear = kb.create({'title': '待整理资料', 'body': '后续需要核对这个模块实际采用的工作流程。', 'source': '用户说明'})
    result = kb.organize({'ids': [clear['id'], unclear['id']], 'apply': True})
    assert result['applied'] == 1
    assert kb.detail(clear['id'])['project'] == 'eva'
    assert kb.detail(clear['id'])['status'] == 'active'
    assert kb.detail(unclear['id'])['status'] == 'candidate'


def test_saved_rules_apply_to_real_extraction_writes(service):
    from evolvmem.legacy_models import LegacyExtractionItem, LegacyExtractionRequest
    kb = manager(service)
    policy = kb.rules.read()
    kb.rules.save({'expected_revision': policy['revision'], 'settings': policy['settings']})
    def extracted(key, value):
        return LegacyExtractionItem(key=key, value=value, attribute='fact', confidence=.95)
    result = service.persist_legacy_extraction(LegacyExtractionRequest(
        summary=extracted('project:jiangli:progress:log:test', 'EVA 客服已经核对退款来源，需要按订单金额处理。'),
        candidates=(extracted('misc:unknown', '这个模块需要进一步确认业务规则与适用范围。'),),
        source_session='synthetic-session', max_writes=8))
    summary = kb.detail(result.summary.context_id)
    assert (summary['project'], summary['status']) == ('eva', 'active')
    uncertain = kb.detail(result.candidates[0].context_id)
    assert uncertain['status'] == 'candidate'
    assert service.legacy_facade().get_by_id(result.candidates[0].legacy_id)['status'] == 'candidate'


def test_task_move_updates_focus_and_historical_checkpoint_together(service):
    kb = manager(service)
    oldkey = 'project:eva:workstream:ws_abcdef:checkpoint'
    layers = ContextLayers('客服任务', '目标：核对客服退款规则', json.dumps({'project': 'eva', 'workstream_id': 'ws_abcdef', 'checkpoint_revision': 1, 'state_version': 1}), 'test')
    item = service.store.create_item(ContextItemDraft(identity_key=oldkey, content_type=ContextContentType.WORKSTREAM_CHECKPOINT, layers=layers, project='eva', status=ContextStatus.ACTIVE))
    with service.store.transaction():
        service.store._connection().execute("INSERT INTO continuity_workstreams(id,project,workspace_fingerprint,current_context_id,checkpoint_revision,state_version,status,created_at,updated_at) VALUES('ws_abcdef','eva','fp',?,1,1,'open','2026-01-01','2026-01-01')", (item.id,))
        service.store._connection().execute("INSERT INTO continuity_focus(project,workspace_fingerprint,workstream_id,revision,updated_at) VALUES('eva','fp','ws_abcdef',1,'2026-01-01')")
    row = kb.detail(item.id)
    with pytest.raises(ValueError, match='workstream_move_confirmation_required'):
        kb.assign(item.id, {'expected_revision': row['revision'], 'project': 'evolvmem'})
    kb.assign(item.id, {'expected_revision': row['revision'], 'project': 'evolvmem', 'move_workstream': True})
    assert kb.detail(item.id)['project'] == 'evolvmem'
    ws = service.store._connection().execute("SELECT * FROM continuity_workstreams WHERE id='ws_abcdef'").fetchone()
    assert ws['project'] == 'evolvmem'
    assert service.store._connection().execute("SELECT workstream_id FROM continuity_focus WHERE project='eva'").fetchone()[0] is None
    assert service.store._connection().execute("SELECT workstream_id FROM continuity_focus WHERE project='evolvmem'").fetchone()[0] == 'ws_abcdef'


def test_reassign_structured_experience_updates_payload_used_by_recall(service):
    kb = manager(service)
    case = {'project':'eva','problem':'退款回执缺少原订单信息','steps':['核对原订单'], 'conditions':{}}
    result = service.experiences().record(case)
    row = kb.detail(result['id'])
    kb.assign(row['id'], {'expected_revision':row['revision'], 'project':'evolvmem'})
    actual = kb.detail(row['id'])
    assert json.loads(actual['experience_payload'])['project'] == 'evolvmem'


def test_batch_returns_partial_results_without_overwriting_stale_item(service):
    kb = manager(service)
    a = kb.create({'title':'第一份资料','body':'EVA 客服核对原订单的规则说明。','project':'eva','action':'publish'})
    b = kb.create({'title':'第二份资料','body':'EVA 客服核对付款金额的规则说明。','project':'eva','action':'publish'})
    changed = kb.update(b['id'], {'expected_revision':b['revision'],'title':'刚更新的第二份资料'})
    result = kb.batch({'action':'assign','project':'evolvmem','items':[{'id':r['id'],'expected_revision':r['revision']} for r in (a,b)]})
    assert (result['succeeded'], result['failed']) == (1,1)
    assert kb.detail(a['id'])['project'] == 'evolvmem'
    assert kb.detail(b['id'])['project'] == 'eva'
    assert kb.detail(b['id'])['revision'] == changed['revision']


def test_task_remains_resumable_and_updatable_after_project_move(service, tmp_path):
    from evolvmem.continuity_models import ContinuityCheckpointRequest, ContinuityResumeRequest
    provider = service._workspace_identity()
    provider.bootstrap_key()
    workspace = tmp_path / 'project-workspace'
    workspace.mkdir()
    with service.store.transaction():
        service._project_store().bind_workspace(provider.resolve(str(workspace)).fingerprint, 'eva', method='test', make_default=True)
    cs = service._continuity()
    task = cs.checkpoint(ContinuityCheckpointRequest(action='create',workspace_path=str(workspace),project_hint='eva',objective='核对客服规则',make_focus=True,expected_focus_revision=0))
    row = manager(service).detail(task.context_id)
    manager(service).assign(row['id'], {'expected_revision':row['revision'],'project':'evolvmem','move_workstream':True})
    resumed = cs.resume(ContinuityResumeRequest(workspace_path=str(workspace),project_hint='evolvmem'))
    assert resumed.code == 'ok'
    assert resumed.checkpoint_revision == task.checkpoint_revision + 1
    cs.checkpoint(ContinuityCheckpointRequest(action='update',workspace_path=str(workspace),project_hint='evolvmem',workstream_id=task.workstream_id,expected_checkpoint_revision=resumed.checkpoint_revision,expected_state_version=resumed.state_version,current_step='继续核对'))


def test_saved_skill_reaches_actual_model_prompt(service, monkeypatch):
    from evolvmem import kimi_hooks
    kb = manager(service)
    rule = kb.rules.read()
    kb.rules.save({'expected_revision':rule['revision'],'instructions':'业务补充：退款资料核对原始支付凭证。'})
    prompts=[]
    def model(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        return json.dumps({'memories':[{'key':'SESSION_SUMMARY','value':'EVA 客服已核对原始支付凭证。','attribute':'fact','confidence':.95}]},ensure_ascii=False)
    monkeypatch.setattr(kimi_hooks,'_call_llm_with_retry',model)
    kimi_hooks._extract_candidates([{'role':'user','content':'核对退款'}],None,config=service.config)
    assert '退款资料核对原始支付凭证' in prompts[0]


def test_knowledge_title_is_available_to_ai_search_and_archive_removes_it(service):
    from evolvmem.context_models import ContextSearchRequest
    kb = manager(service)
    row = kb.create({'title':'zebra 手续费约定','body':'核对实际收款金额后，按原始付款凭证计算退款。','project':'eva','action':'publish'})
    query = ContextSearchRequest(query='zebra',project='eva',top_k=10)
    assert row['id'] in {r.id for r in service.search(query)}
    row = kb.update(row['id'],{'expected_revision':row['revision'],'title':'giraffe 手续费约定'})
    assert row['id'] not in {r.id for r in service.search(query)}
    query = ContextSearchRequest(query='giraffe',project='eva',top_k=10)
    assert row['id'] in {r.id for r in service.search(query)}
    kb.transition(row['id'],{'expected_revision':row['revision'],'action':'archive'})
    assert row['id'] not in {r.id for r in service.search(query)}


def test_historical_unreviewed_project_claim_alone_cannot_auto_confirm(service):
    row=service.store.create_item(ContextItemDraft(identity_key='project:eva:old:claim',content_type=ContextContentType.FACT,
        layers=ContextLayers('采购业务约定','核对采购单和付款金额后，再决定是否继续处理。','核对采购单和付款金额后，再决定是否继续处理。','test'),project='eva',status=ContextStatus.ACTIVE,confidence=.95))
    with service.store.transaction():
        service.store._connection().execute("INSERT INTO context_sources(item_id,source_kind,source_ref,extraction_version,created_at) VALUES(?,'manual','old-note','test','2026-01-01')",(row.id,))
    result=manager(service).organize({'ids':[row.id],'apply':True})
    assert result['applied']==0
    assert result['items'][0]['action']=='review'
    assert manager(service).detail(row.id)['ownership']['state']=='excluded'


def test_runtime_paths_and_generic_project_labels_are_not_business_ownership(service):
    kb=manager(service)
    kb.save_project({'project':'hermes','display_name':'Hermes'})
    kb.save_project({'project':'design','display_name':'设计'})
    sample={'title':'升级工具','body':'将 deepseek harness 升级并安装至 ~/.hermes/node，重启服务后验证。','confidence':.99,'source':'会话'}
    assert kb.preview(sample)['action']=='review'
    sample['body']='讨论审批表单设计，核对角色权限与资料来源。'
    assert kb.preview(sample)['action']=='review'
    sample['body']='Hermes 项目的会话资料需要核对来源和项目名称。'
    assert kb.preview(sample)['project']=='hermes'
    sample['body']='采购规则的运行目录为 .hermes，实际业务归属仍需核对。'
    assert kb.preview(sample)['action']=='review'
    sample['body']='续接飞书审批项目，另回答了 EVA 客服版本更新内容。'
    assert kb.preview(sample)['action']=='review'
