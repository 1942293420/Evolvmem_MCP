import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from evolvmem.capability_store import CapabilityMemoryDraft, CapabilitySourceDraft, CapabilityStore
from evolvmem.context_store import ContextStore
from evolvmem.auto_organization import unit_digest


DIGEST = unit_digest('archive:1', 0, 12, 'user: a fact')


@pytest.fixture
def provenance_db(test_config):
    with ContextStore(test_config) as store:
        conn = store._connection()
        with store.transaction():
            for project in ('alpha', 'beta'):
                conn.execute("INSERT INTO context_project_registry(project,created_at,updated_at) VALUES(?,'old','old')", (project,))
            for identity in (1, 2):
                conn.execute("INSERT INTO context_items(id,identity_key,content_type,project,status,created_at,updated_at) VALUES(?,?,'fact','alpha','active','old','old')", (identity, f'fact:{identity}'))
                conn.execute("INSERT INTO context_layers(item_id,layer,content,content_hash,generator,created_at,updated_at) VALUES(?,'l2','A fact','hash','test','old','old')", (identity,))
            conn.execute("INSERT INTO session_archives(id,project,adapter,external_session_id,payload_path,payload_sha256,expires_at,created_at) VALUES(1,'','codex','session:1','unused','payload','2099','old')")
            conn.execute("INSERT INTO conversation_history(archive_id,messages,body,content_hash,cleaned_at) VALUES(1,'[]','user: a fact','clean','old')")
            raw = hashlib.sha256(json.dumps(['payload', 'available', 'user: a fact'], ensure_ascii=False).encode()).hexdigest()
            revision = hashlib.sha256((raw + ':0').encode()).hexdigest()
            conn.execute("INSERT INTO organization_tasks(id,source_key,source_revision,rule_revision,status,created_at,updated_at) VALUES(1,'archive:1',?,'rule','completed','old','old')", (revision,))
            conn.execute("INSERT INTO organization_units(task_id,ordinal,digest,title,text,source_start,source_end,project,projects,decision,created_at,updated_at) VALUES(1,1,?,'Fact','user: a fact',0,12,'alpha','[\"alpha\",\"beta\"]','manual','old','old')", (DIGEST,))
        with store.transaction():
            conn.execute("UPDATE organization_tasks SET source_snapshot='user: a fact' WHERE id=1")
        yield store


def add_memory(store, *, title='Fact', ref='', source=None):
    return CapabilityStore(store).upsert(CapabilityMemoryDraft(category='backend', title=title, content='A fact', source_ref=ref), source=source)


def unit_source(store, **extra):
    row = store._connection().execute('SELECT t.source_revision,t.source_key,u.digest FROM organization_tasks t JOIN organization_units u ON u.task_id=t.id WHERE t.id=1').fetchone()
    ref = f"{row['source_key']}#{row['digest']}"
    fields = dict(source_kind='unit', source_key=ref, source_ref=ref,
                  source_revision=row[0], task_id=1, unit_digest=row['digest'], unit_revision=1,
                  projects=('alpha', 'beta'), evidence_status='confirmed')
    fields.update(extra)
    return CapabilitySourceDraft(**fields)


def context_source(store, identity=1, **extra):
    from evolvmem.capability_provenance import context_revision
    fields = dict(source_kind='context', source_key=f'context:{identity}', source_ref=f'context_item:{identity}',
                  context_item_id=identity, source_revision=context_revision(store._connection(), identity))
    fields.update(extra)
    return CapabilitySourceDraft(**fields)


def test_current_unit_can_confirm_multiple_projects(provenance_db):
    from evolvmem.capability_provenance import annotate, eligible_memory_ids
    store = provenance_db
    identity = add_memory(store, source=unit_source(store))
    row = annotate(store, [identity])[identity]
    assert row['eligible'] and row['ownership_confirmed']
    assert row['source_projects'] == ['alpha', 'beta']
    assert eligible_memory_ids(store) == {identity}


def test_unassigned_keep_content_is_valid_without_project_confirmation(provenance_db):
    from evolvmem.capability_provenance import annotate
    store = provenance_db
    with store.transaction():
        store._connection().execute("UPDATE organization_units SET decision='review',project='',projects='[]'")
    identity = add_memory(store, source=unit_source(store))
    row = annotate(store)[identity]
    assert row['eligible'] and not row['ownership_confirmed']
    assert row['source_projects'] == []


@pytest.mark.parametrize('sql,reason', [
    ("UPDATE organization_tasks SET status='superseded',superseded_by=NULL", 'task_superseded'),
    ("UPDATE organization_units SET disposition='review'", 'unit_withdrawn'),
    ("UPDATE organization_units SET disposition='set_aside'", 'unit_withdrawn'),
    ("UPDATE organization_units SET revision=revision+1,text='different original'", 'unit_content_changed'),
    ("UPDATE organization_tasks SET source_revision='changed'", 'source_revision_changed'),
    ("UPDATE session_archives SET state='purged'", 'archive_unavailable'),
    ("UPDATE conversation_history SET body='different'", 'source_revision_changed'),
    ("INSERT INTO session_archives(project,adapter,external_session_id,payload_path,payload_sha256,expires_at,created_at) VALUES('','codex','session:2','unused','new','2099','znew')", 'archive_not_current'),
])
def test_invalid_unit_source_never_supplies_projects(provenance_db, sql, reason):
    from evolvmem.capability_provenance import annotate
    store = provenance_db
    identity = add_memory(store, source=unit_source(store))
    with store.transaction():
        store._connection().execute(sql)
    row = annotate(store)[identity]
    assert not row['eligible']
    assert row['source_projects'] == []
    assert row['sources'][0]['reason'] == reason


def test_shared_memory_retains_one_valid_source(provenance_db):
    from evolvmem.capability_provenance import annotate
    store = provenance_db
    identity = add_memory(store, source=unit_source(store))
    add_memory(store, source=context_source(store))
    with store.transaction():
        store._connection().execute("UPDATE organization_tasks SET status='superseded'")
    row = annotate(store)[identity]
    assert row['eligible']
    assert row['source_projects'] == []
    assert len(row['sources']) == 2


def test_context_manual_ownership_is_read_live_not_from_source_claim(provenance_db):
    from evolvmem.capability_provenance import annotate
    store = provenance_db
    identity = add_memory(store, source=context_source(store, projects=('alpha',), evidence_status='confirmed'))
    with store.transaction():
        conn = store._connection()
        conn.execute("UPDATE context_items SET project='beta',projects='[\"beta\"]' WHERE id=1")
        conn.execute("INSERT INTO context_project_resolutions(item_id,resolution_state,decision_source,review_state,resolved_project,created_at,updated_at) VALUES(1,'resolved','human','accepted','beta','old','old')")
    row = annotate(store)[identity]
    assert row['eligible'] and row['source_projects'] == ['beta']


@pytest.mark.parametrize('sql', ["UPDATE context_items SET status='superseded' WHERE id=1", "UPDATE context_layers SET content_hash='changed' WHERE item_id=1", "UPDATE context_items SET content_type='experience',success_count=0 WHERE id=1", "UPDATE context_items SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=1"])
def test_context_must_be_current_and_eligible(provenance_db, sql):
    from evolvmem.capability_provenance import eligible_memory_ids
    store = provenance_db
    identity = add_memory(store, source=context_source(store))
    with store.transaction():
        store._connection().execute(sql)
    assert identity not in eligible_memory_ids(store)


@pytest.mark.parametrize('kind', ['manual', 'reference'])
def test_unanchored_reference_or_manual_claim_needs_review(provenance_db, kind):
    from evolvmem.capability_provenance import annotate
    store = provenance_db
    identity = add_memory(store, source=CapabilitySourceDraft(source_kind=kind, source_key='note', source_ref='file.md', projects=('alpha',), evidence_status='confirmed'))
    row = annotate(store)[identity]
    assert not row['eligible'] and row['source_projects'] == []
    assert row['sources'][0]['reason'] == 'unanchored_reference'


def test_legacy_mapping_and_unit_fallback_do_not_require_schema_mutation(provenance_db):
    from evolvmem.capability_provenance import annotate
    store = provenance_db
    context_id = add_memory(store)
    unit_id = add_memory(store, title='Second', ref=f'archive:1#{DIGEST}')
    with store.transaction():
        conn = store._connection()
        conn.execute("INSERT INTO knowledge_restructure_map VALUES(1,'capability',?,'migrated','old')", (str(context_id),))
        conn.execute('DROP TABLE capability_sources')
    before = conn.total_changes
    rows = annotate(store)
    assert rows[context_id]['eligible'] and rows[unit_id]['eligible']
    assert rows[context_id]['legacy_fallback'] and rows[unit_id]['legacy_fallback']
    assert conn.total_changes == before
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='capability_sources'").fetchone()


def test_historical_only_unit_is_retained_as_invalid_without_inheriting_project(provenance_db):
    from evolvmem.capability_provenance import annotate
    store = provenance_db
    identity = add_memory(store, ref=f'archive:1#{DIGEST}')
    with store.transaction():
        store._connection().execute("UPDATE organization_tasks SET status='superseded'")
    row = annotate(store)[identity]
    assert not row['eligible'] and row['source_projects'] == []
    assert row['sources'][0]['reason'] == 'historical_unit_only'
    assert CapabilityStore(store).get(identity)['status'] == 'active'


def test_legacy_pipeline_ref_preserves_and_checks_original_range(provenance_db):
    from evolvmem.capability_provenance import annotate
    store = provenance_db
    valid = add_memory(store, ref=f'archive:1#{DIGEST}@0-12')
    invalid = add_memory(store, title='Bad range', ref=f'archive:1#{DIGEST}@0-13')
    rows = annotate(store)
    assert rows[valid]['eligible']
    assert not rows[invalid]['eligible']
    assert rows[invalid]['sources'][0]['reason'] == 'source_range_changed'


def test_conversation_revision_matches_existing_oracle_and_member_edits_invalidate(provenance_db):
    from evolvmem.capability_provenance import annotate
    from evolvmem.conversation_tasks import revision
    store = provenance_db
    conn = store._connection()
    with store.transaction():
        conn.execute("INSERT INTO organization_conversations VALUES('conversation:test','test','[\"archive:1\"]','old')")
        value = revision(SimpleNamespace(store=store), 'conversation:test')
        conn.execute("UPDATE organization_tasks SET source_key='conversation:test',source_revision=?", (value,))
        conn.execute('UPDATE organization_units SET digest=?', (unit_digest('conversation:test', 0, 12, 'user: a fact'),))
    identity = add_memory(store, source=unit_source(store))
    assert annotate(store)[identity]['eligible']
    with store.transaction():
        conn.execute("UPDATE conversation_history SET body='changed body'")
    assert not annotate(store)[identity]['eligible']


def test_item_revision_matches_knowledge_editor_without_initializing_services(provenance_db, test_config):
    from evolvmem.capability_provenance import annotate, ProvenanceRead
    from evolvmem.knowledge import KnowledgeBase
    from evolvmem.knowledge_cleaning import record
    from evolvmem.memory_learning import MemoryLearning
    store = provenance_db
    service = SimpleNamespace(store=store, config=test_config)
    service.learning = lambda: MemoryLearning(service)
    service.knowledge = lambda: KnowledgeBase(service)
    conn = store._connection()
    with store.transaction():
        conn.execute("UPDATE context_items SET project='' WHERE id=1")
        editor_revision = KnowledgeBase(service).detail(1)['revision']
        value = hashlib.sha256((editor_revision + ':0').encode()).hexdigest()
        assert value == record(service, 'item:1')['expected_revision']
        assert ProvenanceRead(store).source_revision('item:1') == (value, '')
        conn.execute("UPDATE organization_tasks SET source_key='item:1',source_revision=?", (value,))
        conn.execute('UPDATE organization_units SET digest=?', (unit_digest('item:1', 0, 12, 'user: a fact'),))
    identity = add_memory(store, source=unit_source(store))
    assert annotate(store)[identity]['eligible']
    with store.transaction():
        conn.execute("UPDATE context_layers SET content='edited body' WHERE item_id=1")
    assert not annotate(store)[identity]['eligible']


def test_newer_manual_ownership_retains_content_when_actual_digest_matches(provenance_db):
    from evolvmem.capability_provenance import annotate
    store = provenance_db
    identity = add_memory(store, source=unit_source(store))
    with store.transaction():
        store._connection().execute("UPDATE organization_units SET revision=2,decision='manual',project='beta',projects='[\"beta\"]'")
    row = annotate(store)[identity]
    assert row['eligible'] and row['source_projects'] == ['beta']
    assert row['sources'][0]['ownership_updated']


def test_metadata_cache_reuses_same_database_token_and_invalidates_own_changes(provenance_db, monkeypatch):
    from evolvmem.capability_provenance import annotate
    from evolvmem import history_organization
    store = provenance_db
    identity = add_memory(store, source=unit_source(store))
    original = history_organization._current_archives
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(history_organization, '_current_archives', counted)
    assert annotate(store)[identity]['eligible']
    assert annotate(store)[identity]['eligible']
    assert len(calls) == 1
    with store.transaction():
        store._connection().execute("UPDATE conversation_history SET body='changed'")
    assert not annotate(store)[identity]['eligible']
    assert len(calls) == 2


@pytest.mark.parametrize('change', ['body', 'head'])
def test_metadata_cache_invalidates_external_source_or_head_commits(provenance_db, test_config, change):
    from evolvmem.capability_provenance import annotate
    store = provenance_db
    conn = store._connection()
    if change == 'head':
        with store.transaction():
            conn.execute('CREATE TABLE lan_session_uploads(device_id TEXT,session_id TEXT,sha256 TEXT,archive_id INTEGER,attribution_reason TEXT)')
            conn.execute('CREATE TABLE lan_session_heads(device_id TEXT,session_id TEXT,sha256 TEXT)')
            conn.execute("INSERT INTO lan_session_uploads VALUES('device','session','one',1,'')")
            conn.execute("INSERT INTO lan_session_heads VALUES('device','session','one')")
    identity = add_memory(store, source=unit_source(store))
    assert annotate(store)[identity]['eligible']
    with sqlite3.connect(test_config.db_path) as writer:
        if change == 'body':
            writer.execute("UPDATE conversation_history SET body='external change'")
        else:
            writer.execute("UPDATE lan_session_heads SET sha256='new head'")
    row = annotate(store)[identity]
    assert not row['eligible']
    assert row['sources'][0]['reason'] == ('source_revision_changed' if change == 'body' else 'archive_not_current')


def test_rollback_does_not_leave_transaction_cache_stale(provenance_db):
    from evolvmem.capability_provenance import annotate
    store = provenance_db
    identity = add_memory(store, source=unit_source(store))
    with pytest.raises(RuntimeError):
        with store.transaction():
            store._connection().execute("UPDATE conversation_history SET body='temporary edit'")
            assert not annotate(store)[identity]['eligible']
            raise RuntimeError('rollback')
    assert annotate(store)[identity]['eligible']
