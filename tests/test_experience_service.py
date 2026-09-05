"""Real SQLite experience behavior: evidence, reuse, conditions and revisions."""
import json
import pytest
from evolvmem.context_models import (
    ContextContentType, ContextItemDraft, ContextLayers, ContextMode,
    ContextScope, ContextStatus,
)
from evolvmem.context_service import ContextService
from evolvmem.experience_service import ExperienceService
from evolvmem.experience_sources import ExperienceSourceResolver
from evolvmem.memory_store import MemoryStore


@pytest.fixture
def experiences(test_config, tmp_path):
    source_root = tmp_path / 'codex-sessions'
    source_root.mkdir()
    transcript = source_root / 'rollout-2026-task-1.jsonl'
    transcript.write_text(json.dumps({
        'type': 'response_item',
        'payload': {
            'type': 'function_call_output',
            'output': 'HTTP 分页验证返回 20 条，翻页不重复，总数与数据库一致',
        },
    }, ensure_ascii=False) + '\n', encoding='utf-8')
    with MemoryStore(test_config):
        pass
    core = ContextService(test_config)
    core.initialize(mode=ContextMode.SHADOW, adapter='codex')
    resolver = ExperienceSourceResolver(codex_roots=(source_root,))
    service = ExperienceService(core, source_resolver=resolver)
    service._test_source_ref = f'{transcript.resolve()}#1'
    service._test_source_root = source_root
    yield service
    core.close()


def case(**changes):
    value = dict(project='demo', problem='列表打开缓慢，全量加载订单',
                 conditions={'data_size': 'large', 'freshness': 'realtime'},
                 steps=['分段测量查询与渲染耗时', '数据库按页查询，每页只返回需要的订单'],
                 rationale='接口传输和渲染的数据减少，分页后响应恢复正常',
                 result='同一订单列表从全量请求变为按页读取，翻页与总数核对通过',
                 applicability=['大量列表数据'], exclusions=['少量数据且耗时发生在外部接口'],
                 transferable=True)
    value.update(changes)
    return value


def proof(experiences, task='task-1', event='verify-1', **changes):
    quote = changes.get(
        'quote', 'HTTP 分页验证返回 20 条，翻页不重复，总数与数据库一致')
    source_ref = changes.get('source_ref')
    if source_ref is None:
        transcript = experiences._test_source_root / f'rollout-2026-{task}.jsonl'
        if not transcript.exists():
            transcript.write_text(json.dumps({
                'type': 'response_item',
                'payload': {'type': 'function_call_output', 'output': quote},
            }, ensure_ascii=False) + '\n', encoding='utf-8')
        source_ref = f'{transcript.resolve()}#1'
    value = dict(task_id=task, event_id=event, outcome='success', level='technical',
                 source_kind='tool_result', source_ref=source_ref, quote=quote,
                 note='分页 HTTP 验证返回 20 条，翻页不重复，总数与数据库一致',
                 conditions={'data_size':'large','freshness':'realtime'})
    value.update(changes)
    return value


def test_verified_case_keeps_steps_and_can_be_recalled(experiences):
    saved = experiences.record(case(), evidence=proof(experiences))
    assert saved['status'] == 'active'
    hits = experiences.recall(project='demo', query='列表打开缓慢', constraints={'data_size':'large'})
    assert [h['id'] for h in hits['results']] == [saved['id']]
    hit = hits['results'][0]
    assert hit['validation_level'] == 'single_verified'
    assert hit['steps'][1] == '数据库按页查询，每页只返回需要的订单'
    assert saved['sources'][0]['source_ref'] == experiences._test_source_ref
    assert hit['verification'][0]['level'] == 'technical'
    assert hit['verification'][0]['conditions'] == {
        'data_size': 'large', 'freshness': 'realtime'}


def test_unverified_claim_is_candidate_not_success(experiences):
    saved = experiences.record(case(result='助手说已经修复'))
    assert saved['status'] == 'candidate'
    assert experiences.recall(project='demo', query='列表打开缓慢')['results'] == []
    with pytest.raises(ValueError):
        experiences.outcome(saved['id'], {'outcome':'success','note':'已完成'})


def test_same_verification_replay_is_idempotent_and_independent_task_promotes(experiences):
    first = experiences.record(case(), evidence=proof(experiences))
    second = experiences.record(case(), evidence=proof(experiences))
    assert second['id'] == first['id']
    assert second['success_count'] == 1
    experiences.outcome(first['id'], proof(experiences, 'task-2', 'verify-2'))
    result = experiences.read(first['id'])
    assert result['success_count'] == 2
    assert result['validation_level'] == 'repeated_verified'
    # A second test in the same task is evidence, not another independent use.
    experiences.outcome(first['id'], proof(experiences, 'task-2', 'verify-3'))
    assert experiences.read(first['id'])['success_count'] == 2


def test_inapplicable_only_excludes_its_conditions(experiences):
    saved = experiences.record(case(), evidence=proof(experiences))
    experiences.outcome(saved['id'], proof(experiences, 'new-task', 'feedback', outcome='inapplicable',
                                         conditions={'data_size':'small'}))
    assert experiences.read(saved['id'])['status'] == 'active'
    assert experiences.recall(project='demo', query='列表打开缓慢', constraints={'data_size':'small'})['results'] == []
    assert experiences.recall(project='demo', query='列表打开缓慢', constraints={'data_size':'large'})['results']
    assert experiences.read(saved['id'])['failure_count'] == 0


def test_derived_case_preserves_original_and_cross_project_requires_transferable(experiences):
    old = experiences.record(case(transferable=False), evidence=proof(experiences))
    assert experiences.recall(project='other', query='列表打开缓慢')['results'] == []
    new = experiences.record(case(project='other', conditions={'data_size':'small'},
                                  parent_experience_id=old['id']), evidence=proof(
                                      experiences, 'other-task',
                                      conditions={'data_size': 'small'}))
    assert new['id'] != old['id']
    assert experiences.read(old['id'])['status'] == 'active'
    assert experiences.read(new['id'])['parent_experience_id'] == old['id']


def test_corrected_verification_recomputes_without_double_counting(experiences):
    saved = experiences.record(case(), evidence=proof(experiences))
    experiences.outcome(saved['id'], proof(experiences, outcome='contradicted', revision=2,
                                         note='复查翻页发现遗漏订单，原成功判定撤回'))
    row = experiences.read(saved['id'])
    assert row['success_count'] == 0
    assert row['validation_level'] == 'contradicted'
    assert experiences.recall(project='demo', query='列表打开缓慢')['results'] == []


def test_recall_and_unknown_outcome_never_count_as_success(experiences):
    saved = experiences.record(case(), evidence=proof(experiences))
    experiences.recall(project='demo', query='列表打开缓慢')
    experiences.outcome(saved['id'], proof(experiences, 'task-2', 'use', outcome='used'))
    experiences.outcome(saved['id'], proof(experiences, 'task-2', 'end', outcome='unknown'))
    assert experiences.read(saved['id'])['success_count'] == 1


def test_positive_evidence_must_cover_case_conditions_and_recall_scope(experiences):
    with pytest.raises(ValueError, match='cover case conditions'):
        experiences.record(case(), evidence=proof(
            experiences, conditions={'data_size': 'large'}))
    with pytest.raises(ValueError, match='derive a case'):
        experiences.record(case(conditions={}), evidence=proof(experiences))

    saved = experiences.record(case(), evidence=proof(experiences))
    assert experiences.recall(
        project='demo', query='列表打开缓慢',
        constraints={'data_size': 'large', 'freshness': 'batch'},
    )['results'] == []
    assert experiences.recall(
        project='demo', query='列表打开缓慢',
        constraints={'data_size': 'large', 'freshness': 'realtime'},
    )['results'][0]['id'] == saved['id']


def test_failure_outside_case_conditions_is_rejected(experiences):
    saved = experiences.record(case(), evidence=proof(experiences))
    with pytest.raises(ValueError, match='outside case conditions'):
        experiences.outcome(saved['id'], proof(
            experiences, 'other-task', 'failed', outcome='failure',
            conditions={'data_size': 'small'}))
    assert experiences.read(saved['id'])['failure_count'] == 0


def test_revision_must_increment_and_snapshot_includes_source_and_level(experiences):
    saved = experiences.record(case(), evidence=proof(experiences))
    second = experiences._test_source_root / 'rollout-2026-task-1-second.jsonl'
    second.write_text(json.dumps({
        'type': 'response_item',
        'payload': {'type': 'function_call_output',
                    'output': '第二次验证也通过分页核对'},
    }, ensure_ascii=False) + '\n', encoding='utf-8')

    with pytest.raises(ValueError, match='higher revision'):
        experiences.outcome(saved['id'], proof(
            experiences, level='business', source_ref=f'{second.resolve()}#1',
            quote='第二次验证也通过分页核对'))
    with pytest.raises(ValueError, match='next revision'):
        experiences.outcome(saved['id'], proof(
            experiences, revision=3, source_ref=f'{second.resolve()}#1',
            quote='第二次验证也通过分页核对'))
    updated = experiences.outcome(saved['id'], proof(
        experiences, revision=2, level='business',
        source_ref=f'{second.resolve()}#1', quote='第二次验证也通过分页核对'))
    assert updated['evidence'][-1]['verification_level'] == 'business'
    assert updated['sources'][-1]['source_ref'] == f'{second.resolve()}#1'


def test_source_ref_can_be_resolved_from_task_id(experiences):
    evidence = proof(experiences)
    evidence.pop('source_ref')
    saved = experiences.record(case(), evidence=evidence)
    assert saved['sources'][0]['source_ref'] == experiences._test_source_ref


def test_current_task_uses_codex_thread_environment(experiences, monkeypatch):
    evidence = proof(experiences, 'env-thread-42')
    evidence.pop('source_ref')
    evidence['task_id'] = 'current'
    monkeypatch.setenv('CODEX_THREAD_ID', 'env-thread-42')
    saved = experiences.record(case(), evidence=evidence)
    assert saved['evidence'][0]['task_id'] == 'env-thread-42'


@pytest.mark.parametrize('explicit_ref', [False, True])
@pytest.mark.parametrize('task_id', ['current', None])
def test_native_current_feedback_without_environment_is_bound_and_idempotent(
        experiences, monkeypatch, explicit_ref, task_id):
    monkeypatch.delenv('CODEX_THREAD_ID', raising=False)
    monkeypatch.delenv('CODEX_SESSION_ID', raising=False)
    native_id = '01a072da-f744-7781-800c-d7366b2cb509'
    quote = '按这个方法验收页面通过'
    transcript = experiences._test_source_root / f'rollout-2026-09-06T02-35-47-{native_id}.jsonl'
    transcript.write_text('\n'.join(json.dumps(event, ensure_ascii=False) for event in [
        {'type': 'session_meta', 'payload': {'id': native_id}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
         'content': [{'type': 'input_text', 'text': quote}]}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': quote}},
    ]) + '\n')
    evidence = dict(task_id=task_id, event_id='native-confirmation',
                    outcome='confirmed', level='user_confirmed',
                    source_kind='user_confirmation', quote=quote,
                    note='用户验证页面', conditions=case()['conditions'])
    if explicit_ref:
        evidence['source_ref'] = f'{transcript}#3'
        monkeypatch.setenv('CODEX_THREAD_ID', 'stale-mcp-thread')
    saved = experiences.record(case(), evidence=evidence)
    replay = experiences.outcome(saved['id'], evidence)
    assert replay['success_count'] == 1
    assert len(replay['evidence']) == 1
    assert replay['evidence'][0]['task_id'] == native_id
    assert replay['sources'][0]['source_ref'] == f'{transcript}#2'
    # Replaying the canonical row or changing the client event label is still
    # evidence from one task, not another independently verified success.
    replay = experiences.outcome(saved['id'], dict(
        evidence, task_id=native_id, source_ref=f'{transcript}#2', event_id='retry'))
    assert replay['success_count'] == 1
    assert len(replay['sources']) == 1


def test_existing_source_id_reuses_bound_snapshot_but_not_another_task(experiences):
    saved = experiences.record(case(), evidence=proof(experiences))
    source_id = saved['sources'][0]['id']
    replay = proof(experiences, event='verify-2')
    for field in ('source_kind', 'source_ref', 'quote'):
        replay.pop(field)
    replay['source_id'] = source_id
    updated = experiences.outcome(saved['id'], replay)
    assert updated['success_count'] == 1
    assert '来源摘录：HTTP 分页验证返回 20 条' in updated['evidence'][-1]['note']

    forged = dict(replay, task_id='forged-task', event_id='verify-3')
    with pytest.raises(ValueError, match='already bound'):
        experiences.outcome(saved['id'], forged)


def test_current_existing_source_id_uses_its_previously_verified_task(experiences, monkeypatch):
    monkeypatch.setenv('CODEX_THREAD_ID', 'stale-mcp-thread')
    saved = experiences.record(case(), evidence=proof(experiences))
    evidence = dict(task_id='current', event_id='verify-2',
                    outcome='success', level='technical',
                    source_id=saved['sources'][0]['id'],
                    note='沿用已绑定的验证来源', conditions=case()['conditions'])
    replay = experiences.outcome(saved['id'], evidence)
    assert replay['evidence'][-1]['task_id'] == 'task-1'
    assert replay['success_count'] == 1


def test_tool_result_cannot_claim_user_confirmation_level(experiences):
    with pytest.raises(ValueError, match='cannot claim user_confirmed'):
        experiences.record(case(), evidence=proof(
            experiences, level='user_confirmed'))


def test_sensitive_case_and_evidence_are_rejected(experiences):
    with pytest.raises(ValueError, match='sensitive case'):
        experiences.record(case(result='api_key=sk-secret-value'))
    with pytest.raises(ValueError, match='sensitive evidence'):
        experiences.record(case(), evidence=proof(
            experiences, note='token=secret-token-value'))


def test_legacy_experience_without_payload_does_not_abort_recall(experiences):
    with experiences.store.transaction():
        experiences.store.create_item(ContextItemDraft(
            identity_key='legacy:experience:no-payload',
            content_type=ContextContentType.EXPERIENCE,
            layers=ContextLayers('列表打开缓慢 legacy', '列表打开缓慢 legacy',
                                 'legacy only', 'legacy-v1'),
            project='demo', scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE, confidence=.9))
    saved = experiences.record(case(), evidence=proof(experiences))
    hits = experiences.recall(project='demo', query='列表打开缓慢')
    assert [hit['id'] for hit in hits['results']] == [saved['id']]


def test_recall_skips_oversized_preview_and_keeps_scanning(experiences):
    experiences.config.context_inject_related_max_chars = 1900
    oversized = case(problem='列表性能分页 ' + '超长说明' * 80,
                     steps=['超长步骤' * 120 for _ in range(10)],
                     transferable=True)
    experiences.record(oversized, evidence=proof(experiences, 'big-task'))
    expected = []
    for index in range(3):
        saved = experiences.record(
            case(problem=f'列表性能分页 案例{index}',
                 conditions={'data_size': 'large', 'freshness': 'realtime',
                             'case': str(index)},
                 steps=[f'分页步骤{index}']),
            evidence=proof(
                experiences, f'task-{index + 2}', conditions={
                    'data_size': 'large', 'freshness': 'realtime',
                    'case': str(index)}))
        expected.append(saved['id'])
    hits = experiences.recall(
        project='demo', query='列表性能分页',
        constraints={'data_size': 'large', 'freshness': 'realtime'})
    assert [hit['id'] for hit in hits['results']] == expected
    assert hits['used_chars'] <= 1900
