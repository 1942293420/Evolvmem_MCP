import json
import pytest
from tests.test_history_qa_memory import service
from evolvmem.legacy_models import LegacyAddRequest
from evolvmem.memory_eligibility import eligible


def old_rule(service, *, human=False, manual=False, role='assistant'):
    item = service.legacy_add(LegacyAddRequest(key='project:evo:constraint:legacy-rule',
        value='助手转述的旧约定不应自动成为用户的长期要求。', source_session='legacy-session'))
    with service.store.transaction():
        conn = service.store._connection()
        conn.execute('INSERT INTO learning_memories(item_id,category,payload) VALUES(?,?,?)',
            (item.context_id, 'project_convention', json.dumps({'basis': 'manual' if manual else 'inferred',
             'evidence': [{'role': role, 'quote': '助手转述的旧约定'}]})))
        if human:
            conn.execute("UPDATE context_project_resolutions SET decision_source='human',review_state='accepted' WHERE item_id=?", (item.context_id,))
    return item.context_id


def test_inferred_assistant_rule_cannot_reenter_recall_before_or_after_repair(service):
    from evolvmem.knowledge_provenance import repair_unconfirmed_rules
    ident = old_rule(service)
    assert not eligible(service.store, ident)
    preview = repair_unconfirmed_rules(service)
    assert preview['eligible_ids'] == [ident]
    assert service.store.get_item(ident).status.value == 'active'
    assert repair_unconfirmed_rules(service, apply=True)['updated_ids'] == [ident]
    assert service.store.get_item(ident).status.value == 'candidate'
    assert service.knowledge().detail(ident)['sources'], 'keep evidence and original knowledge'
    assert repair_unconfirmed_rules(service, apply=True)['updated_ids'] == []


@pytest.mark.parametrize('protection', ['human', 'manual', 'user'])
def test_explicit_confirmation_and_user_evidence_survive_legacy_rule_repair(service, protection):
    from evolvmem.knowledge_provenance import repair_unconfirmed_rules
    ident = old_rule(service, human=protection=='human', manual=protection=='manual', role='user' if protection=='user' else 'assistant')
    assert eligible(service.store, ident)
    assert repair_unconfirmed_rules(service, apply=True)['updated_ids'] == []
    assert service.store.get_item(ident).status.value == 'active'


@pytest.mark.parametrize('payload', ['[]', '{"basis":"inferred","evidence":[{}]}',
                                  '{"basis":"inferred","evidence":[{"role":"unknown"}]}'])
def test_unknown_legacy_evidence_is_not_reclassified_as_assistant_inference(service, payload):
    from evolvmem.knowledge_provenance import repair_unconfirmed_rules
    ident = old_rule(service)
    with service.store.transaction():
        service.store._connection().execute('UPDATE learning_memories SET payload=? WHERE item_id=?', (payload, ident))
    assert repair_unconfirmed_rules(service, apply=True)['updated_ids'] == []
    assert service.store.get_item(ident).status.value == 'active'
