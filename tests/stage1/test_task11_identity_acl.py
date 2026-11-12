"""Task 1.1a (PG Plan Task 2): Identity, directional grants, default write library, and attachment/trash ACL."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import psycopg
import pytest

from evolvmem.manual_experience_mcp import ManualMcpError, call as mcp_manual_call, registered_source
from tests.library_foundation.test_ddl_contract import security_findings
from tests.stage1.pg_stage1_harness import (
    ROOT,
    TEST_PG_HOST,
    TEST_PG_PORT,
    create_isolated_control_db,
    drop_isolated_db,
)

ADMIN_SESSION = {'open_id': 'feishu-admin-001', 'name': 'AdminUser', 'csrf': 'csrf-admin-token-0001'}
USER_A_SESSION = {'open_id': 'feishu-user-a-001', 'name': 'UserA', 'csrf': 'csrf-user-a-token-0001'}
USER_B_SESSION = {'open_id': 'feishu-user-b-001', 'name': 'UserB', 'csrf': 'csrf-user-b-token-0001'}
USER_C_SESSION = {'open_id': 'feishu-user-c-001', 'name': 'UserC', 'csrf': 'csrf-user-c-token-0001'}


@pytest.fixture()
def pg_control_env(tmp_path):
    env = create_isolated_control_db(tmp_path, up_to_version=7)
    try:
        yield env
    finally:
        drop_isolated_db(env.database, (env.coordinator_role, env.reader_role, env.owner_role))


def build_pg_console(pg_control_env, tmp_path, *, owner_id='admin_owner'):
    from evolvmem.experience_v2.control.pg_console_store import PgConsoleStore, PgControlConfig
    from evolvmem.knowledge_console import ConsoleService

    cfg = PgControlConfig.from_file(pg_control_env.config_file)
    store = PgConsoleStore(cfg)
    owner_dir = tmp_path / 'sources' / owner_id
    owner_dir.mkdir(parents=True, exist_ok=True)
    svc = ConsoleService(
        tmp_path / 'unused_sqlite' / 'users.db',
        owner_id,
        owner_dir,
        tmp_path / 'lan',
        {},
        pg_control=store,
    )
    svc.bootstrap(ADMIN_SESSION['open_id'], ADMIN_SESSION['name'])
    for uid, sess in (('user_a', USER_A_SESSION), ('user_b', USER_B_SESSION), ('user_c', USER_C_SESSION)):
        svc.observe_login(sess['open_id'], sess['name'])
        st, _, _ = svc.dispatch(
            'POST', '/users', {'id': uid, 'display_name': sess['name'], 'open_id': sess['open_id']}, ADMIN_SESSION
        )
        assert st == 201
    return svc, store, cfg


def init_sqlite_source(directory: Path) -> Path:
    from evolvmem.config import Config
    from evolvmem.context_models import ContextMode
    from evolvmem.context_service import ContextService
    from evolvmem.memory_store import MemoryStore

    directory.mkdir(parents=True, exist_ok=True)
    cfg = Config(data_dir=directory, apply_environment=False)
    cfg.ensure_dirs()
    with MemoryStore(cfg):
        pass
    ctx = ContextService(cfg)
    try:
        ctx.initialize(mode=ContextMode.SHADOW, adapter='codex')
    finally:
        ctx.close()
    return directory


def test_static_sql_contract_007_and_append_only_chain():
    sql_007 = ROOT / 'evolvmem/library_catalog/sql/007_console_control_plane.sql'
    sql_007a = ROOT / 'evolvmem/library_catalog/sql/007a_device_project_acl.sql'
    prov = ROOT / 'evolvmem/library_catalog/sql/provision_console_service.sql'
    assert sql_007.is_file(), '007_console_control_plane.sql missing'
    assert sql_007a.is_file(), '007a_device_project_acl.sql missing'
    assert prov.is_file(), 'provision_console_service.sql missing'
    assert security_findings(sql_007.read_text(encoding='utf-8')) == []
    assert security_findings(sql_007a.read_text(encoding='utf-8')) == []
    assert security_findings(prov.read_text(encoding='utf-8')) == []
    prov_text = prov.read_text(encoding='utf-8')
    assert 'GRANT SELECT' not in prov_text.upper()
    assert 'GRANT INSERT' not in prov_text.upper()


def test_pg_roles_and_rls_real_postgres(pg_control_env):
    from evolvmem.experience_v2.control.migrations import loopback_psycopg_connector

    conn_fn = loopback_psycopg_connector(allow_loopback_disable_ssl=True)
    for role, pw in (
        (pg_control_env.coordinator_role, pg_control_env.coordinator_pw),
        (pg_control_env.reader_role, pg_control_env.reader_pw),
    ):
        with conn_fn(
            host=TEST_PG_HOST, port=TEST_PG_PORT, dbname=pg_control_env.database,
            user=role, password=pw, autocommit=True, sslmode='verify-full',
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb '
                    'FROM pg_roles WHERE rolname = session_user'
                )
                assert cur.fetchone() == (False, False, False, False)
                cur.execute(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='em_control' AND c.relkind='r' "
                    "AND has_table_privilege(session_user, c.oid, 'SELECT,INSERT,UPDATE,DELETE')"
                )
                assert cur.fetchone()[0] == 0
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute('SELECT * FROM em_control.console_users')
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    cur.execute('SELECT * FROM em_control.web_sessions')


def test_directional_nontransitive_grants_and_viewer_write_blocked(pg_control_env, tmp_path):
    svc, _, _ = build_pg_console(pg_control_env, tmp_path)
    assert not (tmp_path / 'unused_sqlite' / 'users.db').exists()
    _, users_body = svc.dispatch('GET', '/users', {}, ADMIN_SESSION)[1:]
    assert 'kane' not in {u['id'] for u in users_body['users']}

    for uid in ('user_a', 'user_b', 'user_c'):
        src = init_sqlite_source(tmp_path / 'sources' / uid)
        svc.configure_library(uid, data_dir=src)

    st, _, g1 = svc.dispatch(
        'POST', '/grants',
        {'viewer_id': 'user_a', 'owner_id': 'user_b', 'history_read': True, 'experience_read': False, 'revision': 0},
        ADMIN_SESSION,
    )
    assert st == 200 and g1['grant']['revision'] == 1
    st, _, _ = svc.dispatch(
        'POST', '/grants',
        {'viewer_id': 'user_b', 'owner_id': 'user_c', 'history_read': True, 'experience_read': True, 'revision': 0},
        ADMIN_SESSION,
    )
    assert st == 200

    assert svc.dispatch('GET', '/libraries/user_b/history', {}, USER_A_SESSION)[0] == 200
    assert svc.dispatch('GET', '/libraries/user_a/history', {}, USER_B_SESSION)[0] == 403
    assert svc.dispatch('GET', '/libraries/user_b/experiences', {}, USER_A_SESSION)[0] == 403
    assert svc.dispatch('GET', '/libraries/user_c/history', {}, USER_A_SESSION)[0] == 403
    assert svc.dispatch('GET', '/libraries/user_c/experiences', {}, USER_A_SESSION)[0] == 403

    assert svc.dispatch(
        'POST', '/grants',
        {'viewer_id': 'user_a', 'owner_id': 'user_b', 'history_read': True, 'experience_read': True, 'revision': 1},
        ADMIN_SESSION,
    )[0] == 200
    assert svc.dispatch('GET', '/libraries/user_b/experiences', {}, USER_A_SESSION)[0] == 200
    assert svc.dispatch(
        'POST', '/libraries/user_b/experiences',
        {'action': 'create', 'category': '系统环境经验', 'problem': 'p', 'solution': 's', 'reusable_experience': 'r', 'request_id': 'req-12345678'},
        USER_A_SESSION,
    )[0] == 403
    assert svc.dispatch(
        'POST', '/libraries/user_b/history/management/delete',
        {'request_id': 'req-del-1', 'targets': []},
        USER_A_SESSION,
    )[0] == 403


def test_manual_write_targets_only_own_default_library_and_unconfigured_fails(pg_control_env, tmp_path, monkeypatch):
    svc, store, _ = build_pg_console(pg_control_env, tmp_path)
    dir_a = init_sqlite_source(tmp_path / 'sources' / 'user_a')
    dir_b = init_sqlite_source(tmp_path / 'sources' / 'user_b')
    svc.configure_library('user_a', data_dir=dir_a)
    svc.configure_library('user_b', data_dir=dir_b)

    src_a = store.get_user_source('admin_owner', 'user_a')
    src_c = store.get_user_source('admin_owner', 'user_c')
    assert src_a['default_write_library_id'] is not None
    assert src_c['default_write_library_id'] is None

    svc.dispatch(
        'POST', '/grants',
        {'viewer_id': 'user_a', 'owner_id': 'user_b', 'history_read': True, 'experience_read': True, 'revision': 0},
        ADMIN_SESSION,
    )

    monkeypatch.setenv('EVOLVMEM_CONTROL_BACKEND', 'postgres')
    monkeypatch.setenv('EVOLVMEM_CONSOLE_PG_CONFIG', str(pg_control_env.config_file))
    monkeypatch.setenv('EVOLVMEM_CONSOLE_OWNER_ID', 'admin_owner')

    reg_a = registered_source(dir_a, owner_data_dir=tmp_path / 'sources' / 'admin_owner', user='user_a')
    res = mcp_manual_call(
        'manual_experience_save',
        {
            'category': '系统环境经验',
            'problem': '服务启动端口冲突',
            'solution': '检查监听进程并释放占用端口后重启',
            'reusable_experience': '部署前先用 ss 校验端口唯一性',
            'request_id': 'req-user-a-note-001',
        },
        reg_a,
    )
    assert res['ok'] is True

    _, body_a = svc.dispatch('GET', '/libraries/user_a/experiences', {'origin': 'manual'}, USER_A_SESSION)[1:]
    _, body_b = svc.dispatch('GET', '/libraries/user_b/experiences', {'origin': 'manual'}, USER_B_SESSION)[1:]
    assert body_a['total'] == 1
    assert body_b['total'] == 0

    with pytest.raises(ManualMcpError, match='experience_not_connected'):
        registered_source(tmp_path / 'sources' / 'user_c', owner_data_dir=tmp_path / 'sources' / 'admin_owner', user='user_c')
    st_c, _, err_c = svc.dispatch(
        'POST', '/libraries/user_c/experiences',
        {'action': 'create', 'category': '系统环境经验', 'problem': 'p', 'solution': 's', 'reusable_experience': 'r', 'request_id': 'req-c-12345678'},
        USER_C_SESSION,
    )
    assert st_c == 404 and err_c['error'] == 'experience_not_connected'


@pytest.mark.parametrize(
    'spoof_field',
    [
        'user_id', 'actor', 'actor_id', 'principal', 'principal_id',
        'library_id', 'default_write_library_id', 'device_id', 'credential_id',
        'owner_id', 'data_dir', 'source_path', 'is_admin',
    ],
)
def test_spoofed_identity_and_library_override_rejected(pg_control_env, tmp_path, spoof_field):
    svc, _, _ = build_pg_console(pg_control_env, tmp_path)
    st, _, body = svc.dispatch('GET', '/me', {spoof_field: 'admin_owner'}, USER_A_SESSION)
    assert st == 400 and body['error'] == 'identity_override_forbidden'


def test_attachment_source_isolation_trash_privacy_and_immediate_revocation(pg_control_env, tmp_path):
    from tests.test_history_candidate_read import build_fixture

    svc, _, _ = build_pg_console(pg_control_env, tmp_path)
    dir_b = init_sqlite_source(tmp_path / 'sources' / 'user_b')
    cand_dir = tmp_path / 'cand_b'
    cand_dir.mkdir()
    cand_db = cand_dir / 'candidate.db'
    build_fixture(cand_db)
    raw_bytes = b'private-attachment-payload-b'
    sha = hashlib.sha256(raw_bytes).hexdigest()
    (cand_dir / 'attachments').mkdir()
    (cand_dir / 'attachments' / 'diagram.png').write_bytes(raw_bytes)
    tok_file = cand_dir / 'tok'
    tok_file.write_text('secret-history-token-b-0001\n', encoding='utf-8')
    with sqlite3.connect(cand_db) as con:
        con.execute('CREATE TABLE IF NOT EXISTS attachments(sha256 TEXT,mime_type TEXT,bytes INTEGER,path TEXT)')
        con.execute('INSERT INTO attachments(sha256,mime_type,bytes,path) VALUES(?,?,?,?)', (sha, 'image/png', len(raw_bytes), 'diagram.png'))
        con.execute('UPDATE messages SET attachments=? WHERE ordinal=0', (json.dumps([{'sha256': sha}]),))

    svc.configure_library(
        'user_b',
        data_dir=dir_b,
        history_environ={
            'EVOLVMEM_HISTORY_CANDIDATE_DB': str(cand_db),
            'EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE': str(tok_file),
        },
    )
    st1, _, _ = svc.dispatch(
        'POST', '/libraries/user_b/experiences',
        {'action': 'create', 'category': '系统环境经验', 'problem': 'active', 'solution': 'sol', 'reusable_experience': 'reuse', 'request_id': 'req-b-active-01'},
        USER_B_SESSION,
    )
    st2, _, created_2 = svc.dispatch(
        'POST', '/libraries/user_b/experiences',
        {'action': 'create', 'category': '系统环境经验', 'problem': 'trashed', 'solution': 'sol2', 'reusable_experience': 'reuse2', 'request_id': 'req-b-trash-01'},
        USER_B_SESSION,
    )
    assert st1 == 201 and st2 == 201
    assert svc.dispatch(
        'POST', '/libraries/user_b/experiences',
        {'action': 'delete', 'id': created_2['experience']['id'], 'revision': 1, 'request_id': 'req-b-del-0001'},
        USER_B_SESSION,
    )[0] == 200

    assert svc.dispatch('GET', f'/libraries/user_b/history/attachment/{sha}', {}, USER_A_SESSION)[0] == 403

    svc.dispatch(
        'POST', '/grants',
        {'viewer_id': 'user_a', 'owner_id': 'user_b', 'history_read': True, 'experience_read': True, 'revision': 0},
        ADMIN_SESSION,
    )
    st_att, mime, payload = svc.dispatch('GET', f'/libraries/user_b/history/attachment/{sha}', {}, USER_A_SESSION)
    assert st_att == 200 and mime == 'image/png' and payload == raw_bytes

    st_exp, _, exp_data = svc.dispatch('GET', '/libraries/user_b/experiences', {'origin': 'manual'}, USER_A_SESSION)
    assert st_exp == 200 and exp_data['total'] == 1
    assert 'deleted_total' not in exp_data.get('summary', {})
    assert svc.dispatch('GET', '/libraries/user_b/experiences', {'origin': 'manual', 'deleted': '1'}, USER_A_SESSION)[0] == 403
    assert svc.dispatch('GET', '/libraries/user_b/experiences', {'origin': 'manual', 'deleted': '1', 'inspect': '1'}, ADMIN_SESSION)[0] == 403

    svc.dispatch(
        'POST', '/grants',
        {'viewer_id': 'user_a', 'owner_id': 'user_b', 'history_read': False, 'experience_read': False, 'revision': 1},
        ADMIN_SESSION,
    )
    assert svc.dispatch('GET', f'/libraries/user_b/history/attachment/{sha}', {}, USER_A_SESSION)[0] == 403
    assert svc.dispatch('GET', '/libraries/user_b/experiences', {'origin': 'manual'}, USER_A_SESSION)[0] == 403
