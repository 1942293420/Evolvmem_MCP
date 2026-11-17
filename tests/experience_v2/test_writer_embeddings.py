"""Atomic manual embeddings through the real isolated PostgreSQL writer."""
from dataclasses import replace, asdict
import hashlib
import json
from types import SimpleNamespace
import concurrent.futures
import threading
import time

import psycopg

import pytest

from tests.stage2.conftest import pg_exp_schema
from tests.experience_v2.test_embedding_runtime import live_server
from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.experience_v2.writer import ExperienceWriter, ExperienceWriteError, Op, TicketIssuer

SECRET = b'isolated-writer-embedding-test-secret'


def _ticket():
    return TicketIssuer(SECRET).issue(proposal_uid='manual:test', decision_id='user:test', lane='user_fast')


def _create(**kwargs):
    return Op(action='create', owner_id='owner', actor_id='owner', request_id='embedding-create-01',
              category='前端经验', problem='Original problem', solution='Original solution',
              reusable_experience='Original rule', **kwargs)


def test_edit_without_encoder_clears_stale_legacy_vector(pg_exp_schema, monkeypatch):
    monkeypatch.delenv('EVOLVMEM_EXPERIENCE_EMBEDDING_CONFIG', raising=False)
    conn, _, _ = pg_exp_schema
    apply_experience_migrations(conn)
    writer = ExperienceWriter(SECRET)
    created = writer.apply(conn, _ticket(), _create(embedding=[1.] + [0.] * 767))['experience']
    writer.apply(conn, _ticket(), Op(action='update', owner_id='owner', actor_id='owner',
        request_id='embedding-update-01', note_id=created['id'], expected_revision=1,
        category='前端经验', problem='Changed problem', solution='Changed solution',
        reusable_experience='Changed rule'))
    assert conn.execute('SELECT embedding IS NULL FROM experience_notes WHERE id=%s',
                        (created['id'],)).fetchone()[0] is True


class FakeClient:
    retrieval_contract_id = 'a' * 64
    legacy_contract_id = 'b' * 64

    def __init__(self):
        self.notes = []
        self.fail = False
        self.bad = None

    def encode_note(self, note, *, deadline=None):
        self.notes.append(dict(note))
        if self.fail:
            raise RuntimeError('encoder unavailable')
        values = [float(sum(ord(c) for c in _text(note)) % 29 + 1), 1.]
        result = SimpleNamespace(legacy_embedding=values + [0.] * 766,
            retrieval_embedding=values + [0.] * 1022,
            contract_id=self.retrieval_contract_id, content_sha256=_hash(note))
        if self.bad:
            setattr(result, *self.bad)
        return result


def _text(note):
    return '\n'.join(str(note.get(k) or '') for k in
                     ('category', 'problem', 'solution', 'reusable_experience'))


def _hash(note):
    return hashlib.sha256(_text(note).encode()).hexdigest()


def _enable_policy(conn, client):
    conn.execute('INSERT INTO experience_embedding_policy '
        '(singleton, required, retrieval_contract_id, legacy_contract_id) VALUES (true,true,%s,%s)',
        (client.retrieval_contract_id, client.legacy_contract_id))


def _state(conn):
    return (conn.execute('SELECT id,category,problem,solution,reusable_experience,revision,'
        'deleted_at,status,embedding::text,retrieval_embedding::text,retrieval_embedding_contract,'
        'retrieval_embedding_content_sha256,updated_at,retrieval_embedding_updated_at '
        'FROM experience_notes ORDER BY id').fetchall(),
        conn.execute('SELECT count(*) FROM experience_events').fetchone()[0],
        conn.execute('SELECT count(*) FROM experience_requests').fetchone()[0])


def _edit(note, **changes):
    return Op(action='update', owner_id='owner', actor_id='owner', request_id='embedding-update-01',
        note_id=note['id'], expected_revision=note['revision'], category='业务经验',
        problem='Changed problem', solution='Changed solution', reusable_experience='Changed rule', **changes)


def test_create_edit_replay_and_cas_keep_both_projections(pg_exp_schema):
    conn, _, _ = pg_exp_schema; apply_experience_migrations(conn)
    client = FakeClient(); _enable_policy(conn, client)
    writer = ExperienceWriter(SECRET, embedding_client=client)
    note = writer.apply(conn, _ticket(), _create(embedding=[9.] + [0.] * 767))['experience']
    row = conn.execute('SELECT vector_dims(embedding),vector_dims(retrieval_embedding),'
        'retrieval_embedding_contract,retrieval_embedding_content_sha256 FROM experience_notes').fetchone()
    assert row == (768,1024,client.retrieval_contract_id,_hash(note))
    assert client.notes[0]['category'] == note['category']
    old = conn.execute('SELECT embedding::text FROM experience_notes').fetchone()[0]
    edited = writer.apply(conn, _ticket(), _edit(note))['experience']
    assert edited['revision'] == 2
    assert conn.execute('SELECT embedding::text FROM experience_notes').fetchone()[0] != old
    assert conn.execute('SELECT retrieval_embedding_content_sha256 FROM experience_notes').fetchone()[0] == _hash(edited)
    client.fail = True
    replay = writer.apply(conn, _ticket(), _edit(note))
    assert replay['replayed'] and replay['experience']['revision'] == 2
    assert len(client.notes) == 2
    with pytest.raises(ExperienceWriteError, match='experience_revision_conflict'):
        writer.apply(conn, _ticket(), replace(_edit(note), request_id='stale-edit-0001'))
    with pytest.raises(ExperienceWriteError, match='experience_request_id_conflict'):
        writer.apply(conn, _ticket(), replace(_edit(note), problem='different payload'))
    assert len(client.notes) == 2


@pytest.mark.parametrize('action', ['create','update','restore','reembed','reconcile_state','rollback'])
def test_encoding_failure_is_atomic_and_retryable(pg_exp_schema, action):
    conn, _, _ = pg_exp_schema; apply_experience_migrations(conn)
    client = FakeClient(); _enable_policy(conn, client)
    writer = ExperienceWriter(SECRET, embedding_client=client)
    note = writer.apply(conn, _ticket(), _create())['experience']
    if action == 'restore':
        note = writer.apply(conn, _ticket(), Op(action='delete', owner_id='owner', actor_id='owner',
            request_id='delete-for-restore',note_id=note['id'], expected_revision=1))['experience']
    conn.commit(); before = _state(conn)
    if action == 'create':
        op = replace(_create(),request_id='failing-create-01')
    elif action == 'update':
        op = _edit(note)
    else:
        kwargs = dict(action=action,owner_id='owner',actor_id='owner',request_id='failing-'+action,
                      note_id=note['id'],expected_revision=note['revision'])
        if action == 'reembed': kwargs['expected_content_sha256'] = _hash(note)
        if action == 'rollback': kwargs['restore_snapshot'] = {**note,'problem':'Restored original'}
        op = Op(**kwargs)
    client.fail = True
    with pytest.raises(ExperienceWriteError) as exc:
        writer.apply(conn,_ticket(),op)
    assert exc.value.status == 503
    assert _state(conn) == before
    client.fail = False
    assert writer.apply(conn,_ticket(),op)['ok']


def test_delete_retire_do_not_need_encoder_and_restore_uses_current_note(pg_exp_schema):
    conn, _, _ = pg_exp_schema; apply_experience_migrations(conn)
    client = FakeClient(); _enable_policy(conn, client)
    writer = ExperienceWriter(SECRET, embedding_client=client)
    note = writer.apply(conn,_ticket(),_create())['experience']; client.fail=True
    deleted = writer.apply(conn,_ticket(),Op(action='delete',owner_id='owner',actor_id='owner',
        request_id='delete-note-0001',note_id=note['id'],expected_revision=1))['experience']
    assert len(client.notes)==1
    client.fail=False
    restored = writer.apply(conn,_ticket(),Op(action='restore',owner_id='owner',actor_id='owner',
        request_id='restore-note-0001',note_id=note['id'],expected_revision=2))['experience']
    assert _hash(client.notes[-1]) == _hash(restored)
    client.fail=True
    writer.apply(conn,_ticket(),Op(action='retire',owner_id='owner',actor_id='owner',
        request_id='retire-note-0001',note_id=note['id'],expected_revision=3))
    assert len(client.notes)==2


def test_reembed_preserves_content_revision_and_has_audited_replay(pg_exp_schema):
    conn, _, _ = pg_exp_schema; apply_experience_migrations(conn)
    note=ExperienceWriter(SECRET).apply(conn,_ticket(),_create())['experience']
    client=FakeClient(); writer=ExperienceWriter(SECRET,embedding_client=client)
    op=Op(action='reembed',owner_id='owner',actor_id='owner',request_id='repair-note-0001',
        note_id=note['id'],expected_revision=1,expected_content_sha256=_hash(note))
    out=writer.apply(conn,_ticket(),op)['experience']
    assert out['revision']==note['revision'] and out['updated_at']==note['updated_at']
    for field in ('id','category','problem','solution','reusable_experience','origin','status','evidence','created_at'):
        assert out[field]==note[field]
    assert out['retrieval_embedding_content_sha256']==_hash(note)
    assert writer.apply(conn,_ticket(),op)['replayed'] is True
    assert len(client.notes)==1
    assert conn.execute("SELECT count(*) FROM experience_events WHERE action='reembed'").fetchone()[0]==1
    assert 'retrieval_embedding' not in out and 'embedding' not in out
    with pytest.raises(ExperienceWriteError,match='experience_content_conflict'):
        writer.apply(conn,_ticket(),replace(op,request_id='repair-wrong-hash',expected_content_sha256='0'*64))
    assert len(client.notes)==1


@pytest.mark.parametrize('bad', [('legacy_embedding',[1.]*767),('retrieval_embedding',[0.]*1024),
    ('retrieval_embedding',[float('nan')]*1024),('retrieval_embedding',[1e40]*1024),
    ('retrieval_embedding',[1e-60]*1024),('contract_id','wrong'),('content_sha256','wrong')])
def test_invalid_encoder_projection_cannot_write_success(pg_exp_schema,bad):
    conn, _, _ = pg_exp_schema; apply_experience_migrations(conn)
    client=FakeClient();_enable_policy(conn,client);client.bad=bad
    writer=ExperienceWriter(SECRET,embedding_client=client)
    before=_state(conn)
    with pytest.raises(ExperienceWriteError) as exc: writer.apply(conn,_ticket(),_create())
    assert exc.value.status==503
    assert _state(conn)==before


def test_required_policy_without_client_fails_closed(pg_exp_schema,monkeypatch):
    monkeypatch.delenv('EVOLVMEM_EXPERIENCE_EMBEDDING_CONFIG',raising=False)
    conn, _, _ = pg_exp_schema;apply_experience_migrations(conn)
    _enable_policy(conn,FakeClient())
    with pytest.raises(ExperienceWriteError) as exc:
        ExperienceWriter(SECRET).apply(conn,_ticket(),_create())
    assert exc.value.status==503
    assert _state(conn)[0]==[]


def test_two_connections_same_request_encode_and_commit_once(pg_exp_schema):
    conn,schema,dsn=pg_exp_schema;apply_experience_migrations(conn);conn.commit()
    client=FakeClient();barrier=threading.Barrier(2)
    def save():
        with psycopg.connect(dsn,options=f'-c search_path={schema},public') as other:
            barrier.wait(timeout=3)
            return ExperienceWriter(SECRET,embedding_client=client).apply(other,_ticket(),_create())
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda _:save(),range(2)))
    assert {o['replayed'] for o in outcomes}=={False,True}
    assert len({o['experience']['id'] for o in outcomes})==1
    assert len(client.notes)==1
    assert _state(conn)[1:]==(1,1)


def test_pg_store_write_uses_same_atomic_encoder(pg_exp_schema,monkeypatch):
    from evolvmem.experience_v2.pg_experience_store import PgExperienceStore, PgExperienceSettings
    conn,schema,dsn=pg_exp_schema;apply_experience_migrations(conn);client=FakeClient()
    _enable_policy(conn,client);conn.commit()
    store=PgExperienceStore(PgExperienceSettings(dsn=dsn,schema=schema,ticket_secret=SECRET),embedding_client=client)
    body=dict(action='create',request_id='store-create-0001',category='业务经验',
        problem='Store problem',solution='Store solution',reusable_experience='Store rule')
    status,out=store.write({'user_id':'owner'},'owner',body,body)
    assert status==201 and out['ok']
    assert len(client.notes)==1
    assert conn.execute('SELECT retrieval_embedding_content_sha256 FROM experience_notes').fetchone()[0]==_hash(out['experience'])
    client.fail=True
    assert store.write({'user_id':'owner'},'owner',body,body)[1]['replayed']
    assert len(client.notes)==1


def test_unconfigured_edit_invalidates_both_old_spaces_and_proof(pg_exp_schema,monkeypatch):
    monkeypatch.delenv('EVOLVMEM_EXPERIENCE_EMBEDDING_CONFIG',raising=False)
    conn,_,_=pg_exp_schema;apply_experience_migrations(conn)
    note=ExperienceWriter(SECRET,embedding_client=FakeClient()).apply(conn,_ticket(),_create())['experience']
    edited=ExperienceWriter(SECRET).apply(conn,_ticket(),_edit(note))['experience']
    row=conn.execute('SELECT embedding IS NULL,retrieval_embedding IS NULL,'
        'retrieval_embedding_contract IS NULL,retrieval_embedding_content_sha256 IS NULL,'
        'retrieval_embedding_updated_at IS NULL FROM experience_notes').fetchone()
    assert row==(True,True,True,True,True)
    assert edited['problem']=='Changed problem'


def test_contract_policy_mismatch_precedes_encoding(pg_exp_schema):
    conn,_,_=pg_exp_schema;apply_experience_migrations(conn)
    client=FakeClient();_enable_policy(conn,client)
    client.legacy_contract_id='c'*64
    with pytest.raises(ExperienceWriteError,match='experience_embedding_contract_mismatch'):
        ExperienceWriter(SECRET,embedding_client=client).apply(conn,_ticket(),_create())
    assert client.notes==[]
    assert _state(conn)[0]==[]


def test_configured_but_unavailable_encoder_cannot_fall_back(pg_exp_schema,monkeypatch,tmp_path):
    conn,_,_=pg_exp_schema;apply_experience_migrations(conn)
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_EMBEDDING_CONFIG',str(tmp_path/'absent.json'))
    with pytest.raises(ExperienceWriteError) as exc:
        ExperienceWriter(SECRET).apply(conn,_ticket(),_create())
    assert exc.value.status==503
    assert _state(conn)[0]==[]


def test_projection_hash_uses_exact_persisted_content_and_restore_snapshot(pg_exp_schema):
    conn,_,_=pg_exp_schema;apply_experience_migrations(conn)
    client=FakeClient();_enable_policy(conn,client);writer=ExperienceWriter(SECRET,embedding_client=client)
    original=replace(_create(),problem='  原问题  ',solution='处理\n步骤 ',reusable_experience=' 规则  ')
    note=writer.apply(conn,_ticket(),original)['experience']
    assert _hash(client.notes[-1])==_hash(note)
    snap={**note,'problem':'历史原文  ','solution':' 原始步骤\n','reusable_experience':' 保留历史空白 '}
    restored=writer.apply(conn,_ticket(),Op(action='rollback',owner_id='owner',actor_id='owner',
        request_id='rollback-exact-01',note_id=note['id'],restore_snapshot=snap))['experience']
    assert restored['problem']==snap['problem']
    assert client.notes[-1]['solution']==snap['solution']
    assert restored['retrieval_embedding_content_sha256']==_hash(restored)


def test_reembed_requires_revision_and_hash_before_encoding(pg_exp_schema):
    conn,_,_=pg_exp_schema;apply_experience_migrations(conn)
    note=ExperienceWriter(SECRET).apply(conn,_ticket(),_create())['experience'];client=FakeClient()
    with pytest.raises(ExperienceWriteError,match='experience_reembed_precondition_required'):
        ExperienceWriter(SECRET,embedding_client=client).apply(conn,_ticket(),
            Op(action='reembed',owner_id='owner',actor_id='owner',request_id='unsafe-repair-01',note_id=note['id']))
    assert client.notes==[]


def test_default_environment_client_writes_via_warm_loopback_http(pg_exp_schema,monkeypatch,tmp_path,live_server):
    client,_runtime=live_server
    path=tmp_path/'embedding-client.json';path.write_text(json.dumps(asdict(client.config)))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_EMBEDDING_CONFIG',str(path))
    conn,_,_=pg_exp_schema;apply_experience_migrations(conn);_enable_policy(conn,client)
    note=ExperienceWriter(SECRET).apply(conn,_ticket(),_create())['experience']
    assert note['retrieval_embedding_contract']==client.retrieval_contract_id
    assert note['retrieval_embedding_content_sha256']==_hash(note)
    assert conn.execute('SELECT vector_dims(embedding),vector_dims(retrieval_embedding) FROM experience_notes').fetchone()==(768,1024)
