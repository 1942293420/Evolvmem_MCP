"""Dual writes must retain their supplied source, not only a projection copy."""
from tests.test_history_qa_memory import service
from evolvmem.legacy_models import LegacyAddRequest, LegacyReplaceRequest


def test_supplied_source_is_kept_for_both_add_and_replace(service):
    source = 'codex:synthetic-source-session'
    a = service.legacy_add(LegacyAddRequest(key='project:evo:constraint:origin', value='界面需要实际截图验证结果。', source_session=source))
    rows = service.knowledge().detail(a.context_id)['sources']
    assert any(row['source_ref'] == source for row in rows)
    b = service.legacy_replace(LegacyReplaceRequest(key='project:evo:constraint:origin', new_value='界面需要实际截图并保存验收结果。', source_session=source))
    rows = service.knowledge().detail(b.context_id)['sources']
    assert any(row['source_ref'] == source for row in rows)


def test_provenance_repair_uses_exact_existing_mapping_and_is_idempotent(service):
    from evolvmem.knowledge_provenance import restore_legacy_sources
    item = service.legacy_add(LegacyAddRequest(key='project:evo:constraint:lost', value='原始资料中明确的截图验证要求。', source_session='codex:old-source'))
    with service.store.transaction():
        service.store._connection().execute('DELETE FROM context_sources WHERE item_id=?', (item.context_id,))
    repaired = restore_legacy_sources(service.store)
    assert repaired['restored_ids'] == [item.context_id]
    assert restore_legacy_sources(service.store)['restored_ids'] == []
    assert service.knowledge().detail(item.context_id)['sources'][0]['source_ref'] == 'codex:old-source'


def test_provenance_repair_does_not_invent_source_or_bind_changed_body(service):
    from evolvmem.knowledge_provenance import restore_legacy_sources
    blank = service.legacy_add(LegacyAddRequest(key='project:evo:constraint:blank', value='没有来源标识的旧版资料正文。'))
    changed = service.legacy_add(LegacyAddRequest(key='project:evo:constraint:changed', value='有来源标识的原始资料正文。', source_session='codex:old-source'))
    with service.store.transaction():
        conn = service.store._connection()
        conn.execute('DELETE FROM context_sources WHERE item_id=?', (changed.context_id,))
        conn.execute("UPDATE context_layers SET content='后续另行编辑的资料正文' WHERE item_id=? AND layer='l2'", (changed.context_id,))
    assert restore_legacy_sources(service.store)['restored_ids'] == []
