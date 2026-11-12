"""One owned SQLite read and complete external-commit monitor for LAN recall."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import stat
import time
from evolvmem.context_temporal import TIMESTAMP_FORMAT,window_contains


class ExperienceReadError(RuntimeError):
    """Fixed content-free read failure; no automatic replay."""


def file_identity(path):
    path=Path(path)
    value=path.stat(); parent=path.parent.stat()
    if not stat.S_ISREG(value.st_mode):
        raise ExperienceReadError('experience_read_unavailable')
    return (str(path.resolve()),value.st_dev,value.st_ino,value.st_mode,value.st_uid,value.st_gid,
            parent.st_dev,parent.st_ino,parent.st_mode,parent.st_uid,parent.st_gid)


class ExperienceSnapshot:
    def __init__(self,path,*,expected,deadline,clock=time.monotonic,closed=lambda:False):
        self.path=Path(path);self.expected=expected;self.deadline=deadline
        self._clock=clock;self._runtime_closed=closed;self.closed=False;self.conn=None
        self._proof_checks=0
        self.observed_at=datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)
        try:
            self._guard()
            self.conn=sqlite3.connect(self.path.absolute().as_uri()+'?mode=ro',uri=True,
                                     timeout=.1,isolation_level=None)
            self.conn.row_factory=sqlite3.Row
            self.conn.execute('PRAGMA trusted_schema=OFF')
            self.conn.execute('PRAGMA query_only=ON')
            self.conn.execute('PRAGMA cache_size=-2048')
            self.conn.set_progress_handler(self._progress,1000)
            self.version=self._version()
            self.conn.execute('BEGIN')
            # Establish the actual snapshot now, not at a later model call.
            self.conn.execute('PRAGMA schema_version').fetchone()
            self._guard()
        except BaseException:
            self.close()
            raise

    def _progress(self):
        return self.closed or self._runtime_closed() or self._clock()>=self.deadline

    def _guard(self):
        if self.closed or self._runtime_closed():
            raise ExperienceReadError('experience_read_closed')
        if self._clock()>=self.deadline:
            raise ExperienceReadError('experience_read_deadline')
        try:current=file_identity(self.path)
        except (OSError,ValueError):
            raise ExperienceReadError('experience_read_unavailable') from None
        if current!=self.expected:
            raise ExperienceReadError('experience_read_changed')

    def _version(self):
        return self.conn.execute('PRAGMA data_version').fetchone()[0]

    def _rollback(self):
        # A fired deadline must not prevent retiring an already owned snapshot.
        self.conn.set_progress_handler(None,0)
        try:
            self.conn.rollback()
            if self.conn.in_transaction:
                raise ExperienceReadError('experience_read_cleanup_failed')
        finally:
            if not self.closed:self.conn.set_progress_handler(self._progress,1000)

    def unchanged(self):
        if self.changed():raise ExperienceReadError('experience_read_changed')

    def changed(self):
        self._guard()
        if self.conn.in_transaction or self.conn.total_changes:
            raise ExperienceReadError('experience_read_changed')
        return self._version()!=self.version

    def validate(self,ids,*,public=False,now=None,proof=None):
        """Fresh selected check; changed commits require bounded dependency proof."""
        self._rollback()
        if (not isinstance(ids,tuple) or len(ids)>23 or len(set(ids))!=len(ids)
                or any(type(i) is not int or i<=0 for i in ids)):
            raise ExperienceReadError('experience_read_unavailable')
        for attempt in range(2):
            self._guard()
            before=self._version();changed=before!=self.version
            if changed and (proof is None or self._proof_checks>=2):
                raise ExperienceReadError('experience_read_changed')
            rows=[]
            try:
                self.conn.execute('BEGIN')
                if ids:
                    placeholders=','.join('?' for _ in ids)
                    rows=self.conn.execute('SELECT id,status,expires_at,effective_from,effective_until FROM context_items '
                        f'WHERE id IN ({placeholders})',ids).fetchall()
                    if public:
                        visible=self.conn.execute('SELECT context_id FROM lan_publications '
                            f'WHERE active=1 AND context_id IN ({placeholders})',ids).fetchall()
                        if {row[0] for row in visible}!=set(ids):
                            raise ExperienceReadError('experience_read_changed')
                moment=(now() if callable(now) else now) or datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)
                if (len(rows)!=len(ids) or any(row['status']!='active'
                        or (row['expires_at'] is not None and row['expires_at']<=moment)
                        or not window_contains(row['effective_from'],row['effective_until'],moment) for row in rows)):
                    raise ExperienceReadError('experience_read_changed')
                if proof is not None:
                    proof.check_time(moment)
                    if changed:
                        self._proof_checks+=1
                        proof.check()
            finally:
                self._rollback()
            self._guard()
            if self._version()==before:
                self.version=before
                return
            if proof is None:break
        raise ExperienceReadError('experience_read_changed')

    def close(self):
        if self.closed:return
        self.closed=True
        if self.conn is not None:
            try:
                self.conn.set_progress_handler(None,0)
                self.conn.rollback()
            finally:
                self.conn.close()
