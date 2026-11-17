"""Source paging is bounded, ordered, complete, and snapshot-final."""
from copy import deepcopy
from types import SimpleNamespace
import hashlib
import json
import threading

import pytest

from evolvmem.context_models import ContextSearchRequest
from evolvmem.library_foundation import FoundationError
import evolvmem.pg_business_core.history_qualification as history_qualification
from evolvmem.pg_business_core.history_qualification import HistoryEligibilityRead
from evolvmem.pg_business_core.history_qualification_sql import BATCH, READ_CONTEXT
from evolvmem.pg_business_core.native_qualification import MAX_BYTES
from evolvmem.pg_business_core.native_search import PgNativeSearch, SelectionRead

LIB = '00000000-0000-0000-0000-000000000001'
PROJECT = '00000000-0000-0000-0000-000000000002'
PROJECT_KEY = 'A'


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def source(ident, item_id=1, library_id=LIB, proofs=0):
    return dict(id=ident, library_id=library_id, item_id=item_id,
                source_kind='manual', source_ref='ref-%d' % ident,
                extraction_version='legacy-write.v1', archive_id=None,
                archive=None, exclusion=None,
                event_proofs=[dict(evidence_id=i, state='verified') for i in range(proofs)])


def payload(count, *, item_id=1, library_id=LIB, proofs=0):
    item = dict(id=item_id, library_id=library_id, project_id=None, project=PROJECT_KEY,
                projects=[], scope='project', content_type='reference', status='active',
                revision=1, source_count=count, expires_at=None,
                effective_from=None, effective_until=None)
    return dict(item=item, item_l1_hash='a' * 64,
                memberships=[dict(library_id=library_id, item_id=item_id, project_id=PROJECT,
                                  project_key=PROJECT_KEY, state='confirmed')],
                layers=[], learning=[], resolution=[], registry=[],
                sources=[source(i, item_id, library_id, proofs) for i in range(1, count + 1)],
                derivations=[], basis=[], conversations=[], evidence=[], rollup=[], knowledge=[])


class SourceSession:
    route = SimpleNamespace(action='read', library=SimpleNamespace(library_id=LIB))
    _closed = False

    def __init__(self, count=0, *, item_id=1, proofs=0):
        self.count, self.item_id, self.proofs = count, item_id, proofs
        self.calls = []
        self._connection = object()
        self.first_error = None
        self.page_error = None
        self.first_mutator = None
        self.page_mutator = None
        self.mutate_page = False

    def full(self, item_id=None):
        return payload(self.count, item_id=self.item_id if item_id is None else item_id,
                       proofs=self.proofs)

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if sql == READ_CONTEXT:
            return ((1, 'history-preserved-v1', 7, 9, 1, 1, 'c' * 64),)
        if sql == BATCH:
            # The unfixed baseline still receives the original two-column
            # payload, which contains all sources and trips its 64 guard.
            return [(self.item_id, self.full())]
        if 'history_item_inputs_first_page' in sql:
            if self.first_error is not None:
                raise self.first_error
            if len(params[2]) > 1:
                return [(ident, payload(0, item_id=ident), None, False)
                        for ident in params[2]]
            data = self.full()
            first = data['sources'][:64]
            data['sources'] = first
            after = first[-1]['id'] if first else None
            more = self.count > 64
            record = (self.item_id, data, after, more)
            if self.first_mutator is not None:
                record = self.first_mutator(record)
            return [record]
        if 'history_item_sources_page' in sql:
            if self.page_error is not None:
                raise self.page_error
            after = params[3]
            remaining = [s for s in self.full()['sources'] if s['id'] > after]
            page = remaining[:64]
            more = len(remaining) > 64
            if self.mutate_page:
                for entry in page:
                    if entry['id'] == 65:
                        entry['source_ref'] = 'changed-on-second-page'
            records = [(s['id'], s, more) for s in page]
            if self.page_mutator is not None:
                records = self.page_mutator(records, after)
            return records
        raise AssertionError('unexpected SQL: %s' % sql)

    def source_calls(self):
        return [sql for sql, _ in self.calls if 'history_item_sources_page' in sql]


def make_read(session):
    read = object.__new__(HistoryEligibilityRead)
    read.session, read.library_id, read.project_id = session, LIB, PROJECT
    read.project_scope = SimpleNamespace(project_id=PROJECT, project_key=PROJECT_KEY,
                                         require=lambda _session: None)
    read._thread, read._connection = threading.get_ident(), session._connection
    read.rows, read.content, read._remaining = {}, {}, MAX_BYTES
    read.readiness = (1, 'history-preserved-v1', 7, 9, 1, 1, 'c' * 64)
    read.now = '2026-11-03 00:00:00'
    return read


def trusted_proof(full):
    read = object.__new__(HistoryEligibilityRead)
    read._connection = object()
    read.session = SimpleNamespace(_connection=read._connection)
    read.library_id, read.project_id = LIB, PROJECT
    read.project_scope = SimpleNamespace(project_id=PROJECT, project_key=PROJECT_KEY,
                                         require=lambda _session: None)
    read._thread = threading.get_ident()
    read.rows, read.content, read._remaining = {1: deepcopy(full)}, {}, MAX_BYTES
    read.readiness = (1, 'history-preserved-v1', 7, 9, 1, 1, 'c' * 64)
    read.now = '2026-11-03 00:00:00'
    return read.proof()


@pytest.mark.parametrize('count', [0, 64, 65, 97, 294])
def test_boundary_pages_assemble_every_source_in_order(count):
    session = SourceSession(count)
    read = make_read(session)
    read._load((1,))
    rows = read.rows[1]['sources']
    assert [s['id'] for s in rows] == list(range(1, count + 1))
    assert read.rows[1] == session.full()
    expected_pages = 0 if count <= 64 else (count - 65) // 64 + 1
    assert len(session.source_calls()) == expected_pages
    assert read.proof() == trusted_proof(session.full())
    assert hashlib.sha256(_json(read.rows[1]).encode()).hexdigest() == \
        hashlib.sha256(_json(session.full()).encode()).hexdigest()


def test_no_continuation_does_not_issue_a_page_query():
    session = SourceSession(64)
    read = make_read(session)
    read._load((1,))
    assert session.source_calls() == []


def test_nested_event_proof_65_is_still_rejected():
    session = SourceSession(65, proofs=65)
    read = make_read(session)
    with pytest.raises(FoundationError, match='recall_dependency_limit'):
        read._load((1,))
    assert read.rows == {}


@pytest.mark.parametrize('mutator', [
    lambda r: (r[0], r[1], None, True),
    lambda r: (r[0], r[1], r[2], 1),
    lambda r: (r[0], r[1], r[2], None),
])
def test_first_page_cursor_contract_is_closed(mutator):
    session = SourceSession(65)
    read = make_read(session)
    session.first_mutator = mutator
    with pytest.raises(FoundationError):
        read._load((1,))
    assert read.rows == {}


def test_short_nonempty_first_page_claiming_more_is_rejected():
    session = SourceSession(65)

    def truncate(record):
        ident, data, _after, _more = record
        data = deepcopy(data)
        data['sources'] = data['sources'][:63]
        return ident, data, 63, True

    session.first_mutator = truncate
    read = make_read(session)
    with pytest.raises(FoundationError, match='invalid_history_source_page'):
        read._load((1,))
    assert read.rows == {}
    assert session.source_calls() == []


def context_source(ident):
    entry = source(ident)
    entry.update(source_kind='context_reference', source_ref=str(ident))
    return entry


def test_summary_294_context_references_load_in_240_and_54_batches(monkeypatch):
    session = SourceSession(0)
    read = make_read(session)
    row = payload(0)
    row['sources'] = [context_source(i) for i in range(1, 295)]
    summaries = []
    monkeypatch.setattr(history_qualification.verdict, 'summary_current',
                        lambda item, ids, dependencies, **kwargs:
                        summaries.append(tuple(ids)) or True)
    assert read._summary_current(row) is True
    batches = [len(params[2]) for sql, params in session.calls
               if 'history_item_inputs_first_page' in sql]
    assert batches == [240, 54]
    assert summaries == [tuple(range(1, 295))]
    assert set(read.rows) == set(range(1, 295))


def test_summary_over_512_context_references_is_rejected_without_sql():
    session = SourceSession(0)
    read = make_read(session)
    row = payload(0)
    row['sources'] = [context_source(i) for i in range(1, 514)]
    with pytest.raises(FoundationError, match='invalid_qualification_ids'):
        read._summary_current(row)
    assert session.calls == []


def test_first_payload_and_each_new_page_are_retained_once():
    session = SourceSession(194)
    read = make_read(session)
    retained = []
    retain = read._retain
    read._retain = lambda value: (retained.append(value), retain(value))[1]
    read._load((1,))
    assert len(retained) == 4
    assert isinstance(retained[0], dict) and len(retained[0]['sources']) == 64
    assert [len(value) for value in retained[1:]] == [64, 64, 2]
    assert read.rows[1] == session.full()


@pytest.mark.parametrize('mutator', [
    lambda records, after: [],
    lambda records, after: [records[0], records[0]],
    lambda records, after: list(reversed(records)),
    lambda records, after: [(records[0][0] - 1, records[0][1], False)],
    lambda records, after: [(records[0][0], records[0][1], 'yes')],
    lambda records, after: [(records[0][0], records[0][1], True)],
    lambda records, after: [(records[0][0], records[0][1], True)] * 65,
])
def test_bad_page_is_rejected_and_not_published(mutator):
    session = SourceSession(294)
    session.page_mutator = mutator
    read = make_read(session)
    with pytest.raises(FoundationError):
        read._load((1,))
    assert read.rows == {}


@pytest.mark.parametrize('field,value,code', [
    ('library_id', 'foreign', 'qualification_scope_mismatch'),
    ('item_id', 99, 'qualification_scope_mismatch'),
])
def test_page_source_scope_is_revalidated(field, value, code):
    session = SourceSession(65)
    def mutate(records, after):
        records[0][1][field] = value
        return records
    session.page_mutator = mutate
    read = make_read(session)
    with pytest.raises(FoundationError, match=code):
        read._load((1,))
    assert read.rows == {}


def test_sql_permission_timeout_and_source_errors_propagate():
    error = RuntimeError('statement_timeout')
    session = SourceSession(65)
    session.first_error = error
    with pytest.raises(RuntimeError) as raised:
        make_read(session)._load((1,))
    assert raised.value is error

    session = SourceSession(65)
    session.page_error = error
    with pytest.raises(RuntimeError) as raised:
        make_read(session)._load((1,))
    assert raised.value is error


def test_8mib_budget_and_512_context_dependency_limits():
    session = SourceSession(0)
    read = make_read(session)
    def huge():
        data = payload(0)
        data['item'] = dict(data['item'], note='x' * (MAX_BYTES + 1))
        return data
    session.full = huge
    with pytest.raises(FoundationError, match='recall_dependency_limit'):
        read._load((1,))
    assert read.rows == {}

    session = SourceSession(0)
    read = make_read(session)
    with pytest.raises(FoundationError, match='invalid_qualification_ids'):
        read._load(tuple(range(1, 514)))
    assert session.calls == []


def test_fresh_final_snapshot_rejects_second_page_change_through_search():
    session = SourceSession(97)

    class Coordinator:
        def run_many(self, routes, read, *, finalize, combine, publish):
            prior = read(session)
            session.mutate_page = True
            return combine(((session.route, finalize(session, prior)),))

    search = PgNativeSearch(Coordinator(), SimpleNamespace(context_min_confidence=0))

    def read(_session, key, request, kind, experience, prior=None):
        current = make_read(_session)
        current._load((1,))
        return SelectionRead(key, proof=current.proof(), dto=())

    search._read = read
    with pytest.raises(FoundationError, match='recall_selected_changed'):
        search.search((session.route,), ((LIB, PROJECT_KEY),), ContextSearchRequest(query='synthetic'))
