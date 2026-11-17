"""Operator browsing sees Core-only knowledge without changing its evidence."""
import json

import pytest

from evolvmem.context_models import (
    ContextContentType, ContextItemDraft, ContextLayers, ContextMode,
    ContextScope, ContextStatus, ContextServiceError,
)
from tests.test_experience_service import experiences, case, proof
from tests.test_web_server import http_server, _get


def test_overview_distinguishes_verified_cases_from_candidates_and_popular_facts(experiences):
    experiences.record(case(), evidence=proof(experiences))
    experiences.record(case(problem='尚未验证的调优方法'))
    with experiences.store.transaction():
        popular = experiences.store.create_item(ContextItemDraft(
            identity_key='popular-fact', content_type=ContextContentType.FACT,
            layers=ContextLayers('高频事实', '命中很多次', '这是事实，不是已验证经验', 'test'),
            project='demo', status=ContextStatus.ACTIVE))
        experiences.store._connection().execute(
            'UPDATE context_items SET access_count=50 WHERE id=?', (popular.id,))
    result = experiences.core.insights().overview()
    assert result['verified_experiences'] == 1
    assert result['candidate_experiences'] == 1
    assert result['repeated_experiences'] == 0
    assert result['unfinished_workstreams'] == 0


def test_experience_filters_apply_before_pagination_and_search_steps(experiences):
    wanted = experiences.record(case(), evidence=proof(experiences))
    experiences.record(case(project='other'))
    pending = experiences.record(case(problem='列表查询的新方法'))
    model = experiences.core.insights()
    result = model.experiences({'project': 'demo', 'status': 'verified', 'page_size': '1'})
    assert [r['id'] for r in result['rows']] == [wanted['id']]
    assert result['total'] == 1
    assert 'sources' not in result['rows'][0]
    assert 'evidence' not in result['rows'][0]
    result = model.experiences({'project': 'demo', 'q': '分段测量', 'page_size': '1', 'page': '2'})
    assert result['total'] == 2
    assert [r['id'] for r in result['rows']] == [wanted['id']]
    assert model.experiences({'status': 'candidate', 'project': 'demo'})['rows'][0]['id'] == pending['id']
    assert model.experiences({'q': "' OR 1=1 --"})['total'] == 0


def test_detail_shows_latest_feedback_sources_and_derived_cases_without_writing(experiences):
    original = experiences.record(case(), evidence=proof(experiences))
    experiences.outcome(original['id'], proof(experiences, 'feedback-task', 'scope',
        outcome='inapplicable', conditions={'data_size': 'small'}))
    child = experiences.record(case(conditions={'data_size': 'small'},
                                  parent_experience_id=original['id']))
    conn = experiences.store._connection()
    before = conn.total_changes
    # The deployed operator console runs in compat; browsing must not turn on
    # Agent recall in that mode or invent usage/success evidence.
    experiences.core._mode = ContextMode.COMPAT
    model = experiences.core.insights()
    result = model.experience(original['id'])
    assert result['success_count'] == 1 and result['failure_count'] == 0
    assert result['evidence'][-1]['conditions'] == {'data_size': 'small'}
    assert result['verification'][0]['level'] == 'technical'
    assert result['sources'][0]['source_ref'] == experiences._test_source_ref
    assert [r['id'] for r in result['derived_cases']] == [child['id']]
    assert model.experience(child['id'])['parent_case']['id'] == original['id']
    model.overview()
    model.experiences({})
    assert conn.total_changes == before
    with pytest.raises(ContextServiceError):
        experiences.read(original['id'])


def test_expired_and_deleted_cases_do_not_inflate_verified_count(experiences):
    expired = experiences.record(case(), evidence=proof(experiences))
    deleted = experiences.record(case(problem='已删除案例'))
    with experiences.store.transaction():
        experiences.store._connection().execute(
            "UPDATE context_items SET expires_at='2000-01-01 00:00:00' WHERE id=?", (expired['id'],))
        experiences.store._connection().execute(
            "UPDATE context_items SET status='deleted' WHERE id=?", (deleted['id'],))
    model = experiences.core.insights()
    assert model.overview()['verified_experiences'] == 0
    assert model.experiences({'status': 'verified'})['rows'] == []
    assert model.experiences({'status': 'inactive'})['rows'][0]['id'] == expired['id']
    with pytest.raises(LookupError):
        model.experience(deleted['id'])


def test_summaries_keep_last_success_visible_after_failed_refresh(experiences):
    store = experiences.store
    with store.transaction():
        item = store.create_item(ContextItemDraft(
            identity_key='project:demo:knowledge:current',
            content_type=ContextContentType.PROJECT_SUMMARY,
            layers=ContextLayers('订单项目进展', '已完成分页；下一步检查筛选', '详细验证记录', 'test'),
            project='demo', status=ContextStatus.ACTIVE))
        store._connection().execute(
            "INSERT INTO context_project_rollups(project,current_context_id,status,covered_through,updated_at) "
            "VALUES ('demo',?,'failed','2026-09-05 00:00:00','2026-09-06 00:00:00')", (item.id,))
    model = experiences.core.insights()
    row = model.summaries({})['rows'][0]
    assert row['id'] == item.id and row['status'] == 'failed'
    assert row['summary'] == '订单项目进展'
    assert 'l2' not in row
    assert model.summary(item.id)['l1'] == '已完成分页；下一步检查筛选'
    assert model.overview()['ready_summaries'] == 0


def test_workstream_detail_uses_current_checkpoint_and_does_not_change_focus(experiences):
    from evolvmem.continuity_models import ContinuityCheckpointRequest
    from evolvmem.continuity_service import ContinuityService
    from evolvmem.workspace_identity import WorkspaceIdentityProvider
    from tests.test_continuity_service import _bind
    core = experiences.core
    identity = WorkspaceIdentityProvider(core.config.data_dir / 'workspace.key')
    identity.bootstrap_key()
    workspace = experiences._test_source_root
    _bind(core.store, identity, workspace, 'demo')
    continuity = ContinuityService(core.config, core.store, identity)
    first = continuity.checkpoint(ContinuityCheckpointRequest(
        action='create', workspace_path=str(workspace), project_hint='demo',
        objective='完成订单分页', current_step='实现', next_action='验证翻页',
        make_focus=True, expected_focus_revision=0))
    latest = continuity.checkpoint(ContinuityCheckpointRequest(
        action='complete', workspace_path=str(workspace), workstream_id=first.workstream_id,
        completed_steps=('验证翻页无重复',), current_step='完成', next_action='本任务已完成',
        expected_checkpoint_revision=first.checkpoint_revision,
        expected_state_version=first.state_version))
    conn = core.store._connection()
    before = conn.total_changes
    model = core.insights()
    assert model.workstreams({'status': 'unfinished'})['rows'] == []
    rows = model.workstreams({'status': 'completed'})['rows']
    assert len(rows) == 1
    detail = model.workstream(first.workstream_id)
    assert detail['checkpoint_revision'] == latest.checkpoint_revision
    assert detail['completed_steps'] == ['验证翻页无重复']
    assert detail['next_action'] == '本任务已完成'
    assert detail['objective'] == '完成订单分页'
    assert 'workspace_fingerprint' not in detail
    assert conn.total_changes == before


def test_http_insights_empty_lists_and_missing_details(http_server):
    import urllib.error
    base, _, _ = http_server
    assert _get(base + '/api/insights')['verified_experiences'] == 0
    for path in ('experiences', 'project-summaries', 'workstreams'):
        assert _get(f'{base}/api/{path}')['rows'] == []
    for path in ('experiences/999999', 'project-summaries/999999', 'workstreams/ws_missing'):
        with pytest.raises(urllib.error.HTTPError) as error:
            _get(f'{base}/api/{path}')
        assert error.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(base + '/api/experiences?status=imaginary')
    assert error.value.code == 400


def test_http_insights_assets_have_correct_types(http_server):
    import urllib.request
    base, _, _ = http_server
    for name, mime in [('insights.js', 'text/javascript'), ('insights.css', 'text/css')]:
        with urllib.request.urlopen(f'{base}/{name}') as response:
            assert response.headers.get_content_type() == mime
            assert response.read()
