"""Unrelated uncertain criteria cannot erase independently proved ownership.

Real SQLite/source/evidence/CAS paths; only the external model is replaced.
"""
import json

import pytest

from evolvmem import auto_organization as org, organization_guidance as guidance
from evolvmem import organization_semantic_review as semantic, ownership_reasoning as reasoning
from tests.test_auto_organization import base_service, service
from tests.test_primary_ownership import prepare


TEXT = 'evo Web控制台增加项目归属审核。'
FOLLOW = '随后增加分页和来源上下文，修复映射问题。'


class ScopeProvider:
    def __init__(self, *, direct=('evo',), role='owner', verdicts=None, mutate=None):
        self.direct = direct
        self.role = role
        self.verdicts = verdicts or {}
        self.mutate = mutate
        self.calls = []

    def __call__(self, prompt, *args, **kwargs):
        target = json.loads(prompt.split('目标范围：', 1)[1].split('\n', 1)[0])
        rows = json.loads(prompt.split('待判断资料：', 1)[1])
        self.calls.append(target)
        if target and self.mutate:
            mutate, self.mutate = self.mutate, None
            mutate()
        verdict = self.verdicts.get(tuple(target['projects']), 'uncertain') if target else 'include'
        items = []
        for row in rows:
            text = row['text']
            previous = row['context'].get('same_source', {}).get('before', '')
            owner_quote = TEXT if TEXT in previous else text
            projects = target['projects'] if target and verdict == 'include' else self.direct if not target else ()
            items.append({'id': row['id'], 'decision': verdict, 'reason': '按当前任务的实际修改对象核对范围。',
                          'quote': text, 'chain': {'change': text},
                          'targets': [{'project': p, 'role': self.role, 'quote': owner_quote} for p in projects],
                          'knowledge_scope': 'project'})
        return json.dumps({'items': items}, ensure_ascii=False)


def rule(service, projects=('dsh',), *, negative=False, scope='project'):
    return guidance.record(service, guidance='只核对目标项目本身的业务修改。', scope='future',
                           project=projects[0] if projects else '', projects=list(projects),
                           knowledge_scope=scope, condition='目标项目本身的修改',
                           matching_mode='semantic', negative=negative)


def setup(service, monkeypatch, texts=None, **provider_options):
    worker, task, _, archive = prepare(service, monkeypatch, texts or [TEXT])
    provider = ScopeProvider(**provider_options)
    monkeypatch.setattr(org, '_load_llm', lambda: ({}, provider))
    return worker, task, provider, archive


def saved(service, task, ordinal=0):
    return dict(service.store._connection().execute(
        'SELECT * FROM organization_units WHERE task_id=? AND ordinal=?', (task['id'], ordinal)).fetchone())


def test_other_project_uncertainty_does_not_erase_same_task_source_proof(service, monkeypatch):
    rule(service)
    worker, task, _, _ = setup(service, monkeypatch, [TEXT, FOLLOW])
    worker._assign(service, task, materialize=False)
    unit = saved(service, task, 1)
    assert (unit['decision'], unit['project'], json.loads(unit['projects'])) == ('auto', 'evo', ['evo'])
    analysis = json.loads(unit['ownership_analysis'])
    assert analysis['targets'][0]['quote'] == TEXT
    assert analysis['chain']['change'] == '用户：' + FOLLOW
    assert unit['item_id'] is None


@pytest.mark.parametrize('projects,scope,negative', [
    (('evo',), 'project', False),
    (('evo', 'dsh'), 'project', False),
    ((), 'global', False),
    (('dsh',), 'project', True),
])
def test_overlapping_global_and_negative_uncertainty_keep_review_and_direct_diagnostics(
        service, monkeypatch, projects, scope, negative):
    rule(service, projects, scope=scope, negative=negative)
    worker, task, _, _ = setup(service, monkeypatch)
    worker._assign(service, task, materialize=False)
    unit = saved(service, task)
    assert (unit['decision'], unit['project']) == ('review', '')
    analysis = json.loads(unit['ownership_analysis'])
    assert (analysis['decision'], analysis['projects']) == ('include', ['evo'])
    assert analysis['evidence_quote'] == '用户：' + TEXT


def test_no_proved_direct_project_cannot_use_a_project_hint_to_skip_uncertainty(service, monkeypatch):
    rule(service)
    worker, task, _, _ = setup(service, monkeypatch, direct=())
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_units SET project_hint='evo' WHERE task_id=?", (task['id'],))
    worker._assign(service, task, materialize=False)
    assert (saved(service, task)['decision'], saved(service, task)['project']) == ('review', '')


@pytest.mark.parametrize('corruption', ['quote', 'chain', 'targets', 'projects', 'version', 'stamp'])
def test_tampered_cached_analysis_cannot_authorize_disjoint_bypass(service, monkeypatch, corruption):
    rule(service)
    worker, task, provider, _ = setup(service, monkeypatch)
    unit = saved(service, task)
    analysis = reasoning.analyze(service, [unit])[semantic._key(unit)]
    if corruption == 'quote': analysis['evidence_quote'] = '不存在的原话'
    elif corruption == 'chain': analysis['chain'] = {'change': '不存在的修改'}
    elif corruption == 'targets': analysis['targets'] = [{'project': 'evo', 'role': 'related', 'quote': TEXT}]
    elif corruption == 'projects': analysis['projects'] = ['shop']
    elif corruption == 'version': analysis['version'] = 'old'
    else: analysis['input_stamp'] = 'stale'
    # Call the scoped classifier directly to ensure invalid evidence is not
    # silently replaced with a second model response by _assign's cache refresh.
    result = semantic.classify_guidance(service, unit, direct_analysis=analysis)
    assert result['status'] == 'review'


def test_shared_applicability_requires_disjoint_from_every_selected_project(service, monkeypatch):
    rule(service, ('shop',))
    worker, task, _, _ = setup(service, monkeypatch, direct=('evo', 'dsh'), role='applicable')
    worker._assign(service, task, materialize=False)
    assert json.loads(saved(service, task)['projects']) == ['dsh', 'evo']
    rule(service, ('dsh',))
    worker._assign(service, task, materialize=False)
    assert saved(service, task)['decision'] == 'review'


def test_actual_negative_match_and_conflicting_positive_rules_still_review(service, monkeypatch):
    rule(service, ('dsh',), negative=True)
    worker, task, _, _ = setup(service, monkeypatch, verdicts={('dsh',): 'include'})
    worker._assign(service, task, materialize=False)
    assert saved(service, task)['decision'] == 'review'
    with service.store.transaction():
        service.store._connection().execute('UPDATE organization_guidance SET negative=0')
    rule(service, ('evo',))
    provider = ScopeProvider(verdicts={('dsh',): 'include', ('evo',): 'include'})
    monkeypatch.setattr(org, '_load_llm', lambda: ({}, provider))
    worker._assign(service, task, materialize=False)
    assert saved(service, task)['decision'] == 'review'


def test_rule_edit_during_uncertain_judgment_keeps_review(service, monkeypatch):
    current = rule(service)
    def mutate(): guidance.set_enabled(service, current['id'], False, expected_revision=1)
    worker, task, _, _ = setup(service, monkeypatch, mutate=mutate)
    worker._assign(service, task, materialize=False)
    assert saved(service, task)['decision'] == 'review'
    assert '依据已变化' in saved(service, task)['reason']


def test_source_change_during_uncertain_judgment_cannot_apply_direct_owner(service, monkeypatch):
    rule(service)
    worker, task, provider, archive = setup(service, monkeypatch)
    def mutate():
        with service.store.transaction():
            service.store._connection().execute('UPDATE knowledge_cleaning_reviews SET revision=revision+1 WHERE source_key=?', (f'archive:{archive.id}',))
    provider.mutate = mutate
    with pytest.raises(ValueError, match='revision_conflict'):
        worker._assign(service, task, materialize=False)
    assert saved(service, task)['decision'] == 'review'
    assert saved(service, task)['project'] == ''


def test_manual_exclusion_of_direct_project_survives_unrelated_rule_uncertainty(service, monkeypatch):
    from evolvmem import organization_exclusions as exclusions
    rule(service)
    worker, task, _, _ = setup(service, monkeypatch)
    unit = {**saved(service, task), 'source_key': task['source_key'], 'source_revision': task['source_revision'], 'rule_revision': task['rule_revision']}
    with service.store.transaction():
        exclusions.write_decision(service, unit, {'projects': ['evo'], 'knowledge_scope': 'project'}, 'exclude')
    worker._assign(service, task, materialize=False)
    assert saved(service, task)['decision'] == 'review'
    assert '已确认这条不归入' in saved(service, task)['reason']


def test_manual_unit_is_untouched_even_with_disjoint_direct_cache(service, monkeypatch):
    rule(service)
    worker, task, _, _ = setup(service, monkeypatch)
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_units SET decision='manual',project='dsh',projects='[\"dsh\"]',revision=7 WHERE task_id=?", (task['id'],))
    before = saved(service, task)
    worker._assign(service, task, materialize=False)
    assert saved(service, task) == before


@pytest.mark.parametrize('change', ['unit_body', 'task_snapshot', 'prior_decisions'])
def test_current_labels_cannot_replace_frozen_source_or_prior_human_evidence(service, monkeypatch, change):
    rule(service)
    worker, task, _, _ = setup(service, monkeypatch)
    with service.store.transaction():
        conn = service.store._connection()
        if change == 'unit_body':
            conn.execute("UPDATE organization_units SET text='evo 新编造的修改' WHERE task_id=?", (task['id'],))
        elif change == 'task_snapshot':
            conn.execute("UPDATE organization_tasks SET source_snapshot='原冻结正文已经被替换' WHERE id=?", (task['id'],))
        else:
            conn.execute('UPDATE organization_units SET task_record=? WHERE task_id=?',
                         (json.dumps({'prior_decisions': [{'decision': 'manual', 'project': 'dsh'}]}), task['id']))
    unit = saved(service, task)
    analysis = reasoning.analyze(service, [unit])[semantic._key(unit)]
    result = semantic.classify_guidance(service, unit, direct_analysis=analysis)
    assert result['status'] == 'review'


def test_prior_human_decision_remains_a_review_before_guidance(service, monkeypatch):
    rule(service)
    worker, task, provider, _ = setup(service, monkeypatch)
    with service.store.transaction():
        service.store._connection().execute('UPDATE organization_units SET task_record=? WHERE task_id=?',
            (json.dumps({'prior_decisions': [{'decision': 'manual', 'project': 'dsh'}]}), task['id']))
    worker._assign(service, task, materialize=False)
    unit = saved(service, task)
    assert unit['decision'] == 'review' and unit['project'] == ''
    assert json.loads(unit['task_record'])['prior_decisions'][0]['project'] == 'dsh'
    assert provider.calls == []



def test_revalidation_preserves_current_predecessor_dependency_even_with_local_quotes(service, monkeypatch):
    from tests.test_cross_batch_context import craft, SESSION
    from tests.test_knowledge_cleaning import ready
    ready(service)
    worker = org.OrganizationWorker(service.config)
    task_ids = []
    for index, text in enumerate([TEXT, '继续处理 evo 的分页校验。']):
        archive = craft(service, session=SESSION, start=1+3*index, end=3+3*index, text=text)
        task = org.enqueue(service, [f'archive:{archive.id}'])['items'][0]
        row = org._task_row(service, task['id'])
        worker._store_units(service, row, [{'source_start': 0, 'source_end': len(row['source_snapshot']),
            'text': row['source_snapshot'], 'title': text, 'category': 'task_requirement',
            'project_hint': '', 'evidence_quote': text}])
        task_ids.append(task['id'])
        if index == 0:
            with service.store.transaction():
                conn = service.store._connection()
                conn.execute("UPDATE organization_units SET decision='manual',project='evo',projects='[\"evo\"]' WHERE task_id=?", (task['id'],))
                conn.execute("UPDATE organization_tasks SET status='completed',stage='done' WHERE id=?", (task['id'],))
    unit = saved(service, task)
    def provider(prompt, *args, **kwargs):
        row = json.loads(prompt.split('待判断资料：', 1)[1])[0]
        return json.dumps({'items': [{'id': 0, 'decision': 'include', 'reason': '继续同一任务的分页修改。',
            'quote': row['text'], 'chain': {'change': row['text']}, 'continues_context': True,
            'targets': [{'project': 'evo', 'role': 'owner', 'quote': row['text']}]}]}, ensure_ascii=False)
    monkeypatch.setattr(org, '_load_llm', lambda: ({}, provider))
    analysis = reasoning.analyze(service, [unit])[semantic._key(unit)]
    assert analysis['uses_predecessor'] is True
    checked = reasoning.current_direct_analysis(service, unit, analysis)
    assert checked['uses_predecessor'] is True
    with service.store.transaction():
        service.store._connection().execute('UPDATE organization_units SET revision=revision+1 WHERE task_id=?', (task_ids[0],))
    assert reasoning.current_direct_analysis(service, unit, analysis) == {}



def test_disjoint_uncertainty_does_not_skip_a_later_positive_rule(service, monkeypatch):
    rule(service, ('dsh',))
    matching = rule(service, ('evo',))
    worker, task, _, _ = setup(service, monkeypatch, verdicts={('evo',): 'include'})
    worker._assign(service, task, materialize=False)
    unit = saved(service, task)
    assert (unit['decision'], unit['project'], unit['applied_guidance_id']) == ('auto', 'evo', matching['id'])


def test_global_direct_analysis_never_proves_project_scope_disjoint(service, monkeypatch):
    rule(service)
    worker, task, _, _ = setup(service, monkeypatch, ['个人通用的习惯：修改后核对实际结果。'])
    base = ScopeProvider()
    def provider(prompt, *args, **kwargs):
        result = json.loads(base(prompt, *args, **kwargs))
        target = json.loads(prompt.split('目标范围：', 1)[1].split('\n', 1)[0])
        if target is None:
            for row in result['items']:
                row.update(targets=[], knowledge_scope='global', general_quote=row['quote'])
        return json.dumps(result, ensure_ascii=False)
    monkeypatch.setattr(org, '_load_llm', lambda: ({}, provider))
    worker._assign(service, task, materialize=False)
    assert saved(service, task)['decision'] == 'review'
    assert saved(service, task)['project'] == ''



def test_history_only_review_retains_valid_direct_analysis_without_assigning(service, monkeypatch):
    rule(service, ('evo',))
    worker, task, _, _ = setup(service, monkeypatch)
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_units SET disposition='history_only' WHERE task_id=?", (task['id'],))
    worker._assign(service, task, materialize=False)
    unit = saved(service, task)
    assert (unit['decision'], unit['project'], unit['disposition']) == ('review', '', 'review')
    assert json.loads(unit['ownership_analysis'])['projects'] == ['evo']
