from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

import pytest

from evolvmem.archive_storage import ArchiveStorage
from evolvmem.config import Config
from evolvmem.history_pg.config import PgHistoryConfig
from evolvmem.history_pg.schema import apply_history_schemas, ensure_databases_exist
from evolvmem.history_pg.workspace_index import extract_workspace_metadata, index_history_workspaces
from tests.stage5.conftest import STAGE5_PG_CONTAINER, STAGE5_PG_PORT, STAGE5_PW_FILE


def raw_session(sid, *paths):
    return ('\n'.join(json.dumps({'type': 'session_meta' if i == 0 else 'turn_context',
        'payload': {'id': sid, 'cwd': p}}) for i, p in enumerate(paths)) + '\n').encode()


def test_native_metadata_only_and_conflicts():
    found = extract_workspace_metadata(raw_session('s', r'C:\Work\Evolvmem'), 'codex', 's')
    assert found['workspace_path'] == r'C:\Work\Evolvmem'
    assert found['reason'] == 'verified'
    assert extract_workspace_metadata(raw_session('s', '/work/a', '/work/b'), 'codex', 's')['reason'] == 'mixed_workspace'
    assert extract_workspace_metadata(raw_session('s', 'relative/folder'), 'codex', 's')['workspace_path'] == ''
    assert extract_workspace_metadata(raw_session('other', '/work/a'), 'codex', 's')['reason'] == 'session_mismatch'
    body = json.dumps({'role': 'user', 'content': '<environment_context><cwd>C:\\fake</cwd></environment_context>'}).encode()
    assert extract_workspace_metadata(body, 'codex', 's')['workspace_path'] == ''
    wrapper = json.dumps({'session_id': 's', 'transcript': raw_session('s', '/work/a').decode()}).encode()
    assert extract_workspace_metadata(wrapper, 'codex', 's')['workspace_path'] == '/work/a'
    turns = json.dumps({'type': 'turn_context', 'payload': {'cwd': '/wrong/path'}})
    for identity in ({'session_id': 'OTHER'}, {'source': {'session_id': 'OTHER'}}):
        mismatch = json.dumps({**identity, 'transcript': turns}).encode()
        assert extract_workspace_metadata(mismatch, 'codex', 's')['reason'] == 'session_mismatch'


def test_index_verifies_hash_preserves_business_links_and_updates(tmp_path: Path, monkeypatch):
    cfg = PgHistoryConfig(host='127.0.0.1', port=STAGE5_PG_PORT,
        admin_password=STAGE5_PW_FILE.read_text().strip(), container_name=STAGE5_PG_CONTAINER,
        cas_root=tmp_path, user_history_dbs={'jiangli': 'evolvmem_history_migration'})
    ensure_databases_exist(cfg)
    apply_history_schemas(cfg)
    sid = 'idx-' + uuid.uuid4().hex
    bid = sid
    p = tmp_path / 'session.jsonl'
    raw = raw_session(sid, r'C:\Work\ActualFolder')
    p.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    with cfg.connect(cfg.history_migration_db) as conn:
        aid = conn.execute("INSERT INTO em_runtime.session_archives(project,adapter,external_session_id,payload_path,payload_sha256,created_at) VALUES ('business-alias','codex',%s,%s,%s,'2026-11-17') RETURNING id", (sid, str(p), sha)).fetchone()[0]
        conn.execute("INSERT INTO em_history.blocks(block_id,adapter,native_session_id,source_archive_ids,projects,block_sha256,message_count) VALUES (%s,'codex',%s,%s,'[\"business-alias\"]',%s,1)", (bid, sid, json.dumps([aid]), sha))
        conn.execute("INSERT INTO em_history.block_projects(block_id,project,evidence) VALUES (%s,'business-alias','user_confirmed')", (bid,))
        conn.commit()
    try:
        dry = index_history_workspaces(cfg, cfg.history_migration_db, block_ids=[bid])
        assert dry['verified'] == 1 and dry['written'] == 0
        first = index_history_workspaces(cfg, cfg.history_migration_db, apply=True, block_ids=[bid])
        assert first['written'] == 1
        second = index_history_workspaces(cfg, cfg.history_migration_db, apply=True, block_ids=[bid])
        assert second['unchanged'] == 1 and second['written'] == 0
        with cfg.connect(cfg.history_migration_db) as conn:
            value = json.loads(conn.execute('SELECT value FROM em_history.candidate_meta WHERE key=%s', ('workspace_projection:v1:block:' + bid,)).fetchone()[0])
            assert value['reason'] == 'verified' and value['workspace_path'] == r'C:\Work\ActualFolder'
            assert conn.execute('SELECT evidence FROM em_history.block_projects WHERE block_id=%s', (bid,)).fetchone()[0] == 'user_confirmed'
            assert conn.execute('SELECT projects FROM em_history.blocks WHERE block_id=%s', (bid,)).fetchone()[0] == '["business-alias"]'
        # A new source revision is revalidated, rather than retaining the old folder.
        raw2 = raw_session(sid, '/other/path')
        p.write_bytes(raw2)
        sha2 = hashlib.sha256(raw2).hexdigest()
        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute('UPDATE em_runtime.session_archives SET payload_sha256=%s WHERE id=%s', (sha2, aid))
            conn.commit()
        assert index_history_workspaces(cfg, cfg.history_migration_db, apply=True, block_ids=[bid])['written'] == 1
        # Corrupt evidence must not restore or invent a folder.
        p.write_bytes(b'corrupt')
        assert index_history_workspaces(cfg, cfg.history_migration_db, apply=True, block_ids=[bid], force=True)['unknown'] == 1
        # Encrypted native archives verify physical hash before metadata extraction.
        storage = ArchiveStorage(Config(data_dir=tmp_path, apply_environment=False), b'x' * 32)
        (tmp_path / 'archive.key').write_bytes(b'x' * 32)
        blob, _ = storage.pack(json.dumps({'session_id': sid, 'transcript': raw.decode()}))
        p.write_bytes(blob)
        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute('UPDATE em_runtime.session_archives SET payload_sha256=%s WHERE id=%s', (hashlib.sha256(blob).hexdigest(), aid))
            conn.commit()
        assert index_history_workspaces(cfg, cfg.history_migration_db, apply=True, block_ids=[bid], data_dir=tmp_path)['verified'] == 1
        # Compacted archives retain an evidence hash independent of current
        # manifest/content hashes. The authenticated storage record is decisive.
        manifest = storage.manifest(blob)
        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute('UPDATE em_runtime.session_archives SET payload_sha256=%s WHERE id=%s', ('0' * 64, aid))
            conn.execute('INSERT INTO em_runtime.session_archive_storage(archive_id,manifest_sha256,content_sha256,logical_bytes) VALUES (%s,%s,%s,%s)',
                (aid, hashlib.sha256(blob).hexdigest(), manifest['sha256'], manifest['size']))
            conn.commit()
        assert index_history_workspaces(cfg, cfg.history_migration_db, apply=True, block_ids=[bid], data_dir=tmp_path)['verified'] == 1
        # Exercise the exact streaming path with a small fixture, including
        # private temporary cleanup and native identity validation.
        import evolvmem.history_pg.workspace_index as wi
        monkeypatch.setattr(wi, 'MAX_METADATA_BYTES', 1)
        assert index_history_workspaces(cfg, cfg.history_migration_db, apply=True, block_ids=[bid], data_dir=tmp_path, force=True)['verified'] == 1
        wrong, wrong_manifest = storage.pack(json.dumps({'source': {'session_id': 'OTHER'}, 'transcript': json.dumps({'type': 'turn_context', 'payload': {'cwd': '/wrong/path'}})}))
        p.write_bytes(wrong)
        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute('UPDATE em_runtime.session_archive_storage SET manifest_sha256=%s,content_sha256=%s,logical_bytes=%s WHERE archive_id=%s',
                (hashlib.sha256(wrong).hexdigest(), wrong_manifest['sha256'], wrong_manifest['size'], aid))
            conn.commit()
        assert index_history_workspaces(cfg, cfg.history_migration_db, apply=True, block_ids=[bid], data_dir=tmp_path)['reasons'] == {'session_mismatch': 1}
    finally:
        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute('DELETE FROM em_history.candidate_meta WHERE key = ANY(%s)', (['workspace_projection:v1:block:' + bid, 'workspace_projection:v1:folder:' + bid],))
            conn.execute('DELETE FROM em_history.block_projects WHERE block_id=%s', (bid,))
            conn.execute('DELETE FROM em_history.blocks WHERE block_id=%s', (bid,))
            conn.execute('DELETE FROM em_runtime.session_archives WHERE id=%s', (aid,))
            conn.commit()
