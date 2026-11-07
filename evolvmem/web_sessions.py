"""Persistent site sessions. Browser cookies and provider tokens are never stored.

Explicit initialize() is the only schema/file creation path. Reads open a short
read-only connection and never renew expiry, prune records, or fall back to an
in-memory session. Account lifecycle code may revoke through its own transaction.
"""
from contextlib import contextmanager
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import time


class SessionStoreError(RuntimeError):
    def __init__(self, code='session_store_unavailable'):
        allowed={'session_store_unavailable','invalid_session_token',
                 'invalid_session_record','invalid_auth_scope','invalid_session_time',
                 'session_token_conflict','session_identity_rejected'}
        self.code=code if code in allowed else 'session_store_unavailable'
        super().__init__(self.code)


class SessionIdentityRejected(SessionStoreError):
    def __init__(self):
        super().__init__('session_identity_rejected')


def _now():
    return time.time()


def _token_hash(token):
    if (not isinstance(token,str) or not 1<=len(token)<=200
            or not token.isascii() or any(not 33<=ord(c)<=126 for c in token)):
        return None
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def _timestamp(value):
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:
        raise SessionStoreError('invalid_session_time')
    return float(value)


def _identity(value):
    if (not isinstance(value,str) or not 1<=len(value)<=200
            or not value.isascii() or any(not 33<=ord(c)<=126 for c in value)):
        raise SessionStoreError('invalid_session_record')
    return value


def _record(session):
    if not isinstance(session,dict):
        raise SessionStoreError('invalid_session_record')
    try:
        open_id=_identity(session['open_id'])
        name=session['name']
        csrf=session['csrf']
        if (not isinstance(name,str) or not 1<=len(name)<=100
                or any(ord(c)<32 for c in name) or _token_hash(csrf) is None):
            raise SessionStoreError('invalid_session_record')
        expires=_timestamp(session['expires'])
        return {'open_id':open_id,'name':name,'csrf':csrf,'expires':expires}
    except (KeyError,TypeError):
        raise SessionStoreError('invalid_session_record') from None


def revoke_identity(conn,open_id):
    """Delete sessions inside the caller's existing metadata transaction.

    No connection creation, table initialization, commit, rollback, or expiry
    renewal. Missing optional table is a no-op during pre-install migration.
    All auth scopes are revoked so an earlier app configuration cannot revive
    an identity's old cookies after a later account re-enable.
    """
    open_id=_identity(open_id)
    try:
        exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_sessions'").fetchone()
        if not exists:
            return 0
        return conn.execute('DELETE FROM web_sessions WHERE open_id=?',(open_id,)).rowcount
    except sqlite3.Error:
        raise SessionStoreError() from None


class PersistentSessions:
    def __init__(self,path,auth_scope,require_existing=False):
        if not isinstance(auth_scope,str) or not 1<=len(auth_scope)<=8192:
            raise SessionStoreError('invalid_auth_scope')
        if type(require_existing) is not bool:
            raise SessionStoreError()
        try:
            self.path=Path(path).expanduser().resolve()
        except (TypeError,ValueError,OSError):
            raise SessionStoreError() from None
        self.auth_scope=hashlib.sha256(auth_scope.encode('utf-8')).hexdigest()
        self.require_existing=require_existing

    @contextmanager
    def _connection(self,write=False):
        conn=None
        try:
            conn=sqlite3.connect(self.path.as_uri()+('?mode=rw' if write else '?mode=ro'),
                                 uri=True,timeout=5,isolation_level=None)
            conn.row_factory=sqlite3.Row
            if write:
                conn.execute('BEGIN IMMEDIATE')
            else:
                conn.execute('PRAGMA query_only=ON')
            yield conn
            if write:
                conn.commit()
        except SessionStoreError:
            if conn is not None and write:
                conn.rollback()
            raise
        except Exception:
            if conn is not None and write:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            raise SessionStoreError() from None
        finally:
            if conn is not None:
                conn.close()

    def initialize(self):
        """Explicit startup only; never called implicitly by get/put/pop."""
        try:
            if not self.path.is_file():
                if self.require_existing:
                    raise SessionStoreError()
                self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                try:
                    descriptor=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                except FileExistsError:
                    pass
                else:
                    os.close(descriptor)
            if os.name!='nt':
                os.chmod(self.path,0o600)
            with self._connection(write=True) as conn:
                conn.execute('''CREATE TABLE IF NOT EXISTS web_sessions(
                    token_hash TEXT PRIMARY KEY NOT NULL,
                    auth_scope TEXT NOT NULL,open_id TEXT NOT NULL,name TEXT NOT NULL,
                    csrf TEXT NOT NULL,expires_at REAL NOT NULL)''')
                conn.execute('CREATE INDEX IF NOT EXISTS web_sessions_identity_idx ON web_sessions(open_id)')
                conn.execute('CREATE INDEX IF NOT EXISTS web_sessions_expiry_idx ON web_sessions(expires_at)')
                conn.execute('SELECT token_hash,auth_scope,open_id,name,csrf,expires_at FROM web_sessions LIMIT 0')
        except SessionStoreError:
            raise
        except Exception:
            raise SessionStoreError() from None

    @staticmethod
    def _view(row):
        return _record({'open_id':row['open_id'],'name':row['name'],
                        'csrf':row['csrf'],'expires':row['expires_at']})

    def put(self,token,session,*,validate=None):
        digest=_token_hash(token)
        if digest is None:
            raise SessionStoreError('invalid_session_token')
        record=_record(session)
        if validate is not None and not callable(validate):
            raise SessionStoreError('invalid_session_record')
        with self._connection(write=True) as conn:
            # This runs after BEGIN IMMEDIATE on the same metadata DB as account
            # disable/rebind+revoke, closing the check/insert race for trusted
            # account validators without coupling this helper to a users schema.
            if validate is not None and not validate(conn,record['open_id']):
                raise SessionIdentityRejected()
            old=conn.execute('SELECT * FROM web_sessions WHERE token_hash=?',(digest,)).fetchone()
            if old:
                if old['auth_scope']!=self.auth_scope or self._view(old)!=record:
                    raise SessionStoreError('session_token_conflict')
                return
            conn.execute('INSERT INTO web_sessions VALUES(?,?,?,?,?,?)',
                (digest,self.auth_scope,record['open_id'],record['name'],record['csrf'],record['expires']))

    def get(self,token):
        digest=_token_hash(token)
        if digest is None:
            return None
        with self._connection() as conn:
            row=conn.execute('SELECT * FROM web_sessions WHERE token_hash=? AND auth_scope=?',
                             (digest,self.auth_scope)).fetchone()
            if row is None:
                return None
            record=self._view(row)
            return record if record['expires']>_now() else None

    def pop(self,token):
        digest=_token_hash(token)
        if digest is None:
            return None
        now=_timestamp(_now())
        with self._connection(write=True) as conn:
            row=conn.execute('SELECT * FROM web_sessions WHERE token_hash=? AND auth_scope=?',
                             (digest,self.auth_scope)).fetchone()
            # A cookie's global primary key must stay revoked even if an app
            # configuration changes and is later restored. Scope still limits
            # the returned identity; it must not limit logout's deletion.
            conn.execute('DELETE FROM web_sessions WHERE token_hash=?',(digest,))
            conn.execute('DELETE FROM web_sessions WHERE expires_at<=?',(now,))
            if row is None:
                return None
            record=self._view(row)
            return record if record['expires']>now else None

    def prune(self,now=None):
        boundary=_timestamp(_now() if now is None else now)
        with self._connection(write=True) as conn:
            return conn.execute('DELETE FROM web_sessions WHERE expires_at<=?',(boundary,)).rowcount

    def revoke_open_id(self,open_id):
        open_id=_identity(open_id)
        with self._connection(write=True) as conn:
            return revoke_identity(conn,open_id)
