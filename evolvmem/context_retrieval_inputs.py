"""Bounded raw inputs for one explicitly owned, read-only retrieval snapshot.

The caller owns BEGIN/ROLLBACK. This scope never starts/ends a transaction and
must end before final selected proofs. Eligibility, source short circuits and
time judgments remain canonical and run on every call.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import fields, is_dataclass
import math
import sqlite3
import threading

MAX_ENTRIES = 1024
MAX_BYTES = 8 * 1024 * 1024
_SCOPES = ContextVar('context_retrieval_inputs', default=())


class RetrievalInputError(RuntimeError):
    """Fixed, content-free snapshot boundary failure."""


def _stamp(conn):
    return (id(conn), conn.in_transaction, conn.total_changes,
            conn.execute('PRAGMA query_only').fetchone()[0],
            conn.execute('PRAGMA data_version').fetchone()[0],
            conn.execute('PRAGMA schema_version').fetchone()[0])


def _weight(value, budget):
    """Conservative serialized weight; never encode/copy unbounded raw text."""
    remaining = budget

    def visit(item, depth=0):
        nonlocal remaining
        if depth > 32 or remaining < 0:
            return False
        if isinstance(item, str):
            remaining -= 6 * len(item) + 2
        elif item is None or type(item) is bool:
            remaining -= 5
        elif type(item) is int:
            remaining -= max(32, item.bit_length() + 3)
        elif type(item) is float:
            if not math.isfinite(item):
                return False
            remaining -= 32
        elif type(item) is dict:
            remaining -= 2 + 2 * len(item)
            if remaining < 0:
                return False
            for key, child in item.items():
                if not visit(key, depth + 1) or not visit(child, depth + 1):
                    return False
        elif type(item) in (list, tuple):
            remaining -= 2 + len(item)
            if remaining < 0:
                return False
            for child in item:
                if not visit(child, depth + 1):
                    return False
        elif is_dataclass(item) and not isinstance(item, type):
            members = fields(item)
            remaining -= 2 + 2 * len(members)
            if remaining < 0:
                return False
            for member in members:
                if not visit(member.name, depth + 1) or not visit(getattr(item, member.name), depth + 1):
                    return False
        else:
            return False
        return remaining >= 0

    return budget - remaining if visit(value) else None


class _Inputs:
    def __init__(self, store):
        self.store = store
        self.conn = store._connection()
        self.owner_thread = threading.get_ident()
        self.row_factory, self.text_factory = self.conn.row_factory, self.conn.text_factory
        self.start = _stamp(self.conn)
        if not self.start[1] or self.start[3] != 1:
            raise RetrievalInputError('context_retrieval_scope_unavailable')
        self.active = True
        self.cache = {}
        self.bytes = 0

    def check(self, *, boundary=False):
        try:
            same = (self.active and threading.get_ident() == self.owner_thread
                    and self.store._connection() is self.conn
                    and self.conn.row_factory is self.row_factory and self.conn.text_factory is self.text_factory
                    and self.conn.in_transaction and self.conn.total_changes == self.start[2])
            if same and boundary:
                same = _stamp(self.conn) == self.start
        except (sqlite3.Error, RuntimeError):
            same = False
        if not same:
            raise RetrievalInputError('context_retrieval_scope_changed')

    def _retain(self, key, value):
        if len(self.cache) >= MAX_ENTRIES:
            return
        size = _weight(value, MAX_BYTES - self.bytes)
        if size is not None:
            self.cache[key] = deepcopy(value)
            self.bytes += size

    def _raw(self, key, load):
        self.check()
        if key in self.cache:  # None/[] are valid cached absence, not a miss.
            value = self.cache[key]
        else:
            value = load()
            self.check()
            self._retain(key, value)
        return deepcopy(value)

    def records(self, item_ids):
        self.check(boundary=True)
        ids = list(dict.fromkeys(item_ids))
        missing = [ident for ident in ids if ('record', ident) not in self.cache]
        found = {ident: self.cache[('record', ident)] for ident in ids if ('record', ident) in self.cache}
        if missing:
            # Keep the original batch projection/decoding; only omit inputs
            # already successfully read in this exact snapshot.
            loaded = {record.item.id: record for record in self.store.get_retrieval_records(missing)}
            self.check()
            for ident in missing:
                found[ident] = loaded.get(ident)
                self._retain(('record', ident), found[ident])
        return tuple(deepcopy(found[ident]) for ident in ids if found[ident] is not None)

    def get(self, item_id, default=None):
        """The existing eligible(rows=...) protocol invokes this after backing."""
        def load():
            row = self.conn.execute(
                'SELECT i.content_type,i.status,i.success_count,i.experience_payload,'
                'l.category,l.payload AS learning_payload FROM context_items i '
                'LEFT JOIN learning_memories l ON l.item_id=i.id WHERE i.id=?', (item_id,)).fetchone()
            return dict(row) if row is not None else None
        row = self._raw(('eligibility', item_id), load)
        return default if row is None else row

    def eligible(self, item_id):
        from evolvmem.memory_eligibility import eligible
        from evolvmem.unit_derivations import BackingRead, _BACKING_SQL
        # One full boundary check per canonical qualification, rather than
        # repeating metadata PRAGMAs for each nested backing/item input.
        self.check(boundary=True)
        # Lazy per ID is intentional: a different candidate/branch may never
        # execute its canonical backing gate. Do not decode its rows early.
        raw = self._raw(('backing', item_id), lambda: [dict(row) for row in self.conn.execute(
            _BACKING_SQL + ' WHERE d.item_id=?', (item_id,)).fetchall()])
        read = BackingRead(self.store, (), preserve_text_decoding=True)
        read.rows[item_id] = raw
        # Fresh archives/sources state preserves the original lazy source
        # iterator and short circuit; never retain its unread tail or verdict.
        result = eligible(self.store, item_id, read=read, rows=self)
        self.check()
        return result

    def close(self):
        self.active = False
        self.cache.clear()
        self.bytes = 0


def current_inputs(store):
    for inputs in reversed(_SCOPES.get()):
        if inputs.store is store:
            inputs.check()
            return inputs
    return None


@contextmanager
def read_scope(store):
    """Explicit LAN owner opt-in; standalone/caller writes never opt in implicitly."""
    inputs = _Inputs(store)
    if any(prior.conn is inputs.conn for prior in _SCOPES.get()):
        raise RetrievalInputError('context_retrieval_scope_unavailable')
    token = _SCOPES.set((*_SCOPES.get(), inputs))
    try:
        yield inputs
        inputs.check(boundary=True)
    finally:
        inputs.close()
        _SCOPES.reset(token)
