"""Foreground enqueue contract: one immutable fresh outbox job.

Older pending rows are owned exclusively by the background decide/apply path.
A user mutation must never lock, retire or UPDATE them: a worker may commit
after this transaction's snapshot but before the mutation statement, which is
exactly the reverse lock-order race. These are statement-boundary regressions,
not a PostgreSQL execution claim.
"""
from pathlib import Path
import copy
import hashlib
import os
import sys

import pytest

SOURCE_ROOT = Path(os.environ.get(
    'EVOLVMEM_SOURCE_ROOT', Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(SOURCE_ROOT))
import evolvmem  # noqa: E402
from evolvmem.pg_business_core.core import PgBusinessCore  # noqa: E402

FORBIDDEN = ('UPDATE ', 'DELETE ', 'FOR UPDATE', 'SKIP LOCKED', 'WITH retired',
             "status='stale'", 'stale_revision')


class Recorder:
    """Boundary double. It fails closed on any transaction-control or DML
    attempt that the contract forbids, and records the immutable old jobs."""

    def __init__(self, legacy=None, context=None, layers=(), old_jobs=(),
                 fail_at=None):
        self.legacy, self.context, self.layers = legacy, context, layers
        self.old_jobs = tuple(copy.deepcopy(job) for job in old_jobs)
        self.calls = []
        self.events = []
        self.fail_at = fail_at

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        self.events.append('sql')
        if ((self.fail_at == 'read' and 'to_jsonb(c)' in sql)
                or (self.fail_at == 'insert'
                    and 'INSERT INTO em_content.vector_outbox(' in sql)):
            raise RuntimeError('synthetic ' + self.fail_at + ' failure')
        if 'to_jsonb(m)' in sql:
            # Simulate another runtime connection committing its projection
            # and outbox work after this transaction's snapshot was taken.
            if self.old_jobs:
                self.old_jobs = tuple(
                    dict(job, status='completed') for job in self.old_jobs)
                self.events.append('worker_commit_after_snapshot')
            return () if self.legacy is None else ((self.legacy,),)
        if 'to_jsonb(c)' in sql:
            if self.context is None:
                return ()
            if 'LEFT JOIN em_content.context_layers' in sql:
                content = self.layers[0][0] if self.layers else None
                return ((self.context, content),)
            return ((self.context,),)
        if "layer='l0'" in sql:
            return tuple(self.layers)
        return ()

    def commit(self, *args, **kwargs):
        raise AssertionError('foreground mutation must not manage the transaction here')

    rollback = commit

    def close(self, *args, **kwargs):
        raise AssertionError('foreground mutation must not close the connection')


def core_for(recorder):
    core = PgBusinessCore.__new__(PgBusinessCore)
    core.lib = '00000000-0000-0000-0000-000000000004'
    core.project_id = '00000000-0000-0000-0000-000000000007'
    core.model_contract = 'model-contract-v1'
    core.session = recorder
    return core


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def assert_single_fresh_insert(sql, params, *, library, project, kind, entity,
                               action, revision, content_hash):
    assert sql.strip().startswith('INSERT INTO em_content.vector_outbox(')
    assert sql.count('vector_outbox') == 1
    assert sql.count('INSERT INTO em_content.vector_outbox') == 1
    assert sql.count(';') == 0
    for token in FORBIDDEN:
        assert token not in sql, token
    assert 'ON CONFLICT(library_id,index_kind,entity_id,entity_revision,action,model_contract) DO NOTHING' in sql
    assert sql.count('%s') == 9
    assert params == (library, project, kind, entity, action, revision,
                      content_hash, 'model-contract-v1', params[-1])
    assert isinstance(params[-1], str) and params[-1]


def test_legacy_mutation_enqueues_one_job_and_leaves_old_jobs_untouched():
    text = 'bounded synthetic legacy body'
    old = ({'id': 11, 'status': 'pending', 'entity_revision': 6},)
    recorder = Recorder(legacy={'value': text, 'revision': 7}, old_jobs=old)
    core = core_for(recorder)
    core._pending('legacy', 31, 'upsert')

    assert recorder.events[:2] == ['sql', 'worker_commit_after_snapshot']
    assert len(recorder.calls) == 2
    sql, params = recorder.calls[-1]
    assert_single_fresh_insert(sql, params, library=core.lib, project=core.project_id,
                               kind='legacy', entity=31, action='upsert',
                               revision=7, content_hash=digest(text))
    # The old pending job was committed by a concurrent worker, and the
    # immutable snapshot shows the mutation neither saw nor rewrote it.
    assert recorder.old_jobs == ({'id': 11, 'status': 'completed',
                                  'entity_revision': 6},)


def assert_single_context_read(recorder, core, entity):
    read_sql, read_params = recorder.calls[0]
    assert 'LEFT JOIN em_content.context_layers l' in read_sql
    assert "l.layer='l0'" in read_sql
    assert 'l.library_id=c.library_id' in read_sql
    assert 'l.project_id IS NOT DISTINCT FROM c.project_id' in read_sql
    assert 'l.item_id=c.id' in read_sql
    assert 'c.library_id=%s' in read_sql
    assert 'c.project_id IS NOT DISTINCT FROM %s' in read_sql
    assert 'c.id=%s' in read_sql
    assert read_sql.count('%s') == 3
    assert read_params == (core.lib, core.project_id, entity)


def test_context_enqueue_keeps_exact_columns_hash_and_conflict_target():
    text = 'bounded synthetic context body'
    recorder = Recorder(context={'revision': 9},
                        layers=[(text, digest(text))])
    core = core_for(recorder)
    core._pending('context', 41, 'remove')

    assert len(recorder.calls) == 2
    assert_single_context_read(recorder, core, 41)
    sql, params = recorder.calls[-1]
    assert_single_fresh_insert(sql, params, library=core.lib, project=core.project_id,
                               kind='context', entity=41, action='remove',
                               revision=9, content_hash=digest(text))


@pytest.mark.parametrize('layers,content', [
    ([('bounded body', digest('bounded body'))], 'bounded body'),
    ((), ''),
    ([('', digest(''))], ''),
    ([('\u4e2d\u6587\U0001f642e\u0301', 'untrusted')], '\u4e2d\u6587\U0001f642e\u0301'),
    ([('bounded body', '0' * 64)], 'bounded body'),
], ids=['normal', 'missing-l0', 'empty', 'unicode', 'tampered-stored-hash'])
def test_context_hash_is_computed_from_utf8_body_in_one_read(layers, content):
    recorder = Recorder(context={'revision': 9}, layers=layers)
    core = core_for(recorder)
    core._pending('context', 41, 'upsert')

    assert len(recorder.calls) == 2
    assert_single_context_read(recorder, core, 41)
    sql, params = recorder.calls[-1]
    assert_single_fresh_insert(sql, params, library=core.lib, project=core.project_id,
                               kind='context', entity=41, action='upsert',
                               revision=9, content_hash=digest(content))


@pytest.mark.parametrize('phase', ['read', 'insert'])
def test_context_failure_propagates_without_transaction_control(phase):
    recorder = Recorder(context={'revision': 9},
                        layers=[('body', digest('body'))], fail_at=phase)
    core = core_for(recorder)
    with pytest.raises(RuntimeError, match='synthetic ' + phase + ' failure'):
        core._pending('context', 41, 'upsert')

    assert len(recorder.calls) == (1 if phase == 'read' else 2)
    assert_single_context_read(recorder, core, 41)
    assert sum('INSERT INTO em_content.vector_outbox(' in sql
               for sql, _ in recorder.calls) == (0 if phase == 'read' else 1)
    # Recorder.commit/rollback/close raise if _pending tries to own the txn.


def test_repeated_context_enqueue_keeps_the_same_idempotent_insert():
    old_jobs = ({'id': 11, 'status': 'pending', 'entity_revision': 6},)
    recorder = Recorder(context={'revision': 9},
                        layers=[('body', '0' * 64)], old_jobs=old_jobs)
    core = core_for(recorder)
    core._pending('context', 41, 'upsert')
    first_sql, first_params = recorder.calls[-1]
    core._pending('context', 41, 'upsert')
    second_sql, second_params = recorder.calls[-1]

    assert len(recorder.calls) == 4
    assert first_sql == second_sql
    assert first_params[:-1] == second_params[:-1]
    for sql, params in ((first_sql, first_params), (second_sql, second_params)):
        assert_single_fresh_insert(sql, params, library=core.lib,
                                   project=core.project_id, kind='context',
                                   entity=41, action='upsert', revision=9,
                                   content_hash=digest('body'))
    assert recorder.old_jobs == old_jobs


def test_repeated_enqueue_is_the_same_idempotent_insert_never_a_retirement():
    recorder = Recorder(legacy={'value': 'body', 'revision': 2})
    core = core_for(recorder)
    core._pending('legacy', 5, 'upsert')
    first_sql, first_params = recorder.calls[-1]
    core._pending('legacy', 5, 'upsert')
    second_sql, second_params = recorder.calls[-1]

    assert first_sql == second_sql
    assert first_params[:-1] == second_params[:-1]  # only created_at differs
    for sql, params in ((first_sql, first_params), (second_sql, second_params)):
        assert_single_fresh_insert(sql, params, library=core.lib,
                                   project=core.project_id, kind='legacy',
                                   entity=5, action='upsert', revision=2,
                                   content_hash=digest('body'))
