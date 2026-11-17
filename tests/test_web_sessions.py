"""Persistent site-session contract; every database is a synthetic temp fixture."""
import concurrent.futures
import hashlib
import importlib.util
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

SOURCE=Path(__file__).with_name('web_sessions.py')
if SOURCE.exists():
    spec=importlib.util.spec_from_file_location('sessions_under_test',SOURCE)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
else:
    try:
        from evolvmem import web_sessions as module
    except ImportError:
        module=None

COOKIE='synthetic-cookie-'+('A'*43)
SCOPE='synthetic-app-configuration-binding'
IDENTITY='synthetic-user-open-id'


def session(expires=1000+8*60*60,**changes):
    return {'open_id':IDENTITY,'name':'Synthetic User','csrf':'synthetic-csrf','expires':expires,**changes}


@pytest.fixture
def store(tmp_path,monkeypatch):
    assert module is not None,'PersistentSessions implementation is missing'
    monkeypatch.setattr(module,'_now',lambda:1000)
    value=module.PersistentSessions(tmp_path/'metadata'/'users.db',SCOPE)
    value.initialize()
    return value


def test_restart_and_independent_instances_read_same_session(store):
    store.put(COOKIE,session())
    second=module.PersistentSessions(store.path,SCOPE,require_existing=True)
    second.initialize()
    assert second.get(COOKIE)==session()
    assert second.pop(COOKIE)==session()
    assert store.get(COOKIE) is None and second.pop(COOKIE) is None


def test_only_cookie_and_configuration_hashes_are_stored(store):
    store.put(COOKIE,session())
    with sqlite3.connect(store.path) as conn:
        conn.row_factory=sqlite3.Row
        row=dict(conn.execute('SELECT * FROM web_sessions').fetchone())
    assert row['token_hash']==hashlib.sha256(COOKIE.encode()).hexdigest()
    assert row['auth_scope']==hashlib.sha256(SCOPE.encode()).hexdigest()
    assert COOKIE.encode() not in store.path.read_bytes()
    assert SCOPE.encode() not in store.path.read_bytes()
    assert set(row)=={'token_hash','auth_scope','open_id','name','csrf','expires_at'}


def test_get_is_readonly_and_fixed_eight_hour_expiry_does_not_slide(store,monkeypatch):
    expires=1000+8*60*60
    store.put(COOKIE,session(expires))
    before=store.path.read_bytes()
    monkeypatch.setattr(module,'_now',lambda:expires-1)
    assert store.get(COOKIE)['expires']==expires
    monkeypatch.setattr(module,'_now',lambda:expires)
    assert store.get(COOKIE) is None
    assert store.path.read_bytes()==before


def test_scope_change_cannot_read_other_configuration_session(store):
    store.put(COOKIE,session())
    other=module.PersistentSessions(store.path,'synthetic-other-configuration',require_existing=True)
    other.initialize()
    assert other.get(COOKIE) is None
    assert store.get(COOKIE)==session()


def test_logout_after_scope_change_permanently_revokes_cookie_when_scope_restored(store):
    store.put(COOKIE,session())
    changed=module.PersistentSessions(store.path,'synthetic-changed-app-scope',require_existing=True)
    changed.initialize()
    assert changed.get(COOKIE) is None
    assert changed.pop(COOKIE) is None
    restored=module.PersistentSessions(store.path,SCOPE,require_existing=True)
    assert restored.get(COOKIE) is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('SELECT count(*) FROM web_sessions WHERE token_hash=?',
            (hashlib.sha256(COOKIE.encode()).hexdigest(),)).fetchone()[0]==0


def test_same_cookie_cannot_be_rewritten_to_slide_expiry(store):
    store.put(COOKIE,session())
    store.put(COOKIE,session())
    with pytest.raises(module.SessionStoreError) as error:
        store.put(COOKIE,session(expires=session()['expires']+100))
    assert str(error.value)=='session_token_conflict'
    assert store.get(COOKIE)==session()


def test_extra_provider_token_field_is_never_persisted(store):
    provider='synthetic-provider-token-never-save'
    store.put(COOKIE,session(access_token=provider,refresh_token=provider))
    assert store.get(COOKIE)==session()
    assert provider.encode() not in store.path.read_bytes()


def test_identity_revoke_immediate_across_instances_and_scopes(store):
    store.put(COOKIE,session())
    second=module.PersistentSessions(store.path,'synthetic-other-configuration',require_existing=True)
    second.initialize()
    second.put('synthetic-other-cookie',session())
    assert store.revoke_open_id(IDENTITY)==2
    assert store.get(COOKIE) is None and second.get('synthetic-other-cookie') is None
    assert store.revoke_open_id(IDENTITY)==0


def test_transaction_revocation_obeys_callers_commit_and_rollback(store):
    store.put(COOKIE,session())
    with sqlite3.connect(store.path) as conn:
        conn.execute('CREATE TABLE synthetic_accounts(id TEXT PRIMARY KEY,enabled INTEGER)')
        conn.execute('INSERT INTO synthetic_accounts VALUES(?,1)',(IDENTITY,))
    conn=sqlite3.connect(store.path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE synthetic_accounts SET enabled=0 WHERE id=?',(IDENTITY,))
        assert module.revoke_identity(conn,IDENTITY)==1
        conn.rollback()
        assert store.get(COOKIE)==session()
        assert conn.execute('SELECT enabled FROM synthetic_accounts').fetchone()[0]==1
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE synthetic_accounts SET enabled=0 WHERE id=?',(IDENTITY,))
        assert module.revoke_identity(conn,IDENTITY)==1
        conn.commit()
        assert store.get(COOKIE) is None
        assert conn.execute('SELECT enabled FROM synthetic_accounts').fetchone()[0]==0
    finally:
        conn.close()


def test_transaction_revocation_missing_optional_table_never_initializes(tmp_path):
    assert module is not None,'PersistentSessions implementation is missing'
    with sqlite3.connect(tmp_path/'synthetic.db') as conn:
        conn.execute('CREATE TABLE synthetic_accounts(id TEXT)')
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO synthetic_accounts VALUES(?)',(IDENTITY,))
        assert module.revoke_identity(conn,IDENTITY)==0
        assert conn.in_transaction
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='web_sessions'").fetchone()
        conn.rollback()
        assert conn.execute('SELECT count(*) FROM synthetic_accounts').fetchone()[0]==0


def test_pop_atomically_removes_cookie_and_prunes_expired_sessions(store):
    store.put(COOKIE,session())
    store.put('synthetic-expired-cookie',session(999))
    assert store.pop(COOKIE)==session()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('SELECT count(*) FROM web_sessions').fetchone()[0]==0


def test_prune_uses_explicit_boundary_without_extending_remaining_sessions(store):
    store.put(COOKIE,session(1500))
    store.put('synthetic-later-cookie',session(2000))
    assert store.prune(now=1500)==1
    assert store.get(COOKIE) is None
    assert store.get('synthetic-later-cookie')['expires']==2000


@pytest.mark.parametrize('token',[None,'','x'*201,'non-ascii-\xe9','line\nbreak',{},123])
def test_invalid_cookie_has_no_database_side_effects(store,token):
    before=store.path.read_bytes()
    assert store.get(token) is None and store.pop(token) is None
    with pytest.raises(module.SessionStoreError):
        store.put(token,session())
    assert store.path.read_bytes()==before


def test_unknown_cookie_returns_none(store):
    assert store.get('synthetic-unknown-cookie') is None
    assert store.pop('synthetic-unknown-cookie') is None


def test_require_existing_and_read_never_create_missing_store(tmp_path):
    assert module is not None,'PersistentSessions implementation is missing'
    path=tmp_path/'missing'/'users.db'
    store=module.PersistentSessions(path,SCOPE,require_existing=True)
    for operation in (store.initialize,lambda:store.get(COOKIE),lambda:store.pop(COOKIE)):
        with pytest.raises(module.SessionStoreError):
            operation()
        assert not path.exists() and not path.parent.exists()


def test_default_store_also_requires_explicit_initialize_before_valid_writes(tmp_path):
    assert module is not None,'PersistentSessions implementation is missing'
    path=tmp_path/'missing'/'sessions.db'
    store=module.PersistentSessions(path,SCOPE)
    with pytest.raises(module.SessionStoreError):
        store.put(COOKIE,session())
    assert not path.exists() and not path.parent.exists()


def test_corrupt_or_uninitialized_file_fails_closed_with_bounded_error(tmp_path):
    assert module is not None,'PersistentSessions implementation is missing'
    path=tmp_path/'synthetic-corrupt.db'
    path.write_bytes(b'synthetic invalid sqlite')
    store=module.PersistentSessions(path,SCOPE,require_existing=True)
    for operation in (store.initialize,lambda:store.get(COOKIE),lambda:store.put(COOKIE,session())):
        with pytest.raises(module.SessionStoreError) as error:
            operation()
        assert str(error.value)=='session_store_unavailable'
        assert str(path) not in str(error.value)
    empty=tmp_path/'synthetic-empty.db'
    with sqlite3.connect(empty):
        pass
    uninitialized=module.PersistentSessions(empty,SCOPE,require_existing=True)
    with pytest.raises(module.SessionStoreError):
        uninitialized.get(COOKIE)


def test_initialize_preserves_existing_metadata_and_sets_private_permissions(tmp_path):
    assert module is not None,'PersistentSessions implementation is missing'
    path=tmp_path/'synthetic-existing.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE synthetic_accounts(id TEXT PRIMARY KEY,value TEXT)')
        conn.execute('INSERT INTO synthetic_accounts VALUES(?,?)',(IDENTITY,'retained'))
    store=module.PersistentSessions(path,SCOPE,require_existing=True)
    store.initialize()
    store.initialize()
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT value FROM synthetic_accounts').fetchone()[0]=='retained'
    assert stat.S_IMODE(path.stat().st_mode)==0o600


def test_threads_use_independent_short_lived_connections(store):
    def save(index):
        token='synthetic-thread-cookie-'+str(index)
        other=module.PersistentSessions(store.path,SCOPE,require_existing=True)
        other.put(token,session(name='Synthetic '+str(index)))
        return other.get(token)['name']
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(save,range(16)))==['Synthetic '+str(i) for i in range(16)]


def test_default_error_is_bounded_and_allows_no_argument_construction():
    assert module is not None,'PersistentSessions implementation is missing'
    assert str(module.SessionStoreError())=='session_store_unavailable'


def _accounts(store,enabled=1):
    with sqlite3.connect(store.path) as conn:
        conn.execute('CREATE TABLE synthetic_accounts(id TEXT PRIMARY KEY,enabled INTEGER)')
        conn.execute('INSERT INTO synthetic_accounts VALUES(?,?)',(IDENTITY,enabled))


def _enabled(conn,openid):
    row=conn.execute('SELECT enabled FROM synthetic_accounts WHERE id=?',(openid,)).fetchone()
    return bool(row and row[0])


def test_validator_rejects_identity_disabled_before_put_without_writing(store):
    _accounts(store,enabled=0)
    before=store.path.read_bytes()
    with pytest.raises(module.SessionIdentityRejected) as error:
        store.put(COOKIE,session(),validate=_enabled)
    assert str(error.value)=='session_identity_rejected'
    assert store.get(COOKIE) is None
    assert store.path.read_bytes()==before


def test_validator_false_or_error_rolls_back_entire_put_transaction(store):
    _accounts(store)
    def rejected(conn,openid):
        conn.execute('UPDATE synthetic_accounts SET enabled=0')
        return False
    with pytest.raises(module.SessionIdentityRejected):
        store.put(COOKIE,session(),validate=rejected)
    def broken(conn,openid):
        conn.execute('UPDATE synthetic_accounts SET enabled=0')
        raise RuntimeError('synthetic private error detail')
    with pytest.raises(module.SessionStoreError) as error:
        store.put(COOKIE,session(),validate=broken)
    assert str(error.value)=='session_store_unavailable'
    with sqlite3.connect(store.path) as conn:
        assert conn.execute('SELECT enabled FROM synthetic_accounts').fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM web_sessions').fetchone()[0]==0


def test_disable_holding_write_lock_precedes_validator_and_rejects_put(store):
    _accounts(store)
    started=threading.Event()
    validated=threading.Event()
    def validator(conn,openid):
        validated.set()
        return _enabled(conn,openid)
    def login():
        started.set()
        store.put(COOKIE,session(),validate=validator)
    conn=sqlite3.connect(store.path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE synthetic_accounts SET enabled=0')
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(login)
            assert started.wait(1)
            assert not validated.wait(0.1)
            conn.commit()
            with pytest.raises(module.SessionIdentityRejected):
                future.result(timeout=2)
        assert validated.is_set() and store.get(COOKIE) is None
    finally:
        conn.close()


def test_put_holding_write_lock_is_followed_by_disable_and_revocation(store):
    _accounts(store)
    validated=threading.Event()
    allow_login=threading.Event()
    disable_started=threading.Event()
    disable_done=threading.Event()
    def validator(conn,openid):
        allowed=_enabled(conn,openid)
        validated.set()
        assert allow_login.wait(2)
        return allowed
    def disable():
        disable_started.set()
        with sqlite3.connect(store.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('UPDATE synthetic_accounts SET enabled=0')
            module.revoke_identity(conn,IDENTITY)
        disable_done.set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        login=pool.submit(store.put,COOKIE,session(),validate=validator)
        assert validated.wait(1)
        stop=pool.submit(disable)
        assert disable_started.wait(1) and not disable_done.wait(0.1)
        allow_login.set()
        login.result(timeout=2)
        stop.result(timeout=2)
    assert store.get(COOKIE) is None
