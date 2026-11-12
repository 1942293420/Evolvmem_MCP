"""Additional failure paths, written after the first behavior tranche passed."""
import dataclasses
import unittest
from tests.library_foundation import test_sessions as sessions
from tests.library_foundation import test_foundation as base
from tests.library_foundation.test_foundation import A, B, LIBRARY


class AdapterEdgeTests(unittest.TestCase):
    error = base.FoundationTests.error
    plan = base.FoundationTests.plan

    def setUp(self):
        sessions.SessionTests.setUp(self)

    def test_changed_route_payload_without_epoch_bump_is_rejected(self):
        route = self.plan()
        self.catalog.library = dataclasses.replace(self.library, connection_ref='pg.other')
        self.error('route_binding_changed', lambda: self.router.revalidate(route))

    def test_malformed_catalog_projection_is_fail_closed(self):
        self.catalog.permission = ['read']
        try:
            self.plan()
        except self.api.FoundationError as exc:
            actual = exc.code
        except Exception as exc:
            actual = type(exc).__name__
        else:
            actual = 'unexpectedly_authorized'
        self.assertEqual('catalog_unavailable', actual)

    def test_connection_registry_rejects_duplicate_trusted_binding(self):
        binding = self.registry.resolve(self.plan())
        with self.assertRaises(ValueError):
            self.api.ConnectionRegistry([binding, binding])

    def test_authority_coordinator_is_required(self):
        with self.assertRaises(ValueError):
            self.api.SessionManager(self.router, self.registry, self.provider, None)

    def test_second_credential_cannot_reuse_first_credential_database_role(self):
        self.catalog.binding = dataclasses.replace(self.binding, credential_id=B)
        other = self.api.Authenticator(self.catalog).authenticate('synthetic-test-token')
        route = self.router.plan(other, 'context_read', library_id=LIBRARY)
        self.error('connection_binding_unavailable', lambda: self.registry.resolve(route))

    def test_principal_and_device_epochs_are_pinned_independently(self):
        self.assertTrue(hasattr(self.binding, 'principal_epoch'), 'principal revoke epoch is not represented')
        self.assertTrue(hasattr(self.binding, 'device_epoch'), 'device revoke epoch is not represented')
        for changes in ({'principal_epoch': 2}, {'device_epoch': 2}):
            self.catalog.binding = dataclasses.replace(self.binding, **changes)
            self.error('unauthorized', lambda: self.plan())

    def test_database_credential_mapping_is_checked_not_only_principal(self):
        self.assertTrue(hasattr(self.api, 'DatabaseCredential'), 'trusted database credential projection is absent')
        bad = self.api.DatabaseCredential(B, self.identity.device_id, self.identity.credential_epoch)
        self.connection.session_credential = lambda: bad
        with self.assertRaises(self.api.FoundationError) as cm:
            with self.manager.open(self.plan()):
                self.fail('wrong database credential must be rejected')
        self.assertEqual('database_credential_mismatch', cm.exception.code)

    def test_runtime_role_cannot_have_set_role_memberships_or_role_creation_power(self):
        for field in ('has_memberships', 'can_create_role', 'can_create_database'):
            self.assertTrue(hasattr(self.connection.role, field), 'runtime privilege metadata is incomplete')
            self.connection.role = dataclasses.replace(
                self.api.DatabaseRole('runtime_a', 'runtime_a', False, False, False, True), **{field: True})
            with self.assertRaises(self.api.FoundationError) as cm:
                with self.manager.open(self.plan()):
                    self.fail('privileged runtime role was admitted')
            self.assertEqual('unsafe_database_role', cm.exception.code)

    def test_disabled_catalog_library_cannot_be_served(self):
        self.assertTrue(hasattr(self.library, 'active'), 'library activation is missing from catalog projection')
        self.catalog.library = dataclasses.replace(self.library, active=False)
        self.error('library_unavailable', lambda: self.plan())

    def test_no_content_session_mapping_refuses_before_business(self):
        self.connection.state = None
        with self.assertRaises(self.api.FoundationError) as cm:
            with self.manager.open(self.plan()):
                self.fail('a session without a trusted role mapping must not be yielded')
        self.assertEqual('database_authorization_mismatch', cm.exception.code)
        self.assertTrue(self.connection.closed)

    def test_publication_failure_after_commit_does_not_undo_or_retry_effect(self):
        def write(session):
            session.execute('synthetic operation', ('once',))
            return {'id': 1}
        def publish(value):
            raise RuntimeError('transport failed')
        with self.assertRaises(self.api.FoundationError) as cm:
            self.manager.run(self.plan('memory_add'), write, publish=publish)
        self.assertEqual('write_committed_delivery_failed', cm.exception.code)
        self.assertEqual(['once'], self.connection.persisted)
        self.assertEqual(1, self.connection.commit_count)
        self.assertFalse(self.authority.held)

    def test_close_failure_after_commit_is_explicitly_committed(self):
        def fail_close():
            raise RuntimeError('private close failure')
        self.connection.close = fail_close
        with self.assertRaises(self.api.FoundationError) as cm:
            with self.manager.open(self.plan('memory_add')) as session:
                session.execute('synthetic operation', ('committed',))
        self.assertEqual('write_committed_delivery_failed', cm.exception.code)
        self.assertEqual(['committed'], self.connection.persisted)

    def test_postcommit_revalidation_failure_reports_committed_write(self):
        old_commit = self.connection.commit
        def commit_then_change():
            old_commit()
            self.catalog.library = dataclasses.replace(self.library, policy_epoch=8)
        self.connection.commit = commit_then_change
        def write(session):
            session.execute('synthetic operation', ('committed',))
        self.error('write_committed_delivery_failed', lambda: self.manager.run(self.plan('memory_add'), write))
        self.assertEqual(['committed'], self.connection.persisted)

    def test_cleanup_failure_does_not_hide_operation_failure(self):
        def fail_close():
            raise RuntimeError('close failed')
        self.connection.close = fail_close
        with self.assertRaisesRegex(ValueError, 'domain failed'):
            with self.manager.open(self.plan('memory_add')):
                raise ValueError('domain failed')
        self.assertFalse(self.authority.held)


if __name__ == '__main__':
    unittest.main()
