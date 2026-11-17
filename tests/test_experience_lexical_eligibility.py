"""Real SQLite regressions for the extra experience lexical channel.

Uses the existing real experience/evidence fixture. Organization ownership is
then attached as synthetic persisted metadata, without mocking eligibility or
retrieval. No embedding model is loaded.
"""
import pytest

from tests.test_experience_service import experiences, case, proof
from evolvmem.context_models import ContextContentType, ContextSearchRequest
from evolvmem.memory_eligibility import eligible


QUERY = '列表打开缓慢'


def _derived_case(experiences, *, independent=False):
    saved = experiences.record(case(), evidence=proof(experiences))
    store = experiences.store
    conn = store._connection()
    with store.transaction():
        task_id = conn.execute(
            "INSERT INTO organization_tasks(source_key,source_revision,rule_revision,status,created_at,updated_at) "
            "VALUES('lexical-fixture:source','source-v1','rule-v1','complete',datetime('now'),datetime('now'))"
        ).lastrowid
        conn.execute(
            "INSERT INTO organization_units(task_id,ordinal,digest,title,text,source_start,source_end,"
            "project,projects,knowledge_scope,decision,disposition,item_id,created_at,updated_at) "
            "VALUES(?,0,'lexical-unit','fixture','fixture',0,1,'demo','[\"demo\"]','project','manual','keep',?,"
            "datetime('now'),datetime('now'))", (task_id, saved['id']))
        conn.execute(
            "INSERT INTO unit_derivations(unit_task_id,unit_digest,item_id,kind,project,created_at) "
            "VALUES(?,'lexical-unit',?,'experience','demo',datetime('now'))", (task_id, saved['id']))
        if not independent:
            # The one persisted evidence source is supplied by this organization
            # unit. With independent=True the verified tool source stays intact.
            conn.execute(
                "UPDATE context_sources SET source_kind='organization',source_ref='lexical-fixture:source#unit' "
                "WHERE item_id=?", (saved['id'],))
    assert eligible(store, saved['id'])
    return saved['id'], task_id


def _recall(experiences, mode):
    return experiences.recall(project='demo', query=QUERY,
        constraints={'data_size': 'large'}, mode=mode)


def _withdraw(experiences, task_id, reason):
    with experiences.store.transaction():
        conn = experiences.store._connection()
        if reason == 'task_superseded':
            conn.execute("UPDATE organization_tasks SET status='superseded' WHERE id=?", (task_id,))
        elif reason == 'unit_review':
            conn.execute("UPDATE organization_units SET decision='review' WHERE task_id=?", (task_id,))
        else:
            conn.execute("UPDATE organization_units SET disposition='set_aside' WHERE task_id=?", (task_id,))


@pytest.mark.parametrize('mode', ['hook', 'full'])
@pytest.mark.parametrize('reason', ['task_superseded', 'unit_review', 'unit_set_aside'])
def test_withdrawn_unit_cannot_reenter_through_extra_lexical_channel(experiences, mode, reason):
    item_id, task_id = _derived_case(experiences)
    assert [r['id'] for r in _recall(experiences, mode)['results']] == [item_id]
    _withdraw(experiences, task_id, reason)
    assert not eligible(experiences.store, item_id)
    # Prove the original semantic path already rejects this actual stored item.
    request = ContextSearchRequest(query=QUERY, project='demo', cross_project=True, top_k=20,
        content_types=(ContextContentType.EXPERIENCE, ContextContentType.PLAYBOOK))
    assert item_id not in {r.id for r in experiences.core.retriever.search(request, eligible_ids=(item_id,), allow_rerank=False)}
    before = experiences.store._connection().total_changes
    actual = _recall(experiences, mode)
    assert experiences.store._connection().total_changes == before, 'recall must remain read-only'
    assert actual == {'results': [], 'used_chars': 0}


@pytest.mark.parametrize('mode', ['hook', 'full'])
def test_independent_verified_source_keeps_case_after_unit_withdrawal(experiences, mode):
    item_id, task_id = _derived_case(experiences, independent=True)
    expected = _recall(experiences, mode)
    assert [r['id'] for r in expected['results']] == [item_id]
    _withdraw(experiences, task_id, 'task_superseded')
    assert eligible(experiences.store, item_id)
    assert _recall(experiences, mode) == expected
