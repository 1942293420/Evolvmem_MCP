"""Task 1.1b (PG Plan Task 2): PG Web sessions, Project ACL, device scoping, connection reuse, and fail-closed routing."""
from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

import psycopg
import pytest

from evolvmem.library_catalog import AclIntent, ControlBinding, ControlCatalog, ControlProvider
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.library_foundation import (
    Authenticator,
    AuthorizedRoute,
    ConnectionBinding,
    FoundationError,
    LibraryDescriptor,
    RequestIdentity,
)
from evolvmem.library_foundation.sessions import LibrarySession
from evolvmem.library_project_acl import ProjectAclCoordinator, ProjectAclIntent, project_uuid
from evolvmem.library_project_acl.scope import resolve_project_scope
from evolvmem.library_project_acl.worker import AclProjector, ProjectionTarget
from evolvmem.library_startup import FileSecretResolver
from evolvmem.manual_experience_mcp import ManualMcpError, registered_source
from tests.stage1.pg_stage1_harness import (
    TEST_PG_HOST,
    TEST_PG_PORT,
    create_isolated_content_db,
    create_isolated_control_db,
    drop_isolated_db,
    superuser_password,
)
from tests.stage1.test_task11_identity_acl import (
    ADMIN_SESSION,
    USER_A_SESSION,
    build_pg_console,
    init_sqlite_source,
)


@pytest.fixture()
def pg_control_env(tmp_path):
    env = create_isolated_control_db(tmp_path, up_to_version=7)
    try:
        yield env
    finally:
        drop_isolated_db(env.database, (env.coordinator_role, env.reader_role, env.owner_role))


def test_pg_web_sessions_8h_ttl_restart_sso_no_oauth_tokens_and_atomic_disable(pg_control_env, tmp_path, monkeypatch):
    from evolvmem.console_access import console_user_gate
    from evolvmem.experience_v2.control.migrations import loopback_psycopg_connector
    from evolvmem.experience_v2.control.pg_web_sessions import PgPersistentSessions
    from evolvmem.web_auth import _SESSION_SECONDS

    assert _SESSION_SECONDS == 8 * 3600
    svc, store, cfg = build_pg_console(pg_control_env, tmp_path)

    sessions_1 = PgPersistentSessions(
        store, 'https://evolvmem.local|app-1|tenant-1', expected_owner_id='admin_owner', console_mode=True
    )
    sessions_1.initialize()
    raw_cookie = 'session-cookie-user-a-secret-token-12345'
    fixed_expiry = time.time() + _SESSION_SECONDS
    sessions_1.put(
        raw_cookie,
        {'open_id': USER_A_SESSION['open_id'], 'name': 'UserA', 'csrf': 'csrf-tok-a', 'expires': fixed_expiry},
    )

    got_1 = sessions_1.get(raw_cookie)
    assert got_1 is not None and abs(got_1['expires'] - fixed_expiry) < 1.0
    sessions_after_restart = PgPersistentSessions(
        cfg, 'https://evolvmem.local|app-1|tenant-1', expected_owner_id='admin_owner', console_mode=True
    )
    got_2 = sessions_after_restart.get(raw_cookie)
    assert got_2 is not None and got_2['expires'] == got_1['expires']

    with psycopg.connect(
        host=TEST_PG_HOST, port=TEST_PG_PORT, dbname=pg_control_env.database,
        user='postgres', password=superuser_password(), sslmode='disable',
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='em_control' AND column_name ~* '(access_token|refresh_token|oauth_token)'"
            )
            assert cur.fetchall() == []
            cur.execute('SELECT count(*) FROM em_control.web_sessions WHERE token_hash = %s', (raw_cookie,))
            assert cur.fetchone()[0] == 0

    bearer_token = 'lan-bearer-token-user-a-0001'
    dev_id = str(uuid.uuid4())
    store.register_trusted_device(
        'user_a', device_id=dev_id, external_device_id='workstation-a', token=bearer_token
    )
    catalog = ControlCatalog(
        ControlProvider(
            ControlBinding(cfg.host, cfg.port, cfg.database, cfg.reader_role, cfg.reader_password_ref),
            loopback_psycopg_connector(allow_loopback_disable_ssl=True),
            FileSecretResolver(cfg.secret_files),
        )
    )
    authn = Authenticator(catalog)
    ident_before = authn.authenticate(bearer_token)
    assert ident_before.device_id == dev_id

    monkeypatch.setenv('EVOLVMEM_CONTROL_BACKEND', 'postgres')
    monkeypatch.setenv('EVOLVMEM_CONSOLE_PG_CONFIG', str(pg_control_env.config_file))
    monkeypatch.setenv('EVOLVMEM_CONSOLE_OWNER_ID', 'admin_owner')
    policy_path = tmp_path / 'policy.json'
    policy_path.write_text(
        json.dumps({'console_enabled': True, 'legacy_web_enabled': False, 'ai_processing_enabled': False}),
        encoding='utf-8',
    )
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(policy_path))
    lan_settings = SimpleNamespace(
        data_dir=tmp_path / 'lan',
        owner_data_dir=tmp_path / 'sources' / 'admin_owner',
    )
    assert console_user_gate('user_a', lan_settings) is None

    st_dis, _, _ = svc.dispatch('POST', '/users/user_a', {'enabled': False, 'revision': 1}, ADMIN_SESSION)
    assert st_dis == 200
    assert sessions_after_restart.get(raw_cookie) is None
    with pytest.raises(FoundationError, match='unauthorized'):
        authn.authenticate(bearer_token)
    assert console_user_gate('user_a', lan_settings) == (403, {'error': 'account_disabled'})


def test_project_acl_and_device_credential_project_scoping(pg_control_env, tmp_path):
    from evolvmem.experience_v2.control.migrations import loopback_psycopg_connector

    svc, store, cfg = build_pg_console(pg_control_env, tmp_path)
    src_b = init_sqlite_source(tmp_path / 'sources' / 'user_b')
    svc.configure_library('user_b', data_dir=src_b)

    info_a = store.get_user_source('admin_owner', 'user_a')
    info_b = store.get_user_source('admin_owner', 'user_b')
    dev_owner_b, cred_owner_b = str(uuid.uuid4()), str(uuid.uuid4())
    store.register_trusted_device(
        'user_b', device_id=dev_owner_b, external_device_id='b-admin-box',
        token='tok-owner-b-coord', credential_id=cred_owner_b, unrestricted_own_projects=True,
    )
    dev_viewer_a, cred_viewer_a = str(uuid.uuid4()), str(uuid.uuid4())
    store.register_trusted_device(
        'user_a', device_id=dev_viewer_a, external_device_id='a-reader-box',
        token='tok-viewer-a', credential_id=cred_viewer_a,
    )
    dev_scoped_b, cred_scoped_b = str(uuid.uuid4()), str(uuid.uuid4())
    store.register_trusted_device(
        'user_b', device_id=dev_scoped_b, external_device_id='b-scoped-laptop',
        token='tok-scoped-b', credential_id=cred_scoped_b, unrestricted_own_projects=False,
    )

    pg_lib_id = str(uuid.uuid4())
    conn_ref = 'content.user_b_pg'
    content_env = create_isolated_content_db(
        library_id=pg_lib_id,
        connection_ref=conn_ref,
        owner_principal_id=info_b['principal_id'],
        runtime_principals=[
            ('b', info_b['principal_id'], cred_owner_b, dev_owner_b, 1),
            ('sb', info_b['principal_id'], cred_scoped_b, dev_scoped_b, 1),
            ('a', info_a['principal_id'], cred_viewer_a, dev_viewer_a, 1),
        ],
    )
    try:
        store.register_pg_active_library(
            owner_user_id='user_b',
            library_id=pg_lib_id,
            connection_ref=conn_ref,
            projects=('proj-alpha', 'proj-beta'),
        )
        proj_pw_file = tmp_path / 'proj.pw'
        proj_pw_file.write_text(content_env.projector_pw + '\n', encoding='utf-8')
        os.chmod(proj_pw_file, 0o600)
        secrets = {**cfg.secret_files, 'content.proj': str(proj_pw_file)}
        for label, (r_name, r_pw) in content_env.runtime_roles.items():
            f = tmp_path / f'rt_{label}.pw'
            f.write_text(r_pw + '\n', encoding='utf-8')
            os.chmod(f, 0o600)
            secrets[f'rt.{label}'] = str(f)

        conn_fn = loopback_psycopg_connector(allow_loopback_disable_ssl=True)
        resolver = FileSecretResolver(secrets)
        coord_provider = ControlProvider(
            ControlBinding(cfg.host, cfg.port, cfg.database, cfg.coordinator_role, cfg.coordinator_password_ref),
            conn_fn, resolver,
        )
        rt_principal_map = {
            'b': (info_b['principal_id'], cred_owner_b, dev_owner_b),
            'sb': (info_b['principal_id'], cred_scoped_b, dev_scoped_b),
            'a': (info_a['principal_id'], cred_viewer_a, dev_viewer_a),
        }
        target = ProjectionTarget(
            library_id=pg_lib_id,
            connection_ref=conn_ref,
            connection=ControlBinding(
                TEST_PG_HOST, TEST_PG_PORT, content_env.database, content_env.projector_role, 'content.proj'
            ),
            runtime_bindings=tuple(
                ConnectionBinding(
                    conn_ref, pg_lib_id, *rt_principal_map[label],
                    TEST_PG_HOST, TEST_PG_PORT, content_env.database, r_name, f'rt.{label}'
                )
                for label, (r_name, _) in content_env.runtime_roles.items()
            ),
        )
        projector = AclProjector(coord_provider, (target,), conn_fn, resolver)
        proj_coord = ProjectAclCoordinator(coord_provider)

        ident_b = RequestIdentity(info_b['principal_id'], cred_owner_b, dev_owner_b, 1, 1, 1)
        ident_a = RequestIdentity(info_a['principal_id'], cred_viewer_a, dev_viewer_a, 1, 1, 1)

        op1 = AclIntent(ident_b, pg_lib_id, info_a['principal_id'], 'read', 1, str(uuid.uuid4()), 'grant-lib-read')
        proj_coord.apply(op1, projector)
        op2 = ProjectAclIntent(
            ident_b, pg_lib_id, info_a['principal_id'], 'read', 2, str(uuid.uuid4()), 'grant-proj-alpha',
            project_uuid('proj-alpha'), 'proj-alpha',
        )
        proj_coord.apply(op2, projector)

        def _check_scope_as_a(policy_epoch: int, project_key: str):
            r_name, r_pw = content_env.runtime_roles['a']
            lib_desc = LibraryDescriptor(
                pg_lib_id, 'legacy', 'postgres', conn_ref, policy_epoch, 1, 'pg_active', 'ready', 'postgres',
                owner_principal_id=info_b['principal_id'], active=True,
            )
            route = AuthorizedRoute(ident_a, lib_desc, 'context_search', 'read')
            with conn_fn(
                host=TEST_PG_HOST, port=TEST_PG_PORT, dbname=content_env.database,
                user=r_name, password=r_pw, autocommit=True, sslmode='verify-full',
            ) as raw:
                with raw.cursor() as cur:
                    cur.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')

                class _SimpleAdapter:
                    def execute(self, sql, params=()):
                        with raw.cursor() as c:
                            c.execute(sql, params)
                            return c.fetchall()

                sess = LibrarySession(route, _SimpleAdapter())
                return resolve_project_scope(sess, project_key)

        scope_alpha = _check_scope_as_a(3, 'proj-alpha')
        assert scope_alpha.project_key == 'proj-alpha'
        with pytest.raises(Exception):
            _check_scope_as_a(3, 'proj-beta')

        op3 = ProjectAclIntent(
            ident_b, pg_lib_id, info_a['principal_id'], None, 3, str(uuid.uuid4()), 'revoke-proj-alpha',
            project_uuid('proj-alpha'), 'proj-alpha',
        )
        proj_coord.apply(op3, projector)
        with pytest.raises(Exception):
            _check_scope_as_a(4, 'proj-alpha')

        ident_scoped_b = RequestIdentity(info_b['principal_id'], cred_scoped_b, dev_scoped_b, 1, 1, 1)
        assert store.resolve_device_project_access(ident_scoped_b, pg_lib_id, 'proj-alpha', 'read') is False
        assert store.resolve_device_project_access(ident_scoped_b, pg_lib_id, 'proj-beta', 'read') is False
        store.grant_device_project(dev_scoped_b, pg_lib_id, 'proj-alpha', permission='write', active=True)
        assert store.resolve_device_project_access(ident_scoped_b, pg_lib_id, 'proj-alpha', 'write') is True
        assert store.resolve_device_project_access(ident_scoped_b, pg_lib_id, 'proj-beta', 'read') is False
    finally:
        drop_isolated_db(
            content_env.database,
            (content_env.projector_role, *[r for r, _ in content_env.runtime_roles.values()], content_env.owner_role),
        )


def test_pooled_connection_cleanup_no_residual_identity(pg_control_env):
    from evolvmem.experience_v2.control.migrations import loopback_psycopg_connector

    conn_fn = loopback_psycopg_connector(allow_loopback_disable_ssl=True)
    resolver = FileSecretResolver(pg_control_env.secret_files)
    guard = SyncRequestDeadlineGuard(timeout_seconds=10.0)
    provider = ControlProvider(
        ControlBinding(
            TEST_PG_HOST, TEST_PG_PORT, pg_control_env.database,
            pg_control_env.reader_role, 'control.reader',
        ),
        conn_fn,
        resolver,
        deadline_guard=guard,
        control_request_reuse=True,
    )
    with guard.enforce():
        try:
            with provider.open(read_only=True) as s1:
                pid_1 = s1.execute('SELECT pg_backend_pid() AS pid')[0]['pid']
                s1.execute("SELECT set_config('evolvmem.test_residual', 'user_a_secret', false)")
                assert s1.execute("SELECT current_setting('evolvmem.test_residual', true) AS v")[0]['v'] == 'user_a_secret'
            with provider.open(read_only=True) as s2:
                pid_2 = s2.execute('SELECT pg_backend_pid() AS pid')[0]['pid']
                residual = s2.execute("SELECT current_setting('evolvmem.test_residual', true) AS v")[0]['v']
                assert pid_1 == pid_2, 'physical connection should have been reused'
                assert residual in (None, ''), f'DISCARD ALL must clear session GUCs, got {residual!r}'
        finally:
            provider.close_reuse()


def test_transitional_sqlite_content_routing_and_fail_closed_pg_unavailable(pg_control_env, tmp_path, monkeypatch):
    from evolvmem.config import Config
    from evolvmem.console_access import console_user_gate
    from evolvmem.context_models import ContextMode
    from evolvmem.context_service import ContextService
    from evolvmem.experience_v2.control.pg_web_sessions import PgPersistentSessions
    from evolvmem.knowledge_console import ConsoleService
    from evolvmem.memory_store import MemoryStore
    from evolvmem.web_auth import AuthSettings, WebAuth
    from evolvmem.web_server import make_handler
    from evolvmem.web_sessions import PersistentSessions

    svc, store, _ = build_pg_console(pg_control_env, tmp_path)
    owner_dir = init_sqlite_source(tmp_path / 'sources' / 'admin_owner')
    svc.configure_library('admin_owner', data_dir=owner_dir)
    src_meta = store.get_user_source('admin_owner', 'admin_owner')
    assert src_meta['backend'] == 'sqlite'
    assert src_meta['authority_state'] == 'sqlite_active'
    assert src_meta['write_backend'] == 'sqlite'

    decoy_sqlite = tmp_path / 'decoy' / 'users.db'
    decoy_svc = ConsoleService(decoy_sqlite, 'admin_owner', owner_dir, tmp_path / 'lan', {})
    decoy_svc.bootstrap(ADMIN_SESSION['open_id'], 'DecoyOwner')
    auth_file = owner_dir / 'web_auth.json'
    auth_file.write_text(
        json.dumps({
            'enabled': True, 'app_id': 'cli_test', 'app_secret': 'sec_test',
            'redirect_uri': 'http://127.0.0.1:19377/auth/feishu/callback',
            'owner_open_id': ADMIN_SESSION['open_id'], 'owner_only': False,
        }),
        encoding='utf-8',
    )
    os.chmod(auth_file, 0o600)
    scope = WebAuth(AuthSettings.load(owner_dir)).persistence_scope()
    cookie_tok = 'cookie-transitional-token-0001'
    decoy_sessions = PersistentSessions(decoy_sqlite, scope, require_existing=True)
    decoy_sessions.initialize()
    decoy_sessions.put(cookie_tok, {**ADMIN_SESSION, 'expires': time.time() + 3600})

    pg_sessions = PgPersistentSessions(store, scope, expected_owner_id='admin_owner', console_mode=True)
    pg_sessions.initialize()
    pg_sessions.put(cookie_tok, {**ADMIN_SESSION, 'expires': time.time() + 3600})

    policy_file = tmp_path / 'policy.json'
    policy_file.write_text(
        json.dumps({'console_enabled': True, 'legacy_web_enabled': False, 'ai_processing_enabled': False}),
        encoding='utf-8',
    )

    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(policy_file))
    monkeypatch.setenv('EVOLVMEM_CONSOLE_REGISTRY', str(decoy_sqlite))
    monkeypatch.setenv('EVOLVMEM_CONSOLE_OWNER_ID', 'admin_owner')
    monkeypatch.setenv('EVOLVMEM_CONTROL_BACKEND', 'postgres')
    monkeypatch.setenv('EVOLVMEM_CONSOLE_PG_CONFIG', str(pg_control_env.config_file))

    app_cfg = Config(data_dir=owner_dir, apply_environment=False)
    with MemoryStore(app_cfg):
        pass
    ctx = ContextService(app_cfg)
    ctx.initialize(mode=ContextMode.SHADOW, adapter='codex')
    try:
        handler_cls = make_handler(ctx)
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler_cls)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            req = urllib.request.Request(
                f'http://127.0.0.1:{port}/api/knowledge-console/me',
                headers={'Cookie': f'evolvmem_session={cookie_tok}'},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                payload = json.loads(resp.read())
                assert payload['current_user']['id'] == 'admin_owner'
        finally:
            httpd.shutdown()
            httpd.server_close()

        broken_cfg_file = tmp_path / 'broken_pg.json'
        broken_data = json.loads(pg_control_env.config_file.read_text(encoding='utf-8'))
        broken_data['port'] = 59999
        broken_cfg_file.write_text(json.dumps(broken_data), encoding='utf-8')
        os.chmod(broken_cfg_file, 0o600)
        monkeypatch.setenv('EVOLVMEM_CONSOLE_PG_CONFIG', str(broken_cfg_file))

        handler_broken = make_handler(ctx)
        httpd2 = ThreadingHTTPServer(('127.0.0.1', 0), handler_broken)
        port2 = httpd2.server_address[1]
        thread2 = threading.Thread(target=httpd2.serve_forever, daemon=True)
        thread2.start()
        try:
            req2 = urllib.request.Request(
                f'http://127.0.0.1:{port2}/api/knowledge-console/me',
                headers={'Cookie': f'evolvmem_session={cookie_tok}'},
            )
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req2, timeout=5)
            assert exc_info.value.code == 503
            err_body = json.loads(exc_info.value.read())
            assert err_body['error'] == 'pg_control_unavailable'
            assert 'PG 不可用' in err_body.get('message', '')
        finally:
            httpd2.shutdown()
            httpd2.server_close()

        lan_settings = SimpleNamespace(data_dir=tmp_path / 'lan', owner_data_dir=owner_dir)
        gate_status, gate_payload = console_user_gate('admin_owner', lan_settings)
        assert gate_status == 503
        assert gate_payload['error'] == 'pg_control_unavailable'
        assert 'PG 不可用' in gate_payload.get('message', '')

        with pytest.raises(ManualMcpError, match='pg_control_unavailable'):
            registered_source(owner_dir, owner_data_dir=owner_dir, user='admin_owner')
    finally:
        ctx.close()
