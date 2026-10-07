"""Continuation-aware coalescing and whitespace-only evidence-quote resolution.

Synthetic sources only. The real segmenter, the real project registry/rules
evaluation, the real worker storage path and the real CAS assignment path are
exercised; only the provider answers are scripted.
"""
import json

import pytest

from evolvmem import topic_segmentation
from evolvmem.topic_segmentation import resolve_evidence_quote, segment
from evolvmem.knowledge_api import dispatch
from evolvmem.context_models import ContextMode
from evolvmem.context_store import _now_iso
from tests.test_web_server import _make_service
from tests.unit_model_fixture import UnitProvider, run_worker

LINES = ('Evo 演示项目：先明确验收条件。', '继续细化：补充边界情况与失败路径。')
CONT = '继续细化'


@pytest.fixture
def service(test_config):
    service = _make_service(test_config, mode=ContextMode.SHADOW)
    for project in ('evo', 'shop'):
        service.knowledge().save_project({'project': project})
    service.knowledge().save_project({'project': 'dsh', 'display_name': 'DSH 演示项目'})
    yield service
    service.close()


# ------------------------------------------------------- quote resolution unit

def test_resolve_quote_keeps_the_exact_verbatim_substring():
    text = 'Evo 演示项目：先明确验收条件。'
    assert resolve_evidence_quote(text, '先明确验收条件') == '先明确验收条件'
    assert resolve_evidence_quote(text, text) == text


def test_resolve_quote_only_forgives_the_kind_and_count_of_a_real_separator():
    text = 'AB CD EF'
    # One separator against two, and NBSP/full-width space against a space.
    assert resolve_evidence_quote(text, 'AB  CD EF') == 'AB CD EF'
    assert resolve_evidence_quote(text, 'AB\u00a0CD\u3000EF') == 'AB CD EF'
    text = '继续 细化：补充 边界情况与失败路径。'
    assert resolve_evidence_quote(text, '继续\u00a0细化：补充\t边界情况与失败路径。') == text


def test_resolve_quote_rejects_a_missing_or_extra_separator():
    text = 'AB CD EF'
    assert resolve_evidence_quote(text, 'ABCD EF') is None, 'a dropped separator is not a quote'
    assert resolve_evidence_quote(text, 'AB CDEF') is None
    assert resolve_evidence_quote('ABCD EF', 'AB CD EF') is None, 'an added separator is not a quote'


def test_resolve_quote_rejects_rewrites_case_changes_and_empty_input():
    text = 'Evo 演示项目：先明确验收条件。'
    assert resolve_evidence_quote(text, '') is None
    assert resolve_evidence_quote(text, '   ') is None
    assert resolve_evidence_quote(text, '验收条件明确') is None, 'a rewrite is not a quote'
    assert resolve_evidence_quote(text, 'evo 演示项目') is None, 'case must match exactly'


def test_resolve_quote_never_assembles_a_quote_across_lines():
    text = '继续\n细化：补充边界情况。'
    assert resolve_evidence_quote(text, '继续细化：补充边界情况') is None
    # A verbatim multi-line quote is still verbatim, never reassembled.
    assert resolve_evidence_quote(text, '继续\n细化') == '继续\n细化'


def test_resolve_quote_returns_none_when_the_whitespace_variant_is_ambiguous():
    assert resolve_evidence_quote('AB\u00a0CD 和 AB\u3000CD', 'AB CD') is None
    assert resolve_evidence_quote('AB\u00a0CD 独一份', 'AB CD') == 'AB\u00a0CD'


def test_parse_only_accepts_boolean_true_for_continues_previous():
    from evolvmem.topic_segmentation import _parse
    parsed = _parse(json.dumps({'units': [
        {'title': 'a', 'continues_previous': True},
        {'title': 'b', 'continues_previous': 'true'},
        {'title': 'c', 'continues_previous': 1},
        {'title': 'd'}]}, ensure_ascii=False))
    assert [item['continues_previous'] for item in parsed] == [True, False, False, False]


# ----------------------------------------------- coalescing against real rules

def archive_lines(service, session, lines):
    from evolvmem.session_archive import SessionArchiver
    from tests.test_knowledge_cleaning import ready
    roles = ('user', 'assistant')
    messages = [{'role': roles[index % 2], 'content': line} for index, line in enumerate(lines)]
    source = SessionArchiver(service.config, service.store).archive_session(
        '', 'kimi', session, json.dumps({'messages': messages}, ensure_ascii=False))
    ready(service)
    return source


def prepared(service, source):
    from evolvmem.auto_organization import source_messages, source_snapshot
    key = f'archive:{source.id}'
    text = source_snapshot(service, key)[1]
    spans = topic_segmentation.message_spans(source_messages(service, key))
    return key, text, spans


def base_unit(start, end, **over):
    entry = {'start_id': start, 'end_id': end, 'title': '单元 %d' % start,
             'cleaned_summary': '摘要 %d。' % start, 'category': 'reference',
             'project_hint': '', 'evidence_quote': '', 'disposition': 'keep',
             'disposition_reason': ''}
    entry.update(over)
    return entry


def pair(first=None, second=None):
    """One named task plus its declared continuation on the next message."""
    head = base_unit(1, 1, title='先明确验收条件', cleaned_summary='先明确验收条件。',
                     category='task_requirement', project_hint='evo', evidence_quote='Evo 演示项目')
    tail = base_unit(2, 2, title='继续细化', cleaned_summary='继续细化边界情况。',
                     category='task_requirement', project_hint='evo', evidence_quote=CONT,
                     continues_previous=True)
    head.update(first or {})
    tail.update(second or {})
    return [head, tail]


def scripted_units(service, key, text, spans, shapes, *, size=topic_segmentation.SEGMENT_CHARS):
    replies = json.dumps({'units': shapes}, ensure_ascii=False)
    return segment(text, lambda prompt: replies, records=spans, size=size)


def coalesced(service, key, units, text):
    from evolvmem.auto_organization import coalesce_units
    policy = service.knowledge().rules.read()
    return coalesce_units(service, {'source_key': key}, units, text, policy)


def test_a_same_task_refinement_is_merged_and_the_whole_source_stays_covered(service):
    source = archive_lines(service, 'coalesce-merge', LINES)
    key, text, spans = prepared(service, source)
    units = scripted_units(service, key, text, spans, pair())
    merged = coalesced(service, key, units, text)
    assert len(merged) == 1
    unit = merged[0]
    topic_segmentation.coverage(text, merged)  # whole-source coverage is re-checked
    assert unit['text'] == text[unit['source_start']:unit['source_end']]
    assert unit['source_start'] == units[0]['source_start']
    assert unit['source_end'] == units[1]['source_end']
    assert 'Evo 演示项目' in unit['text'] and '继续细化' in unit['text']
    # The first verifiable project quote and the group's own start flags survive.
    assert unit['evidence_quote'] == 'Evo 演示项目'
    assert unit['project_hint'] == 'evo'
    assert unit['continues_previous'] is False
    assert unit['title'] == '先明确验收条件'
    # Real message roles inside the merged slice are unchanged.
    assert topic_segmentation.unit_messages(unit, spans) == [
        {'role': 'user', 'content': 'Evo 演示项目：先明确验收条件。'},
        {'role': 'assistant', 'content': CONT + '：补充边界情况与失败路径。'}]


def test_different_categories_downgrade_to_reference_and_never_upgrade(service):
    source = archive_lines(service, 'coalesce-category', LINES)
    key, text, spans = prepared(service, source)
    merged = coalesced(service, key,
                       scripted_units(service, key, text, spans,
                                      pair(second={'category': 'experience'})), text)
    assert len(merged) == 1 and merged[0]['category'] == 'reference'
    merged = coalesced(service, key,
                       scripted_units(service, key, text, spans,
                                      pair(first={'category': 'experience'},
                                           second={'category': 'reference'})), text)
    assert len(merged) == 1 and merged[0]['category'] == 'reference'


@pytest.mark.parametrize('first,second', [
    ({}, {'continues_previous': False}),
    ({}, {'continues_previous': 'true'}),
    ({}, {'project_hint': ''}),
    ({}, {'project_hint': 'dsh'}),
    ({}, {'evidence_quote': ''}),
    ({}, {'evidence_quote': '这句不在原文里'}),
    ({'evidence_quote': ''}, {}),
    ({}, {'disposition': 'review', 'disposition_reason': '无法确定'}),
    ({}, {'disposition': 'set_aside', 'disposition_reason': '没有长期价值'}),
    ({'disposition': 'review', 'disposition_reason': '无法确定'}, {}),
], ids=['no-continuation', 'string-true', 'empty-hint', 'other-hint', 'no-current-quote',
        'unlocatable-quote', 'no-previous-quote', 'review', 'set-aside', 'previous-not-keep'])
def test_unsafe_pairs_are_never_merged(service, request, first, second):
    source = archive_lines(service, 'coalesce-reject-' + request.node.callspec.id, LINES)
    key, text, spans = prepared(service, source)
    units = scripted_units(service, key, text, spans, pair(first=first, second=second))
    merged = coalesced(service, key, units, text)
    assert len(merged) == 2
    assert [unit['text'] for unit in merged] == [unit['text'] for unit in units]


def test_a_current_unit_with_another_project_anchor_blocks_the_merge(service):
    lines = ('Evo 演示项目：先明确验收条件。', 'DSH 演示项目的导出继续细化。')
    source = archive_lines(service, 'coalesce-other-anchor', lines)
    key, text, spans = prepared(service, source)
    units = scripted_units(service, key, text, spans,
                           pair(second={'evidence_quote': 'DSH 演示项目的导出继续细化'}))
    assert len(coalesced(service, key, units, text)) == 2


def test_a_previous_group_naming_two_projects_blocks_the_merge(service):
    lines = ('Evo 演示项目与 DSH 演示项目都要先明确验收条件。', '继续细化：补充边界情况。')
    source = archive_lines(service, 'coalesce-two-anchors', lines)
    key, text, spans = prepared(service, source)
    units = scripted_units(service, key, text, spans, pair())
    assert len(coalesced(service, key, units, text)) == 2


def test_a_continuation_is_never_inherited_across_a_chunk_boundary(service):
    source = archive_lines(service, 'coalesce-chunk', LINES)
    key, text, spans = prepared(service, source)

    def answer(prompt):
        quote = 'Evo 演示项目' if '分段 1/' in prompt else CONT
        return json.dumps({'units': [base_unit(1, 1, project_hint='evo', evidence_quote=quote,
                                               continues_previous=True,
                                               category='task_requirement')]}, ensure_ascii=False)

    units = segment(text, answer, records=spans, size=25)
    assert len(units) == 2, 'the fixture must produce two chunks'
    assert [unit['continues_previous'] for unit in units] == [False, False], \
        'the first unit of every chunk is forced false'
    assert len(coalesced(service, key, units, text)) == 2


def test_overlapping_or_gapped_input_is_rejected_instead_of_being_swallowed(service):
    source = archive_lines(service, 'coalesce-coverage', LINES)
    key, text, spans = prepared(service, source)
    units = scripted_units(service, key, text, spans, pair())
    overlapping = [dict(units[0]), dict(units[1])]
    overlapping[1]['source_start'] = overlapping[0]['source_end'] - 3
    with pytest.raises(topic_segmentation.SegmentationError) as info:
        coalesced(service, key, overlapping, text)
    assert info.value.code == 'coverage_overlap'
    gapped = [dict(units[0]), dict(units[1])]
    gapped[1]['source_start'] = gapped[0]['source_end'] + 3
    with pytest.raises(topic_segmentation.SegmentationError) as info:
        coalesced(service, key, gapped, text)
    assert info.value.code == 'coverage_gap'
    # A genuine pair still merges, so the guard never blocks a valid continuation.
    assert len(coalesced(service, key, units, text)) == 1


def test_a_human_decision_on_the_same_source_range_blocks_the_merge(service):
    source = archive_lines(service, 'coalesce-manual', LINES)
    key, text, spans = prepared(service, source)
    task = dispatch(service, 'POST', 'organization/tasks', {'items': [{'key': key}]})['items'][0]
    units = scripted_units(service, key, text, spans, pair())
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute(
            'INSERT INTO organization_units(task_id,ordinal,digest,title,text,cleaned_text,source_start,'
            "source_end,category,role,disposition,disposition_reason,project_hint,project,decision,reason,"
            'evidence_quote,extraction_stage,created_at,updated_at) '
            "VALUES(?,99,'manual-digest','人工确认',?,?,?,?,'task_requirement','user','keep','','evo','evo',"
            "'manual','人工确认','Evo 演示项目','pending',?,?)",
            (task['id'], units[0]['text'], units[0]['text'], units[0]['source_start'],
             units[0]['source_end'], _now_iso(), _now_iso()))
    assert len(coalesced(service, key, units, text)) == 2


# ------------------------------------------------- worker boundary, end to end

class ScriptedSegments(UnitProvider):
    """Real contracts; segmentation answers the scripted units for every chunk."""

    def __init__(self, shapes):
        super().__init__(projects={'evo', 'dsh'})
        self.shapes = shapes

    def segment(self, prompt):
        return json.dumps({'units': [dict(shape) for shape in self.shapes]}, ensure_ascii=False)


def enqueue(service, key):
    return dispatch(service, 'POST', 'organization/tasks', {'items': [{'key': key}]})['items'][0]


def stored_units(service, task_id):
    return dispatch(service, 'GET', 'organization/units', {'task_id': task_id})['items']


def test_worker_stores_one_merged_unit_with_real_roles_and_exact_coverage(service, monkeypatch):
    source = archive_lines(service, 'coalesce-worker', LINES)
    key, text, spans = prepared(service, source)
    task = enqueue(service, key)
    tasks = run_worker(service, monkeypatch, ScriptedSegments(pair()))
    task = next(item for item in tasks if item['id'] == task['id'])
    units = stored_units(service, task['id'])
    assert len(units) == 1, 'the same-task continuation must be one stored unit'
    unit = units[0]
    snapshot = dispatch(service, 'GET', 'organization/detail', {'task_id': task['id']})['source_text']
    assert snapshot[unit['source_start']:unit['source_end']] == unit['text']
    assert 'Evo 演示项目' in unit['text'] and CONT in unit['text']
    assert unit['role'] == 'user' and unit['project'] == 'evo' and unit['decision'] == 'auto'


def test_worker_assign_saves_the_real_quote_after_a_whitespace_only_difference(service, monkeypatch):
    source = archive_lines(service, 'quote-nbsp', ('Evo 演示项目：导出必须保留来源版本号。',))
    key, text, spans = prepared(service, source)
    task = enqueue(service, key)
    shape = base_unit(1, 1, title='导出要求', cleaned_summary='导出必须保留来源版本号。',
                      category='task_requirement', project_hint='evo',
                      evidence_quote='Evo\u00a0演示项目：导出必须保留来源版本号。')
    run_worker(service, monkeypatch, ScriptedSegments([shape]))
    units = stored_units(service, task['id'])
    assert len(units) == 1
    unit = units[0]
    assert unit['evidence_quote'] == 'Evo 演示项目：导出必须保留来源版本号。'
    assert '\u00a0' not in unit['evidence_quote'], 'the saved quote is the real source substring'
    snapshot = dispatch(service, 'GET', 'organization/detail', {'task_id': task['id']})['source_text']
    assert snapshot[unit['source_start']:unit['source_end']] == unit['text']
    assert unit['project'] == 'evo' and unit['decision'] == 'auto'


def organization_memory_count(service):
    return service.store._connection().execute(
        "SELECT count(*) FROM context_items WHERE identity_key LIKE 'organization:%'").fetchone()[0]


def test_worker_assign_keeps_an_unlocatable_quote_as_review_without_fake_evidence(service, monkeypatch):
    from evolvmem.auto_organization import OrganizationWorker, _task_row
    source = archive_lines(service, 'quote-rewrite', ('Evo 演示项目：导出必须保留来源版本号。',))
    key, text, spans = prepared(service, source)
    task = enqueue(service, key)
    raw = 'Evo 演示项目：导出必须保留校验和。'
    shape = base_unit(1, 1, title='导出要求', cleaned_summary='导出必须保留校验和。',
                      category='task_requirement', project_hint='evo', evidence_quote=raw)
    run_worker(service, monkeypatch, ScriptedSegments([shape]))
    units = stored_units(service, task['id'])
    assert len(units) == 1
    unit = units[0]
    assert unit['decision'] == 'review' and not unit['project']
    assert unit['evidence_quote'] == raw, 'the model quote is kept, never cleared to bypass review'
    snapshot = dispatch(service, 'GET', 'organization/detail', {'task_id': task['id']})['source_text']
    assert snapshot[unit['source_start']:unit['source_end']] == unit['text']
    # A second automatic pass must not turn missing evidence into an automatic pass.
    memory_before = organization_memory_count(service)
    OrganizationWorker(service.config)._assign(service, _task_row(service, task['id']))
    again = stored_units(service, task['id'])[0]
    assert again['decision'] == 'review' and not again['project']
    assert again['evidence_quote'] == raw
    assert again['item_id'] is None, 'an unresolved unit is never written to history'
    assert organization_memory_count(service) == memory_before


def test_decide_accepts_a_whitespace_variant_and_rejects_a_rewrite(service):
    from evolvmem.auto_organization import OrganizationWorker
    worker = OrganizationWorker(service.config)
    policy = service.knowledge().rules.read()
    names = {'evo', 'dsh'}
    text = 'evo\u00a0演示项目与 dsh 演示项目都要求保留来源版本。'
    project, reason, decision = worker._decide(
        service, {'text': text, 'evidence_quote': 'evo 演示项目与'}, 'dsh', policy, names)
    assert decision == 'auto' and project == 'evo', (project, reason)
    project, reason, decision = worker._decide(
        service, {'text': text, 'evidence_quote': 'evo 演示项目均'}, 'dsh', policy, names)
    assert decision == 'review' and not project
