"""A bounded multi-level topic key must survive extraction instead of being
silently dropped as invalid metadata.

Real review evidence (synthetic fixture, no credentials or business data): the
model emitted a legal hierarchical topic key

    project:sampleatlas:constraint:report_export:row_order

for a correct user constraint ("导出时还必须保留原始行顺序，不允许按金额重新排序。"), the
independent answer review judged it ``supported``, and the task still finished
``done`` with no knowledge entry and no QA. The cause is not the project, the
evidence or the confidence: ``extraction_policy._STABLE_KEY_RE`` demanded
exactly four segments, ``evaluate_candidate`` returned
``PolicyDecision(accepted=False, reason='metadata')`` and
``session_extraction.prepare_extraction`` silently filtered the candidate out.

These tests pin the smallest compatible contract: the historical four-segment
key and every existing character/length/namespace rule stay as they were, a
*bounded* multi-level topic tail is accepted, and a key is never rewritten
after the answer-support verdict was recorded.
"""
import json

import pytest

from evolvmem import answer_support, extraction_policy as policy, kimi_hooks, memory_recall, qa_memory
from evolvmem.auto_extractor import CandidateMemory
from evolvmem.context_models import ContextMode
from evolvmem.session_extraction import prepare_extraction
from tests.test_web_server import _make_service

PROJECT = 'sampleatlas'
USER_LINE = '继续报表导出任务，导出时还必须保留原始行顺序，不允许按金额重新排序。'
ANSWER = '报表导出必须保留原始行顺序，不允许按金额重新排序。'
QUOTE = '导出时还必须保留原始行顺序，不允许按金额重新排序。'
FIVE_SEGMENT_KEY = 'project:sampleatlas:constraint:report_export:row_order'
SUMMARY_VALUE = '报表导出任务新增行顺序约束，未执行操作。'

# The learning payload of the rejected real-model candidate, copied verbatim
# from the synthetic fixture shipped with this review.
LEARNING = {
    'category': 'task_requirement',
    'basis': 'explicit',
    'quote': QUOTE,
    'question': '报表导出对行顺序有什么要求？',
    'answer': ANSWER,
    'normalization': {'requirement': ANSWER, 'acceptance': [], 'questions': []},
    'trigger': '继续报表导出任务时',
    'rationale': '用户明确要求导出结果不得按金额重排，需保持原始行序。',
    'instruction': '导出报表时保持原始行顺序，禁止按金额排序。',
    'topic': '报表导出行顺序',
    'action': 'add',
}


def candidate(value=ANSWER, key='project:test:fact:item', **overrides):
    values = {
        'value': value,
        'attribute': 'constraint',
        'importance': 5.0,
        'confidence': 0.9,
        'tier': 'normal',
    }
    values.update(overrides)
    return CandidateMemory(key=key, **values)


def extraction_reply(key=FIVE_SEGMENT_KEY, *, value=ANSWER, attribute='constraint',
                     summary=SUMMARY_VALUE, tags=('报表导出', '排序', '约束'),
                     importance=8, confidence=0.9, learning=None):
    """One provider reply shaped exactly like the rejected real-model output."""
    return json.dumps({'memories': [
        {'key': 'SESSION_SUMMARY', 'value': summary},
        {'key': key, 'value': value, 'attribute': attribute, 'confidence': confidence,
         'importance': importance, 'tier': 'pinned', 'tags': list(tags),
         'learning': dict(LEARNING if learning is None else learning)},
    ]}, ensure_ascii=False)


def verdicts(*entries):
    return json.dumps([dict(entry) for entry in entries], ensure_ascii=False)


def reviewed_reply(_prompt):
    """The independent reviewer agrees with the candidate's own quote."""
    return verdicts({'id': 1, 'verdict': 'supported', 'reason': '与用户原话一致', 'quote': QUOTE})


@pytest.fixture
def service(test_config):
    svc = _make_service(test_config, mode=ContextMode.SHADOW)
    svc.knowledge().save_project({'project': PROJECT})
    yield svc
    svc.close()


@pytest.fixture
def messages():
    return [{'role': 'user', 'content': USER_LINE}]


def install_model(monkeypatch, reply, review=reviewed_reply):
    """Route both real provider callbacks: extraction and the answer review."""
    def model(prompt, *args, **kwargs):
        return review(prompt) if answer_support.REVIEW_PROMPT in prompt else reply
    monkeypatch.setattr(kimi_hooks, '_load_llm_config', lambda **kwargs: object())
    monkeypatch.setattr(kimi_hooks, '_call_llm_with_retry', model)


# ---- the policy contract: bounded multi-level topics -------------------------

@pytest.mark.parametrize('key', [
    'project:test:fact:item',                                   # historical four segments
    FIVE_SEGMENT_KEY,                                           # the rejected real shape
    'project:sampleatlas:constraint:report_export:row_order:detail',
    'project:sampleatlas:constraint:report_export:row_order:detail:extra',
    'project:a:b:c:d:e:f:g',                                    # the bounded maximum (8)
    'user:preference:report_export:row_order',
])
def test_a_bounded_multi_level_topic_key_stays_valid_metadata(key):
    """Catch: a legal hierarchical topic must not read as invalid metadata."""
    assert policy.evaluate_candidate(candidate(key=key)) == policy.PolicyDecision(True)


@pytest.mark.parametrize('key,reason', [
    ('not-a-stable-key', 'metadata'),                           # one segment
    ('project:sampleatlas:fact', 'metadata'),                   # three segments
    ('project:sampleatlas:constraint:a:b:c:d:e:f', 'metadata'),  # one over the bound
    ('project:sampleatlas:fact::item', 'metadata'),             # empty segment
    ('project:sampleatlas:fact:item:', 'metadata'),             # trailing empty segment
    ('project:sampleatlas:fact:item name', 'metadata'),         # unsupported separator
    ('project:sampleatlas:fact:' + 'x' * 200, 'metadata'),      # over the key length bound
    ('project:sampleatlas:fact:eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig', 'sensitive'),
])
def test_the_key_shape_and_security_negatives_are_unchanged(key, reason):
    """Catch: widening the topic depth must not open malformed or risky keys."""
    assert policy.evaluate_candidate(candidate(key=key)) == policy.PolicyDecision(False, reason)


# ---- prepare_extraction: the reproduced silent drop --------------------------

def test_prepare_extraction_keeps_the_five_segment_topic_key(service, monkeypatch, messages):
    """Catch: the reproduced defect — a correct five-segment candidate silently vanished."""
    install_model(monkeypatch, extraction_reply())
    request = prepare_extraction(service.config, PROJECT, 'five-segment', messages, object())

    keys = [item.key for item in request.candidates]
    assert keys == [FIVE_SEGMENT_KEY], keys
    assert request.candidates[0].value == ANSWER
    assert request.candidates[0].attribute == 'constraint'
    # The session summary keeps its own item and never becomes an atomic candidate.
    assert 'SESSION_SUMMARY' not in keys
    assert request.summary.key.startswith(f'project:{PROJECT}:progress:log:')


def test_prepare_extraction_still_drops_a_wrong_namespace_key(service, monkeypatch, messages):
    """Catch: the bounded topic bound must not widen which namespaces are addressed."""
    install_model(monkeypatch, extraction_reply(
        key='other:sampleatlas:constraint:report_export:row_order'))
    request = prepare_extraction(service.config, PROJECT, 'wrong-namespace', messages, object())

    assert request.candidates == ()


@pytest.mark.parametrize('key', [
    'project:sampleatlas:constraint:report_export:',      # empty trailing segment
    'project:sampleatlas:constraint::row_order',          # empty middle segment
    'project:sampleatlas:constraint:' + 'y' * 200,        # overlong key
])
def test_prepare_extraction_still_drops_malformed_keys(service, monkeypatch, messages, key):
    """Catch: a malformed key must not reach knowledge because deeper topics now pass."""
    install_model(monkeypatch, extraction_reply(key=key))
    request = prepare_extraction(service.config, PROJECT, 'malformed', messages, object())

    assert request.candidates == ()


def test_prepare_extraction_keeps_a_user_scope_multi_level_key(service, monkeypatch, messages):
    """Catch: the bounded topic must work for the user namespace too."""
    key = 'user:preference:report_export:row_order'
    install_model(monkeypatch, extraction_reply(key=key, attribute='preference'))
    request = prepare_extraction(service.config, PROJECT, 'user-scope', messages, object())

    assert [item.key for item in request.candidates] == [key]


# ---- the full path: persist, QA listing, retrieval, replay -------------------

def persist_once(service, messages, session='five-segment'):
    request = prepare_extraction(service.config, PROJECT, session, messages, object())
    return request, service.persist_legacy_extraction(request, source_messages=messages)


def test_the_five_segment_key_becomes_active_qa_knowledge(service, monkeypatch, messages):
    """Catch: the bounded key must reach persistence as current, source-backed knowledge."""
    install_model(monkeypatch, extraction_reply())
    request, result = persist_once(service, messages)

    written = [item for item in result.candidates if item.context_id is not None]
    assert len(written) == 1, result
    row = service.knowledge().detail(written[0].context_id)
    # The persisted row keeps the five-segment topic and only gains the
    # per-write ``:learn:<digest>`` suffix every extracted item gets.
    assert row['identity_key'].split(':learn:')[0] == FIVE_SEGMENT_KEY
    assert row['status'] == 'active', row['ingestion_reason']
    assert row['body'] == ANSWER
    assert row['learning']['answer_support']['verdict'] == 'supported'

    listing = qa_memory.list_items(service, {'project': PROJECT})
    assert listing['total'] == 1
    entry = listing['items'][0]
    assert entry['id'] == written[0].context_id
    assert entry['question'] == LEARNING['question']
    assert entry['answer'] == ANSWER
    assert entry['status'] == 'active'
    assert entry['effective'] is True

    recalled = memory_recall.recall(
        service, {'project': PROJECT, 'query': '报表导出行顺序要求', 'kind': 'experience'})
    assert [item['id'] for item in recalled['qa']] == [written[0].context_id]


def test_replaying_the_same_extraction_adds_no_duplicate(service, monkeypatch, messages):
    """Catch: the same source replayed must not create a second knowledge/QA entry."""
    install_model(monkeypatch, extraction_reply())
    _, first = persist_once(service, messages)
    first_id = first.candidates[0].context_id
    assert first_id is not None

    _, replay = persist_once(service, messages)

    assert replay.candidates == (), '重放同一批次不得再写入'
    listing = qa_memory.list_items(service, {'project': PROJECT})
    assert listing['total'] == 1
    assert [item['id'] for item in listing['items']] == [first_id]


# ---- answer-support binding across the key path ------------------------------

def test_the_reviewed_verdict_stays_bound_to_the_written_five_segment_key(
        service, monkeypatch, messages):
    """Catch: normalization after the verdict must not invalidate a fresh ``supported``."""
    install_model(monkeypatch, extraction_reply())
    request = prepare_extraction(service.config, PROJECT, 'binding', messages, object())
    learning = request.candidates[0].learning

    binding = learning['answer_support']['binding']
    assert binding['key'] == FIVE_SEGMENT_KEY
    assert answer_support.check_binding(
        learning, messages, key=request.candidates[0].key.casefold(),
        category=learning.get('category', ''), trigger=learning.get('trigger', '')) == ''

    assert answer_support.check_binding(
        learning, messages, key='project:sampleatlas:constraint:report_export:other',
        category=learning.get('category', ''), trigger=learning.get('trigger', '')).endswith(
        '候选标识已变化，需要重新核对')


def test_an_upper_case_wrong_project_is_resolved_before_the_review(
        service, monkeypatch, messages):
    """Catch: a root-case variant left the project rewrite after the verdict was recorded."""
    install_model(monkeypatch, extraction_reply(
        key='PROJECT:wrong:constraint:report_export:row_order'))
    request = prepare_extraction(service.config, PROJECT, 'upper-root', messages, object())

    assert [item.key for item in request.candidates] == [FIVE_SEGMENT_KEY]
    support = request.candidates[0].learning['answer_support']
    assert support['binding']['key'] == FIVE_SEGMENT_KEY
    assert support['verdict'] == 'supported'
