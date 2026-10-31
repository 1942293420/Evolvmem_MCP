"""Synthetic same-session arrival regressions; no live archives or providers."""
from collections import Counter

from tests.test_cross_batch_context import config, service, craft, SESSION, OTHER_SESSION  # noqa: F401


def test_expanding_same_session_reads_each_native_payload_only_once(service, monkeypatch):
    from evolvmem import conversation_tasks, organization_context
    from evolvmem.session_archive import SessionArchiver
    batches = [craft(service, session=SESSION, start=i * 3 + 1, end=i * 3 + 3,
                     text=f'Independent batch {i}: keep exact source bytes.') for i in range(40)]
    organization_context._INFO_CACHE.clear()
    reads = Counter()
    original = SessionArchiver.read_payload
    def measured(archiver, archive_id):
        reads[archive_id] += 1
        return original(archiver, archive_id)
    monkeypatch.setattr(SessionArchiver, 'read_payload', measured)
    keys = conversation_tasks.register(service, [f'archive:{b.id}' for b in batches[-5:]], expand=True)
    assert len(keys) == 1
    assert conversation_tasks.members(service, keys[0]) == [f'archive:{b.id}' for b in batches]
    assert set(reads) == {b.id for b in batches}
    assert max(reads.values()) == 1, 'same native payload was decrypted repeatedly in one registration'


def test_same_operation_exclusions_are_not_retained_on_the_next_registration(service):
    from evolvmem import conversation_tasks, organization_context
    a = craft(service, session=SESSION, start=1, end=3, text='First verified source.')
    b = craft(service, session=SESSION, start=4, end=6, text='Second verified source.')
    other = craft(service, session=OTHER_SESSION, start=1, end=3, text='Other session remains independent.')
    keys = conversation_tasks.register(service, [f'archive:{a.id}', f'archive:{other.id}'], expand=True)
    assert len(keys) == 2
    conn = service.store._connection()
    row = service.store.get_session_archive(b.id)
    with service.store.transaction():
        conn.execute('INSERT INTO archive_source_exclusions(archive_id,payload_sha256,reason,verified_at) VALUES(?,?,?,?)',
                     (b.id, row['payload_sha256'], 'codex_subagent', '2026-10-21 00:00:00'))
    assert organization_context.archive_info(service, f'archive:{b.id}') is None
    assert conversation_tasks.register(service, [f'archive:{b.id}'], expand=True) == []


def test_exclusion_added_during_registration_prevents_manifest_write(service, monkeypatch):
    import pytest
    from evolvmem import conversation_tasks, organization_context
    from evolvmem.session_archive import SessionArchiver
    a = craft(service, session=SESSION, start=1, end=3, text='Current verified source.')
    b = craft(service, session=SESSION, start=4, end=6, text='Newly excluded source.')
    organization_context._INFO_CACHE.clear()
    original = SessionArchiver.read_payload
    added = []
    def changed_policy(archiver, archive_id):
        raw = original(archiver, archive_id)
        if archive_id == b.id and not added:
            added.append(True)
            row = service.store.get_session_archive(b.id)
            with service.store.transaction():
                service.store._connection().execute(
                    'INSERT INTO archive_source_exclusions(archive_id,payload_sha256,reason,verified_at) VALUES(?,?,?,?)',
                    (b.id, row['payload_sha256'], 'codex_subagent', '2026-10-21 00:00:00'))
        return raw
    monkeypatch.setattr(SessionArchiver, 'read_payload', changed_policy)
    with pytest.raises(ValueError, match='classification_source_changed|conversation_source_changed'):
        conversation_tasks.register(service, [f'archive:{a.id}', f'archive:{b.id}'], expand=True)
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_conversations').fetchone()[0] == 0


def test_payload_revision_change_cannot_reuse_registration_identity(service, monkeypatch):
    import pytest
    from evolvmem import conversation_tasks, organization_context
    from evolvmem.session_archive import SessionArchiver
    a = craft(service, session=SESSION, start=1, end=3, text='Initially verified source.')
    b = craft(service, session=SESSION, start=4, end=6, text='Triggers a source revision change.')
    organization_context._INFO_CACHE.clear()
    original = SessionArchiver.read_payload
    replacement = service.store.get_session_archive(b.id)
    changed = []
    def changed_source(archiver, archive_id):
        raw = original(archiver, archive_id)
        if archive_id == b.id and not changed:
            changed.append(True)
            with service.store.transaction():
                service.store._connection().execute(
                    'UPDATE session_archives SET payload_sha256=?,payload_path=? WHERE id=?',
                    (replacement['payload_sha256'], replacement['payload_path'], a.id))
        return raw
    monkeypatch.setattr(SessionArchiver, 'read_payload', changed_source)
    with pytest.raises(ValueError, match='classification_source_changed|conversation_source_changed'):
        conversation_tasks.register(service, [f'archive:{a.id}', f'archive:{b.id}'], expand=True)
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_conversations').fetchone()[0] == 0
