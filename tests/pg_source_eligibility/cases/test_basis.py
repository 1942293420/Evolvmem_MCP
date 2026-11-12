from contextlib import contextmanager
import unittest
from .test_chain import Chain,SqlPort,BINDING,LIB,H
from evolvmem.pg_business_core.basis_reconcile import reconcile_quarantined_basis
from evolvmem.library_foundation import FoundationError


class Basis(Chain):
    def setUp(self):
        super().setUp();self.read_only=True;self.fail_update=False;self.commit_count=0
        self.c.execute("INSERT INTO organization_context_basis(unit_task_id,unit_digest,prior_task_id,prior_digest,project,created_at,updated_at) VALUES(1,'d',99,'missing','p','a','b')")
        self.c.commit()
        original=self.port.execute
        def execute(sql,params=()):
            if sql==H.IDENTITY_SQL:
                row=list(original(sql,params)[0]);row[2]='on' if self.read_only else 'off';return [tuple(row)]
            if sql.startswith('SELECT to_char'):return [('2030-01-01 00:00:00',)]
            if sql.startswith('UPDATE em_content.'):
                if self.fail_update and 'organization_context_basis' in sql:raise RuntimeError('synthetic SQL failure')
                cursor=self.c.execute(sql.replace('em_content.','').replace('%s','?'),params)
                return [tuple(row) for row in cursor.fetchall()]
            if ' FOR UPDATE' in sql:sql=sql.replace(' FOR UPDATE','')
            return original(sql,params)
        self.port.execute=execute
    @contextmanager
    def write_factory(self):
        self.read_only=False;self.c.execute('BEGIN')
        try:
            yield self.port
            self.c.commit();self.commit_count+=1
        except BaseException:
            self.c.rollback();raise
        finally:self.read_only=True
    def test_stale_basis_then_bounded_reconcile_then_fresh_refusal(self):
        with self.assertRaisesRegex(FoundationError,'basis_reconciliation_required'):self.call()
        result=reconcile_quarantined_basis(self.write_factory,BINDING,limit=1)
        self.assertEqual((1,False,False),(result['invalidated'],result['remaining'],result['task_refresh_complete']))
        self.assertEqual(1,self.commit_count)
        self.assertEqual(('review','',2),tuple(self.c.execute('SELECT decision,project,revision FROM organization_units').fetchone()))
        self.assertEqual((),self.call())
        self.assertEqual(0,reconcile_quarantined_basis(self.write_factory,BINDING,limit=1)['invalidated'])
    def test_manual_is_never_revoked(self):
        self.c.execute("UPDATE organization_units SET decision='manual'");self.c.commit()
        self.assertEqual(0,reconcile_quarantined_basis(self.write_factory,BINDING)['invalidated'])
        self.assertEqual('manual',self.c.execute('SELECT decision FROM organization_units').fetchone()[0])
    def test_unit_and_basis_update_are_atomic_on_failure(self):
        self.fail_update=True
        with self.assertRaisesRegex(FoundationError,'quarantine_query_failed'):reconcile_quarantined_basis(self.write_factory,BINDING)
        self.assertEqual(('auto',1),tuple(self.c.execute('SELECT decision,revision FROM organization_units').fetchone()))
        self.assertEqual('active',self.c.execute('SELECT state FROM organization_context_basis').fetchone()[0])
        self.assertEqual(0,self.commit_count)
    def test_ambiguous_digest_refuses_instead_of_selecting_first(self):
        row=dict(self.c.execute('SELECT * FROM organization_units').fetchone());row['ordinal']=2
        self.c.execute('INSERT INTO organization_units('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()));self.c.commit()
        with self.assertRaisesRegex(FoundationError,'ambiguous_basis_unit'):reconcile_quarantined_basis(self.write_factory,BINDING)
    def test_invalid_limit_has_no_connection(self):
        def forbidden():raise AssertionError('connection must not be opened')
        with self.assertRaisesRegex(FoundationError,'invalid_reconciliation_limit'):reconcile_quarantined_basis(forbidden,BINDING,limit=129)

    def test_post_commit_local_exception_never_returns_success_receipt(self):
        @contextmanager
        def uncertain():
            with self.write_factory() as connection:yield connection
            raise RuntimeError('synthetic_post_commit_exception')
        with self.assertRaisesRegex(RuntimeError,'synthetic_post_commit_exception'):
            reconcile_quarantined_basis(uncertain,BINDING)
        self.assertEqual('invalid',self.c.execute('SELECT state FROM organization_context_basis').fetchone()[0])
        self.assertEqual(0,reconcile_quarantined_basis(self.write_factory,BINDING)['invalidated'])


# Do not re-run the unrelated inherited Chain cases on the deliberately stale fixture.
for _name in tuple(Chain.__dict__):
    if _name.startswith('test_') and _name not in Basis.__dict__:setattr(Basis,_name,None)
del Chain
