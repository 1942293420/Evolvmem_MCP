"""Behavior tests for reusing the just-read legacy row in the fresh outbox job.

The fake driver executes the real ``legacy_add``/``legacy_replace``/
``legacy_remove`` methods against an in-memory row store.  It never talks to
PostgreSQL, never opens a transaction and never calls a model or service.
"""
import copy
import hashlib
import json

import pytest

from evolvmem.config import Config
from evolvmem.legacy_models import (LegacyAddRequest, LegacyReplaceRequest,
                                    LegacyRemoveRequest)
from evolvmem.pg_business_core import core as core_module
from evolvmem.pg_business_core.core import PgBusinessCore
from evolvmem.pg_business_core.domain import UnsupportedBusiness

assert core_module.PgBusinessCore is PgBusinessCore

LIB = '00000000-0000-0000-0000-000000000004'
PROJECT = '00000000-0000-0000-0000-000000000007'
PROJECT_KEY = 'reviewed-demo-project'
CONTRACT = 'model-contract-v1'
OUTBOX_SQL = '''INSERT INTO em_content.vector_outbox(library_id,project_id,index_kind,entity_id,action,entity_revision,content_hash,model_contract,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(library_id,index_kind,entity_id,entity_revision,action,model_contract) DO NOTHING'''


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class FakeDriver:
    """Minimal row store for the exact SQL shapes used by PgBusinessCore.

    Reads return deep copies so a later UPDATE cannot silently refresh a row
    that a caller is already holding.
    """

    def __init__(self):
        self.memories = {}
        self.contexts = {}
        self.layers = []
        self.mappings = {}
        # Optional mapping-row scope; direct seed assignments default to LIB/PROJECT.
        self.mapping_scopes = {}
        self.sources = []
        self.resolutions = []
        self.outbox = []
        self.outbox_attempts = []
        self.calls = []
        self.row_transform = None
        self._next_memory = 1
        self._next_context = 100
        self.last_memory_id = None
        self.last_context_id = None

    # -- query helpers -------------------------------------------------
    def get_by_id_count(self):
        return sum(1 for sql, _ in self.calls
                   if 'to_jsonb(m)' in sql and 'id=%s' in sql
                   and 'LIMIT 1' not in sql)

    def outbox_inserts(self):
        return [(sql, params) for sql, params in self.calls
                if 'INSERT INTO em_content.vector_outbox(' in sql]

    def seed_memory(self, row):
        self.memories[row['id']] = copy.deepcopy(row)
        self._next_memory = max(self.memories) + 1

    def seed_context(self, row):
        self.contexts[row['id']] = copy.deepcopy(row)
        self._next_context = max(self.contexts) + 1

    # -- execution -----------------------------------------------------
    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        params = tuple(params)

        if 'INSERT INTO em_content.memories(' in sql:
            memory_id = self._next_memory
            self._next_memory += 1
            self.last_memory_id = memory_id
            self.memories[memory_id] = {
                'id': memory_id, 'library_id': params[0], 'project_id': params[1],
                'key': params[2], 'value': params[3], 'attribute': params[4],
                'tags': params[5], 'source_session': params[6],
                'importance': params[7], 'tier': params[8],
                'expires_at': params[9], 'supersedes': params[10],
                'created_at': params[11], 'updated_at': params[12],
                'status': 'active', 'superseded_by': None, 'revision': 1,
                'access_count': 0, 'last_accessed': None,
            }
            return ((memory_id,),)

        if 'SELECT to_jsonb(m)' in sql:
            if 'LIMIT 1' in sql:
                key = params[2]
                active = sorted(mid for mid, row in self.memories.items()
                                if row['library_id'] == params[0]
                                and row['project_id'] == params[1]
                                and row['key'] == key and row['status'] == 'active')
                if not active:
                    return ()
                row = copy.deepcopy(self.memories[active[0]])
                if 'LEFT JOIN em_content.legacy_memory_migrations' in sql:
                    mapping_id = self.mappings.get(active[0])
                    mapping_scope = self.mapping_scopes.get(active[0], (LIB, PROJECT))
                    # SQL IS NOT DISTINCT FROM: None == None is a match.
                    if mapping_id is not None and mapping_scope == (params[0], params[1]):
                        return ((row, mapping_id),)
                    return ((row, None),)
                return ((row,),)
            row = self.memories.get(params[2])
            if row is None or row['library_id'] != params[0] \
                    or row['project_id'] != params[1]:
                return ()
            row = copy.deepcopy(row)
            if self.row_transform is not None:
                row = self.row_transform(row)
            return ((row,),)

        if "UPDATE em_content.memories SET status='deleted'" in sql:
            row = self.memories[params[3]]
            row['status'] = 'deleted'
            row['updated_at'] = params[0]
            row['revision'] += 1
            return ()

        if 'UPDATE em_content.memories SET status=\'superseded\'' in sql:
            if 'id IN (SELECT' in sql:
                item_id = params[6]
                ids = sorted(legacy_id for legacy_id, mapped in self.mappings.items()
                             if mapped == item_id)
                for legacy_id in ids:
                    self._supersede_memory(legacy_id, params[0], params[1])
                return tuple((legacy_id,) for legacy_id in ids)
            self._supersede_memory(params[4], params[0], params[1])
            return ()

        if 'INSERT INTO em_content.context_items(' in sql:
            item_id = self._next_context
            self._next_context += 1
            self.last_context_id = item_id
            self.contexts[item_id] = {
                'id': item_id, 'library_id': params[0], 'project_id': params[1],
                'identity_key': params[2], 'content_type': params[3],
                'project': params[4], 'scope': params[5], 'status': params[6],
                'tier': params[7], 'tags': json.loads(params[8]),
                'importance': params[9], 'confidence': params[10],
                'expires_at': params[11], 'supersedes': params[12],
                'effective_from': params[13], 'effective_until': params[14],
                'occurred_at': params[15], 'mentioned_at': params[16],
                'created_at': params[17], 'updated_at': params[18],
                'source_state': 'none', 'source_count': 0,
                'success_count': 0, 'failure_count': 0, 'access_count': 0,
                'last_accessed': None, 'last_verified_at': None,
                'superseded_by': None, 'personal_general': False,
                'projects': [], 'experience_payload': None, 'revision': 1,
            }
            return ((item_id,),)

        if 'SELECT to_jsonb(c)' in sql:
            row = self.contexts.get(params[2])
            if row is None or row['library_id'] != params[0] \
                    or row['project_id'] != params[1]:
                return ()
            if 'LEFT JOIN em_content.context_layers' in sql:
                content = next((content
                                for lib, project, item_id, layer, content, _, _
                                in self.layers
                                if lib == params[0] and project == params[1]
                                and item_id == params[2] and layer == 'l0'), None)
                return ((copy.deepcopy(row), content),)
            return ((copy.deepcopy(row),),)

        if 'INSERT INTO em_content.context_layers(' in sql:
            self.layers.append((params[0], params[1], params[2], params[3],
                                params[4], params[5], params[6]))
            return ()

        if 'SELECT layer,content,generator FROM em_content.context_layers' in sql:
            return tuple((layer, content, generator)
                         for lib, project, item_id, layer, content, _, generator
                         in self.layers
                         if lib == params[0] and project == params[1]
                         and item_id == params[2])

        if "SELECT content,content_hash FROM em_content.context_layers" in sql:
            return tuple((content, content_hash)
                         for lib, project, item_id, layer, content, content_hash, _
                         in self.layers
                         if lib == params[0] and project == params[1]
                         and item_id == params[2] and layer == 'l0')

        if 'SELECT id FROM em_content.context_items' in sql:
            ids = sorted(item_id for item_id, row in self.contexts.items()
                         if row['library_id'] == params[0]
                         and row['project_id'] == params[1]
                         and row['identity_key'] == params[2]
                         and row['project'] == params[3]
                         and row['scope'] == params[4]
                         and row['status'] == 'active' and item_id != params[5])
            return tuple((item_id,) for item_id in ids)

        if 'SELECT context_item_id FROM em_content.legacy_memory_migrations' in sql:
            item_id = self.mappings.get(params[2])
            return () if item_id is None else ((item_id,),)

        if 'INSERT INTO em_content.legacy_memory_migrations' in sql:
            self.mappings[params[2]] = params[3]
            self.mapping_scopes[params[2]] = (params[0], params[1])
            return ()

        if 'INSERT INTO em_content.context_sources(' in sql:
            self.sources.append(params)
            return ((len(self.sources),),)

        if 'INSERT INTO em_content.context_project_resolutions' in sql:
            self.resolutions.append(params)
            return ()

        if 'SELECT count(*) FROM em_content.context_sources' in sql:
            return ((0,),)

        if "UPDATE em_content.context_items SET status='deleted'" in sql:
            self.contexts[params[3]]['status'] = 'deleted'
            self.contexts[params[3]]['updated_at'] = params[0]
            self.contexts[params[3]]['revision'] += 1
            return ()

        if 'UPDATE em_content.context_items SET source_count=' in sql:
            self.contexts[params[2]]['source_count'] = len(self.sources)
            return ()

        if "UPDATE em_content.context_items SET status='superseded',superseded_by=%s" in sql:
            self._supersede_context(params[4], params[1], superseded_by=params[0])
            return ()

        if "UPDATE em_content.context_items SET status='superseded'" in sql:
            self._supersede_context(params[3], params[0])
            return ()

        if "UPDATE em_content.context_items SET superseded_by=%s,updated_at=%s" in sql:
            self.contexts[params[4]]['superseded_by'] = params[0]
            self.contexts[params[4]]['updated_at'] = params[1]
            return ()

        if 'INSERT INTO em_content.vector_outbox(' in sql:
            self.outbox_attempts.append((sql, params))
            key = params[0:8]
            if not any(job[0:8] == key for job in self.outbox):
                self.outbox.append((sql, params))
            return ()

        raise AssertionError('unexpected SQL shape: ' + sql.splitlines()[0])

    def _supersede_memory(self, memory_id, superseded_by, updated_at):
        row = self.memories[memory_id]
        row['status'] = 'superseded'
        row['superseded_by'] = superseded_by
        row['updated_at'] = updated_at
        row['revision'] += 1

    def _supersede_context(self, item_id, updated_at, *, superseded_by=None):
        row = self.contexts[item_id]
        row['status'] = 'superseded'
        if superseded_by is not None:
            row['superseded_by'] = superseded_by
        row['updated_at'] = updated_at
        row['revision'] += 1


class Route:
    action = 'write'


class Lease:
    active = True

    def require(self, library_id, key, project_id):
        assert (library_id, project_id) == (LIB, PROJECT)


class Scope:
    project_key = PROJECT_KEY
    library_id = LIB
    project_id = PROJECT
    policy_epoch = 7

    def require(self, session):
        return None


def core_for(driver):
    driver.route = Route()
    driver._closed = False
    core = PgBusinessCore.__new__(PgBusinessCore)
    core.session = driver
    core.config = Config(apply_environment=False)
    core.lease = Lease()
    core.library = LIB
    core.project_scope = Scope()
    core.lib = LIB
    core.project_id = PROJECT
    core.model_contract = CONTRACT
    return core


def memory_row(memory_id, *, key='alpha', value='old body', attribute='decision',
               revision=1, status='active'):
    return {
        'id': memory_id, 'library_id': LIB, 'project_id': PROJECT, 'key': key,
        'value': value, 'attribute': attribute, 'tags': '', 'source_session': '',
        'importance': 5.0, 'tier': 'normal', 'expires_at': None,
        'supersedes': None, 'created_at': '2026-01-01 00:00:00',
        'updated_at': '2026-01-01 00:00:00', 'status': status,
        'superseded_by': None, 'revision': revision, 'access_count': 0,
        'last_accessed': None,
    }


def context_row(item_id, identity_key, *, project=PROJECT_KEY,
                scope='project', content_type='decision', status='active',
                revision=1):
    return {
        'id': item_id, 'library_id': LIB, 'project_id': PROJECT,
        'identity_key': identity_key, 'content_type': content_type,
        'project': project, 'scope': scope, 'status': status, 'tier': 'normal',
        'tags': [], 'importance': 5.0, 'confidence': 0.5,
        'expires_at': None, 'supersedes': None,
        'effective_from': None, 'effective_until': None, 'occurred_at': None,
        'mentioned_at': None, 'created_at': '2026-01-01 00:00:00',
        'updated_at': '2026-01-01 00:00:00', 'source_state': 'none',
        'source_count': 0, 'success_count': 0, 'failure_count': 0,
        'access_count': 0, 'last_accessed': None, 'last_verified_at': None,
        'superseded_by': None, 'personal_general': False, 'projects': [],
        'experience_payload': None, 'revision': revision,
    }


def identity_key(key='alpha'):
    return 'project:' + PROJECT + ':' + key


def assert_job(sql, params, *, kind, entity, action, revision, content_hash,
               created_at=None):
    assert sql == OUTBOX_SQL
    assert params[:8] == (LIB, PROJECT, kind, entity, action, revision,
                          content_hash, CONTRACT)
    assert isinstance(params[8], str) and params[8]
    if created_at is not None:
        assert params[8] == created_at


def jobs(driver):
    return {(params[2], params[3], params[4]): params
            for _, params in driver.outbox}


# ---------------------------------------------------------------------------
# add: one SELECT removed, exact outbox parameters retained
# ---------------------------------------------------------------------------
def test_add_reuses_the_draft_row_and_removes_exactly_one_select():
    driver = FakeDriver()
    core = core_for(driver)
    request = LegacyAddRequest(key='alpha', value='new body', attribute='decision')
    core.legacy_add(request)

    # The fresh legacy row is reused; context revision and body share one read.
    assert driver.get_by_id_count() == 1
    assert len(driver.calls) == 13
    assert len(driver.outbox_inserts()) == 2
    assert len(driver.outbox) == 2
    new_id = driver.last_memory_id
    legacy_job = jobs(driver)[('legacy', new_id, 'upsert')]
    assert_job(driver.outbox_inserts()[0][0], legacy_job, kind='legacy',
               entity=new_id, action='upsert', revision=1,
               content_hash=digest('new body'))
    context_job = jobs(driver)[('context', 100, 'upsert')]
    l0 = driver.layers[0][4]
    assert_job(driver.outbox_inserts()[1][0], context_job, kind='context',
               entity=100, action='upsert', revision=1,
               content_hash=digest(l0))
    # The row really is the fresh DB row, not a request-derived revision.
    assert driver.memories[new_id]['revision'] == 1
    assert driver.memories[new_id]['value'] == 'new body'


def test_add_keeps_context_proof_and_removal_reads_unchanged():
    driver = FakeDriver()
    driver.seed_memory(memory_row(1, key='alpha', value='old body'))
    driver.seed_context(context_row(10, identity_key('alpha')))
    driver.mappings[1] = 10
    core = core_for(driver)
    core.legacy_remove(LegacyRemoveRequest(legacy_id=1))

    # legacy_remove retains both legacy reads; context proof uses one read.
    assert driver.get_by_id_count() == 2
    assert len(driver.calls) == 10
    assert len(driver.outbox) == 2
    removed = jobs(driver)
    assert_job(driver.outbox_inserts()[0][0], removed[('legacy', 1, 'remove')],
               kind='legacy', entity=1, action='remove', revision=2,
               content_hash=digest('old body'))
    assert_job(driver.outbox_inserts()[1][0], removed[('context', 10, 'remove')],
               kind='context', entity=10, action='remove', revision=2,
               content_hash=digest(''))


# ---------------------------------------------------------------------------
# replace: fresh row scope, old/absorbed post-update row
# ---------------------------------------------------------------------------
def test_replace_without_predecessor_reuses_only_the_new_row():
    driver = FakeDriver()
    core = core_for(driver)
    core.legacy_replace(LegacyReplaceRequest(key='alpha', new_value='new body',
                                             attribute='decision'))
    # No predecessor: reuse the fresh legacy row and join its context proof.
    assert driver.get_by_id_count() == 1
    assert len(driver.calls) == 14
    assert len(driver.outbox) == 2
    new_id = driver.last_memory_id
    assert_job(driver.outbox_inserts()[0][0],
               jobs(driver)[('legacy', new_id, 'upsert')], kind='legacy',
               entity=new_id, action='upsert', revision=1,
               content_hash=digest('new body'))


def test_replace_predecessor_job_uses_post_update_revision():
    driver = FakeDriver()
    driver.seed_memory(memory_row(1, value='old body'))
    driver.seed_context(context_row(10, identity_key('alpha')))
    driver.mappings[1] = 10
    core = core_for(driver)
    core.legacy_replace(LegacyReplaceRequest(key='alpha', new_value='new body',
                                             attribute='decision'))

    # Initial legacy read joins its mapping; two context proofs each join.
    assert driver.get_by_id_count() == 2
    assert len(driver.calls) == 24
    assert len(driver.outbox) == 4
    new_id = driver.last_memory_id
    job_map = jobs(driver)
    assert driver.memories[1]['revision'] == 2
    assert_job(driver.outbox_inserts()[0][0], job_map[('legacy', 1, 'upsert')],
               kind='legacy', entity=1, action='upsert', revision=2,
               content_hash=digest('old body'))
    assert_job(driver.outbox_inserts()[1][0], job_map[('legacy', new_id, 'upsert')],
               kind='legacy', entity=new_id, action='upsert', revision=1,
               content_hash=digest('new body'))
    assert ('context', 10, 'remove') in job_map
    assert driver.contexts[10]['status'] == 'superseded'
    assert driver.contexts[10]['revision'] == 2


def test_replace_absorbed_legacy_rows_follow_their_post_update_revision():
    driver = FakeDriver()
    driver.seed_memory(memory_row(1, key='alpha', value='old body', revision=1))
    driver.seed_memory(memory_row(5, key='absorbed', value='absorbed body',
                                  revision=1))
    driver.seed_context(context_row(10, identity_key('alpha')))
    driver.seed_context(context_row(20, identity_key('alpha')))
    driver.mappings[1] = 10
    driver.mappings[5] = 20
    core = core_for(driver)
    core.legacy_replace(LegacyReplaceRequest(key='alpha', new_value='new body',
                                             attribute='decision'))

    # Initial legacy read joins its mapping; three context proofs each join.
    assert driver.get_by_id_count() == 3
    assert len(driver.calls) == 32
    new_id = driver.last_memory_id
    assert driver.mappings[new_id] == driver.last_context_id
    assert driver.memories[new_id]['revision'] == 1
    assert driver.memories[new_id]['status'] == 'active'
    assert driver.memories[1]['revision'] == 2
    assert driver.memories[5]['revision'] == 2
    job_map = jobs(driver)
    assert len(driver.outbox) == 6
    assert_job(driver.outbox_inserts()[0][0], job_map[('legacy', 5, 'upsert')],
               kind='legacy', entity=5, action='upsert', revision=2,
               content_hash=digest('absorbed body'))
    assert_job(driver.outbox_inserts()[1][0], job_map[('legacy', 1, 'upsert')],
               kind='legacy', entity=1, action='upsert', revision=2,
               content_hash=digest('old body'))
    assert_job(driver.outbox_inserts()[2][0], job_map[('legacy', new_id, 'upsert')],
               kind='legacy', entity=new_id, action='upsert', revision=1,
               content_hash=digest('new body'))
    assert ('context', 10, 'remove') in job_map
    assert ('context', 20, 'remove') in job_map
    assert ('context', driver.last_context_id, 'upsert') in job_map
    for sql, _ in driver.outbox_inserts():
        assert sql == OUTBOX_SQL


@pytest.mark.parametrize('field,bad', [
    ('id', 999),
    ('library_id', '00000000-0000-0000-0000-000000000099'),
    ('project_id', '00000000-0000-0000-0000-000000000098'),
])
def test_conflicting_supplied_identity_fails_closed_before_enqueue(field, bad):
    driver = FakeDriver()
    driver.row_transform = lambda row: dict(row, **{field: bad})
    core = core_for(driver)
    with pytest.raises(UnsupportedBusiness) as caught:
        core.legacy_add(LegacyAddRequest(key='alpha', value='new body',
                                         attribute='decision'))
    assert caught.value.code == 'fresh_legacy_row_mismatch'
    assert driver.outbox == []
    assert driver.outbox_attempts == []


def test_pending_rejects_unverified_supplied_row_before_any_sql():
    driver = FakeDriver()
    core = core_for(driver)
    with pytest.raises(UnsupportedBusiness) as caught:
        core._pending('legacy', 31, 'upsert', fresh_row={'id': 32})
    assert caught.value.code == 'fresh_legacy_row_mismatch'
    assert driver.calls == []
    assert driver.outbox == []


def test_remove_wrong_mode_ignores_stale_supplied_row_and_reads_storage():
    driver = FakeDriver()
    driver.seed_memory(memory_row(31, value='stored body', revision=2))
    stale = memory_row(31, value='stored body', revision=1)
    core = core_for(driver)
    core._pending('legacy', 31, 'remove', fresh_row=stale)

    # The supplied row is same-identity but stale; remove must use the read.
    assert driver.get_by_id_count() == 1
    assert len(driver.outbox_attempts) == 1
    assert_job(driver.outbox_inserts()[0][0],
               jobs(driver)[('legacy', 31, 'remove')],
               kind='legacy', entity=31, action='remove', revision=2,
               content_hash=digest('stored body'))


def test_missing_fresh_proof_falls_back_to_the_original_read():
    driver = FakeDriver()
    core = core_for(driver)
    with pytest.raises(TypeError):
        core._pending('legacy', 31, 'upsert', fresh_row=None)
    # The fallback performed the original SELECT; no fabricated job was emitted.
    assert driver.get_by_id_count() == 1
    assert driver.outbox == []


@pytest.mark.parametrize('row_scope', ['other-project', 'other-library', 'missing'])
def test_context_pending_missing_authorized_item_fails_before_outbox(row_scope):
    driver = FakeDriver()
    row = context_row(41, identity_key(), revision=9)
    if row_scope == 'other-project':
        row['project_id'] = '00000000-0000-0000-0000-000000000098'
    elif row_scope == 'other-library':
        row['library_id'] = '00000000-0000-0000-0000-000000000099'
    if row_scope != 'missing':
        driver.seed_context(row)
    # Even a same-ID current-project layer must not bypass a missing item.
    driver.layers.append((LIB, PROJECT, 41, 'l0', 'must not leak',
                          digest('must not leak'), 'synthetic'))
    core = core_for(driver)
    with pytest.raises(TypeError):
        core._pending('context', 41, 'upsert')
    assert driver.outbox_attempts == []
    assert driver.outbox == []
    assert len(driver.calls) == 1


@pytest.mark.parametrize('layer_scope', ['other-project', 'other-library'])
def test_context_pending_ignores_same_id_layer_from_another_scope(layer_scope):
    driver = FakeDriver()
    driver.seed_context(context_row(41, identity_key(), revision=9))
    layer_lib, layer_project = LIB, PROJECT
    if layer_scope == 'other-project':
        layer_project = '00000000-0000-0000-0000-000000000098'
    else:
        layer_lib = '00000000-0000-0000-0000-000000000099'
    driver.layers.append((layer_lib, layer_project, 41, 'l0', 'must not leak',
                          digest('must not leak'), 'synthetic'))
    core = core_for(driver)
    core._pending('context', 41, 'upsert')
    assert len(driver.calls) == 2
    sql, params = driver.outbox_inserts()[0]
    assert_job(sql, params, kind='context', entity=41, action='upsert',
               revision=9, content_hash=digest(''))


def test_context_pending_null_project_matches_item_and_l0_layer():
    driver = FakeDriver()
    row = context_row(41, 'personal:alpha', revision=9)
    row['project_id'] = None
    driver.seed_context(row)
    driver.layers.append((LIB, None, 41, 'l0', 'personal body',
                          '0' * 64, 'synthetic'))
    core = core_for(driver)
    core.project_id = None
    core._pending('context', 41, 'upsert')
    assert len(driver.calls) == 2
    sql, params = driver.outbox_inserts()[0]
    assert sql == OUTBOX_SQL
    assert params[:8] == (LIB, None, 'context', 41, 'upsert', 9,
                          digest('personal body'), CONTRACT)
    assert isinstance(params[8], str) and params[8]
