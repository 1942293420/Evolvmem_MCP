"""Bounded one-shot coverage correction for whole-source segmentation.

Synthetic sources only. The provider is scripted; the numbered-record protocol,
validation, exact offsets, the whole-source coverage check and the worker
boundary are the real ones.
"""
import json
import re
import time

import pytest

from evolvmem import topic_segmentation
from evolvmem.topic_segmentation import SegmentationError, segment

NUMBERED = re.compile(r'^\[(\d+)\] (\w+)：(.*)$', re.M)
LINES = [
    '甲项目：先明确验收条件，再开始改界面。',
    '继续确认甲项目的验收范围与顺序。',
    '乙项目：导出必须保留来源版本号。',
]


def source_of(lines=None):
    return '\n'.join(lines or LINES)


def unit(start, end, **over):
    entry = {'start_id': start, 'end_id': end, 'title': '单元 %d-%d' % (start, end),
             'cleaned_summary': '覆盖 %d-%d 的摘要。' % (start, end), 'category': 'reference',
             'project_hint': '', 'evidence_quote': '', 'disposition': 'keep',
             'disposition_reason': ''}
    entry.update(over)
    return entry


def answer(*units):
    return json.dumps({'units': list(units)}, ensure_ascii=False)


CHUNK_MARKER = re.compile(r'分段 (\d+)/(\d+)')


def numbered_ids(prompt):
    return [int(position) for position, _role, _content in NUMBERED.findall(prompt)]


def chunk_index(prompt):
    match = CHUNK_MARKER.search(prompt)
    assert match, 'every request keeps its program chunk number'
    return int(match.group(1))


class Scripted:
    """Answers by call order; repeats the last answer once the script runs out."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        item = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def test_tail_gap_is_corrected_by_exactly_one_more_call():
    source = source_of()
    provider = Scripted([answer(unit(1, 2)), answer(unit(1, 2), unit(3, 3))])
    units = segment(source, provider)
    assert len(provider.calls) == 2, 'one original call plus exactly one correction'
    assert [(u['start_id'], u['end_id']) for u in units] == [(1, 2), (3, 3)]
    for item in units:
        assert source[item['source_start']:item['source_end']] == item['text']
    correction = provider.calls[1]
    assert 'coverage_gap' in correction, 'the validation error type must be stated'
    assert 'records 3-3 unprocessed' in correction, 'the failing position must be stated'
    assert sorted(numbered_ids(correction)) == [1, 2, 3], 'the whole chunk numbering must be sent again'
    assert 'start_id' in correction and 'end_id' in correction, 'the whole chunk must be re-emitted'


def test_middle_gap_and_overlap_are_each_corrected_once():
    source = source_of()
    gap = Scripted([answer(unit(1, 1), unit(3, 3)),
                    answer(unit(1, 1), unit(2, 2), unit(3, 3))])
    units = segment(source, gap)
    assert len(gap.calls) == 2 and 'coverage_gap' in gap.calls[1]
    assert source[units[1]['source_start']:units[1]['source_end']] == units[1]['text']

    overlap = Scripted([answer(unit(1, 2), unit(2, 3)),
                        answer(unit(1, 2), unit(3, 3))])
    units = segment(source, overlap)
    assert len(overlap.calls) == 2 and 'coverage_overlap' in overlap.calls[1]
    assert [(u['start_id'], u['end_id']) for u in units] == [(1, 2), (3, 3)]


def test_quote_not_found_is_corrected_once_from_the_legacy_body_protocol():
    source = 'AAAA甲项目要求。BBBB乙项目要求。'
    missing = json.dumps({'units': [{'title': '甲', 'body': 'AAAA甲项目要求。'},
                                    {'title': '乙', 'body': 'CCCC不存在的要求。'}]}, ensure_ascii=False)
    complete = json.dumps({'units': [{'title': '甲', 'body': 'AAAA甲项目要求。'},
                                     {'title': '乙', 'body': 'BBBB乙项目要求。'}]}, ensure_ascii=False)
    provider = Scripted([missing, complete])
    units = segment(source, provider)
    assert len(provider.calls) == 2
    assert 'quote_not_found' in provider.calls[1]
    assert ''.join(u['text'] for u in units) == source


def test_correction_never_copies_the_previous_raw_response():
    filler = 'Zq' * 4000
    first = answer(unit(1, 2, cleaned_summary='前情摘要。' + filler))
    assert len(first) > 8000
    provider = Scripted([first, answer(unit(1, 2), unit(3, 3))])
    segment(source_of(), provider)
    assert len(provider.calls) == 2
    assert filler[:120] not in provider.calls[1], 'the raw response must not be echoed back'


def test_persistent_gap_fails_after_exactly_two_calls_and_returns_nothing():
    provider = Scripted([answer(unit(1, 2))])
    with pytest.raises(SegmentationError) as info:
        segment(source_of(), provider)
    assert info.value.code == 'coverage_gap'
    assert info.value.detail == 'records 3-3 unprocessed'
    assert len(provider.calls) == 2, 'the second failure keeps the original error, it is not retried again'


def test_a_complete_chunk_is_never_asked_twice():
    provider = Scripted([answer(unit(1, 2), unit(3, 3))])
    units = segment(source_of(), provider)
    assert provider.calls and len(provider.calls) == 1
    assert len(units) == 2


def test_bad_response_and_provider_errors_are_not_retried_or_swallowed():
    bad = Scripted(['这不是 JSON'])
    with pytest.raises(SegmentationError) as info:
        segment(source_of(), bad)
    assert info.value.code == 'bad_response'
    assert len(bad.calls) == 1

    broken = Scripted([RuntimeError('provider connection reset')])
    with pytest.raises(RuntimeError, match='connection reset'):
        segment(source_of(), broken)
    assert len(broken.calls) == 1


def _six_line_source():
    return source_of(['TK%d 甲项目第%d条要求，必须完整覆盖。' % (i, i) for i in range(1, 7)])


def test_only_the_failing_chunk_is_retried_and_each_chunk_has_one_budget():
    line_chars = len('TK1 甲项目第1条要求，必须完整覆盖。')
    source = _six_line_source()
    size = line_chars * 2 + 1  # exactly two records per chunk, six records in three chunks
    provider = Scripted([answer(unit(1, 2)),          # chunk 1: complete
                         answer(unit(1, 1)),          # chunk 2: tail gap, corrected once
                         answer(unit(1, 2)),          # chunk 2 correction: complete
                         answer(unit(1, 2))])         # chunk 3: complete
    units = segment(source, provider, size=size)
    assert len(provider.calls) == 4, 'only the failing chunk is asked a second time'
    per_chunk = {}
    for prompt in provider.calls:
        per_chunk.setdefault(chunk_index(prompt), []).append(prompt)
    assert sorted(per_chunk) == [1, 2, 3]
    assert [len(per_chunk[index]) for index in (1, 2, 3)] == [1, 2, 1]
    assert 'coverage_gap' in per_chunk[2][1] and 'coverage_gap' not in per_chunk[1][0]
    assert [(u['start_id'], u['end_id']) for u in units] == [(1, 2)] * 3
    for item in units:
        assert source[item['source_start']:item['source_end']] == item['text']


def test_a_failing_first_chunk_stops_after_its_own_budget_without_looping():
    line_chars = len('TK1 甲项目第1条要求，必须完整覆盖。')
    source = _six_line_source()
    provider = Scripted([answer(unit(1, 1))])
    with pytest.raises(SegmentationError) as info:
        segment(source, provider, size=line_chars * 2 + 1)
    assert info.value.code == 'coverage_gap'
    assert len(provider.calls) == 2, 'a failing chunk stops the run after its one correction'


def test_corrected_units_keep_real_source_identity_offsets_and_evidence():
    from evolvmem.conversation import render
    messages = [{'role': 'user', 'content': '甲项目：先明确验收条件。'},
                {'role': 'assistant', 'content': '明白，先写验收条件，再开始改界面。'}]
    source = render(messages)
    spans = topic_segmentation.message_spans(messages)
    provider = Scripted([
        answer(unit(1, 1, project_hint='甲项目', category='task_requirement')),
        answer(unit(1, 1, project_hint='甲项目', category='task_requirement',
                    evidence_quote='甲项目：先明确验收条件。'),
               unit(2, 2, project_hint='', category='experience'))])
    units = segment(source, provider, records=spans)
    assert len(provider.calls) == 2
    assert 'records 2-2 unprocessed' in provider.calls[1]
    for item in units:
        assert source[item['source_start']:item['source_end']] == item['text']
    assert units[0]['project_hint'] == '甲项目' and units[0]['category'] == 'task_requirement'
    assert units[1]['category'] == 'experience'
    assert topic_segmentation.unit_evidence(units[0], spans)[0] == 'user'
    assert topic_segmentation.unit_evidence(units[1], spans)[0] == 'assistant'
    assert topic_segmentation.unit_messages(units[1], spans) == [
        {'role': 'assistant', 'content': '明白，先写验收条件，再开始改界面。'}]


# -- worker boundary: synthetic end-to-end runs --

from evolvmem.context_models import ContextMode  # noqa: E402
from evolvmem.knowledge_api import dispatch  # noqa: E402
from tests.test_web_server import _make_service  # noqa: E402
from tests.unit_model_fixture import (  # noqa: E402
    UnitProvider, many_topic_archive, run_worker)


@pytest.fixture
def service(test_config):
    service = _make_service(test_config, mode=ContextMode.SHADOW)
    for project in ('evo', 'shop'):
        service.knowledge().save_project({'project': project})
    service.knowledge().save_project({'project': 'dsh', 'display_name': 'DSH 演示项目'})
    yield service
    service.close()


def org(service, route='', body=None):
    return dispatch(service, 'GET' if body is None else 'POST', 'organization' + route, body)


class GapOnceProvider(UnitProvider):
    """Real contracts; the first segmentation answer drops the tail of the chunk."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.segment_calls = 0

    def segment(self, prompt):
        self.segment_calls += 1
        good = super().segment(prompt)
        if self.segment_calls == 1:
            data = json.loads(good)
            data['units'] = data['units'][:-1]
            return json.dumps(data, ensure_ascii=False)
        return good


def test_worker_end_to_end_corrects_one_coverage_gap_and_stores_the_whole_source(service, monkeypatch):
    source = many_topic_archive(service, session='coverage-correction')
    task = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]
    provider = GapOnceProvider(projects={'evo', 'dsh'})
    tasks = run_worker(service, monkeypatch, provider)
    task = next(item for item in tasks if item['id'] == task['id'])
    assert provider.segment_calls == 2, 'one original call plus exactly one correction'
    assert 'coverage_gap' in provider.calls[1]
    assert task['error_code'] != 'coverage_gap' and task['stage'] == 'done'
    assert task['status'] in ('review', 'completed')
    units = org(service, '/units', {'task_id': task['id']})['items']
    assert units, 'the corrected segmentation must be stored, never half-written'
    snapshot = org(service, '/detail', {'task_id': task['id']})['source_text']
    for item in units:
        assert snapshot[item['source_start']:item['source_end']] == item['text']


def test_worker_stop_between_calls_cancels_the_correction(service, monkeypatch):
    from evolvmem import kimi_hooks
    from evolvmem.auto_organization import OrganizationWorker

    source = many_topic_archive(service, session='coverage-stop')
    task = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    base = GapOnceProvider(projects={'evo', 'dsh'})
    segmentation_prompts = []

    def hook(prompt, *args, **kwargs):
        if '整理分段助手' in prompt:
            segmentation_prompts.append(prompt)
            worker._stop.set()  # cancellation arrives while the provider is answering
        return base(prompt, *args, **kwargs)

    monkeypatch.setattr(kimi_hooks, '_load_llm_config', lambda **kw: object())
    monkeypatch.setattr(kimi_hooks, '_call_llm_with_retry', hook)
    worker.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and worker._thread.is_alive():
            time.sleep(.05)
        assert not worker._thread.is_alive(), 'the worker must observe the stop instead of retrying'
    finally:
        worker.stop()
    assert len(segmentation_prompts) == 1, 'no correction call after the worker was told to stop'
    assert 'coverage_gap' not in segmentation_prompts[0]
