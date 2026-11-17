"""Actual host/provider construction; inert connectors, no database simulation."""
import unittest
from unittest.mock import patch
from uuid import UUID
from evolvmem.config import Config
from evolvmem.library_host import LibraryHost
from evolvmem.library_catalog import ControlBinding
from evolvmem.library_foundation import ConnectionBinding, FoundationError
from evolvmem.library_project_acl.worker import ProjectionTarget
from evolvmem.library_deadline import timeout_reuse


def uid(n):
    return str(UUID(int=n))


class AclCoordinatorBudgetTests(unittest.TestCase):
    def host(self, *, reuse=False, acl=True):
        def forbidden(*args, **kwargs):
            raise AssertionError('construction must not connect or resolve secrets')
        content = ConnectionBinding('content.target', uid(1), uid(2), uid(3), uid(4),
                                    'localhost', 5432, 'content', 'runtime', 'content.secret')
        target = ProjectionTarget(uid(1), 'content.target',
            ControlBinding('localhost', 5432, 'content', 'projector', 'projector.secret'), (content,))
        return LibraryHost(control_binding=ControlBinding('localhost',5432,'control','reader','reader.secret'),
            content_bindings=(content,), control_connector=forbidden, content_connector=forbidden,
            secret_resolver=forbidden, config=Config(apply_environment=False), model_contract='synthetic',
            enabled=True, timeout_seconds=12, control_request_reuse=reuse,
            acl_control_binding=ControlBinding('localhost',5432,'control','coordinator','coordinator.secret') if acl else None,
            acl_projection_targets=(target,) if acl else ())

    def test_actual_scope_has_distinct_caps_same_original_guard(self):
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                host=self.host(reuse=reuse)
                try:
                    with host.request_scope() as guard:
                        state=host._state();reader=state.catalog.provider;admin=state.acl_coordinator.provider
                        deadline=guard.absolute_deadline()
                        self.assertEqual((2000,500),(reader.statement_timeout_ms,reader.lock_timeout_ms))
                        self.assertEqual((4000,500),(admin.statement_timeout_ms,admin.lock_timeout_ms))
                        self.assertIsNot(reader,admin)
                        self.assertIs(admin,state.acl_projector.control_provider)
                        for obj in (reader,admin,state.acl_projector,state.manager.authority):
                            self.assertIs(guard,obj.deadline_guard)
                        self.assertEqual(12,guard.timeout_seconds)
                        self.assertEqual(deadline,guard.absolute_deadline())
                        self.assertIsNone(admin._reuse)
                    self.assertEqual(0,host.active_requests)
                finally:host.close()

    def test_short_request_budget_and_acl_disabled_are_unchanged(self):
        for acl in (False,True):
            with self.subTest(acl=acl):
                host=self.host(acl=acl)
                try:
                    with host.request_scope(timeout_seconds=.3) as guard:
                        self.assertEqual(.3,guard.timeout_seconds)
                        self.assertLessEqual(guard.remaining_seconds(),.3)
                        if not acl:self.assertIsNone(host._state().acl_coordinator)
                    with self.assertRaises(ValueError):
                        with host.request_scope(timeout_seconds=13):pass
                finally:host.close()

    def test_rpc_has_no_cap_field_and_admin_rejects_override(self):
        specs=LibraryHost._specs(include_acl=True)
        for spec in specs:
            self.assertFalse({'statement_timeout_ms','lock_timeout_ms','coordinator_timeout_ms'}
                             & set(spec['inputSchema']['properties']))
        host=self.host()
        try:
            with host.request_scope():
                state=host._state()
                args=dict(library_id=uid(1),target_principal_id=uid(2),permission='write',
                          expected_policy_epoch=1,operation_id=uid(5),request_id='probe',
                          project_key='synthetic',statement_timeout_ms=9999)
                with self.assertRaises(FoundationError) as caught:
                    host._admin(state,'library_project_acl_update',args)
                self.assertEqual('invalid_arguments',caught.exception.code)
                self.assertEqual(4000,state.acl_coordinator.provider.statement_timeout_ms)
        finally:host.close()

    def test_ordinary_2s_profile_does_not_match_4s_or_coordinator(self):
        profile=timeout_reuse.attest(kind='control',host='localhost',hostaddr='127.0.0.1',port=5432,
            database='control',user='reader',sslrootcert='/synthetic/ca.crt',ca_sha256='1'*64,
            statement_ms=2000,lock_ms=500,source_sha256='2'*64,proof_sha256='3'*64)
        kw=dict(host='localhost',hostaddr='127.0.0.1',port=5432,dbname='control',user='reader',
                sslmode='verify-full',sslrootcert='/synthetic/ca.crt')
        self.assertIs(profile,timeout_reuse.profile_for('control',kw,statement_ms=2000,lock_ms=500,profile=profile))
        self.assertIsNone(timeout_reuse.profile_for('control',kw,statement_ms=4000,lock_ms=500,profile=profile))
        self.assertIsNone(timeout_reuse.profile_for('control',dict(kw,user='coordinator'),
                                                  statement_ms=4000,lock_ms=500,profile=profile))
        with patch.dict(timeout_reuse._REGISTRY, {}, clear=True):
            timeout_reuse.install_profile(profile)
            self.assertIs(profile,timeout_reuse.profile_for('control',kw,statement_ms=2000,lock_ms=500))
            self.assertIsNone(timeout_reuse.profile_for('control',kw,statement_ms=4000,lock_ms=500))
            self.assertIsNone(timeout_reuse.profile_for('control',dict(kw,user='coordinator'),
                                                      statement_ms=4000,lock_ms=500))


if __name__=='__main__':unittest.main()
