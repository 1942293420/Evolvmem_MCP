"""Behavior tests at catalog/driver ports; no database or network is opened."""
import dataclasses
import hashlib
import importlib
import importlib.util
from pathlib import Path
import sys
import unittest


A = '00000000-0000-0000-0000-000000000001'
B = '00000000-0000-0000-0000-000000000002'
DEVICE = '00000000-0000-0000-0000-000000000010'
CREDENTIAL = '00000000-0000-0000-0000-000000000020'
LIBRARY = '00000000-0000-0000-0000-000000000100'
PROJECT = '00000000-0000-0000-0000-000000000200'


class Catalog:
    """External catalog port double with deliberately mutable current state."""
    def __init__(self, binding, library, permission='read'):
        self.binding, self.library, self.permission = binding, library, permission
        self.lookup_count = 0
        self.available = True

    def credential_for_digest(self, digest):
        if not self.available:
            raise RuntimeError('private catalog endpoint')
        return self.binding if self.binding.token_digest == digest else None

    def authorization_snapshot(self, identity, library_id):
        self.lookup_count += 1
        if not self.available:
            raise RuntimeError('private catalog endpoint')
        if library_id != self.library.library_id:
            return None
        api = importlib.import_module('evolvmem.library_foundation')
        return api.AuthorizationSnapshot(self.binding, self.library, self.permission)


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('evolvmem.library_foundation'),
                             'PG foundation implementation is not present')
        self.api = importlib.import_module('evolvmem.library_foundation')
        self.binding = self.api.CredentialBinding(
            credential_id=CREDENTIAL, principal_id=A, device_id=DEVICE,
            token_digest=hashlib.sha256(b'synthetic-test-token').hexdigest(), epoch=3)
        self.library = self.api.LibraryDescriptor(
            library_id=LIBRARY, kind='project', backend='postgres',
            connection_ref='pg.synthetic', project_id=PROJECT,
            policy_epoch=7, route_epoch=11, authority_state='pg_active',
            acl_state='ready', writer_backend='postgres', active=True)
        self.catalog = Catalog(self.binding, self.library)
        self.identity = self.api.Authenticator(self.catalog, clock=lambda: 100).authenticate(
            'synthetic-test-token', claimed_device_id=DEVICE)
        self.router = self.api.AuthorityRouter(self.catalog, implemented_tools={
            'postgres': frozenset({'context_read', 'memory_add', 'library_acl_update', 'context_search'})})

    def error(self, code, call):
        with self.assertRaises(self.api.FoundationError) as cm:
            call()
        self.assertEqual(code, cm.exception.code)
        return cm.exception

    def plan(self, tool='context_read', **kwargs):
        return self.router.plan(self.identity, tool, library_id=LIBRARY, **kwargs)

    def test_authentication_rejects_wrong_token_without_private_details(self):
        err = self.error('unauthorized', lambda: self.api.Authenticator(self.catalog).authenticate('wrong'))
        self.assertNotIn('wrong', str(err))

    def test_device_is_credential_bound_not_request_selected(self):
        self.error('device_binding_mismatch', lambda: self.api.Authenticator(self.catalog).authenticate(
            'synthetic-test-token', claimed_device_id=B))
        self.assertEqual(DEVICE, self.identity.device_id)

    def test_expired_or_revoked_binding_cannot_authenticate(self):
        for changes in ({'expires_at': 99}, {'active': False}, {'principal_active': False}, {'device_active': False}):
            self.catalog.binding = dataclasses.replace(self.binding, **changes)
            self.error('unauthorized', lambda: self.api.Authenticator(self.catalog, clock=lambda: 100).authenticate('synthetic-test-token'))

    def test_identity_and_route_are_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.identity.principal_id = B
        route = self.plan()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            route.library.route_epoch = 12

    def test_unknown_and_known_unauthorized_library_have_same_error(self):
        self.catalog.permission = None
        self.error('library_access_denied', lambda: self.plan())
        self.error('library_access_denied', lambda: self.router.plan(self.identity, 'context_read', library_id=B))

    def test_read_write_admin_matrix(self):
        expected = {
            'read': {'context_read'},
            'write': {'context_read', 'memory_add'},
            'admin': {'context_read', 'memory_add', 'library_acl_update'},
        }
        for permission, permitted in expected.items():
            self.catalog.permission = permission
            for tool in ('context_read', 'memory_add', 'library_acl_update'):
                if tool in permitted:
                    self.assertEqual(tool, self.plan(tool).tool)
                else:
                    self.error('library_access_denied', lambda: self.plan(tool))

    def test_ownership_does_not_grant_implicit_access(self):
        self.catalog.library = dataclasses.replace(self.library, kind='device', project_id=None,
                                                  device_id=DEVICE, owner_principal_id=A)
        self.catalog.permission = None
        self.error('library_access_denied', lambda: self.plan())

    def test_unported_tools_fail_without_sqlite_fallback(self):
        self.catalog.permission = 'admin'
        self.error('library_backend_unsupported', lambda: self.plan('capability_search'))
        empty = self.api.AuthorityRouter(self.catalog)
        self.error('library_backend_unsupported', lambda: empty.plan(self.identity, 'context_read', library_id=LIBRARY))

    def test_untrusted_connection_or_identity_fields_are_rejected(self):
        for field in ('dsn', 'connection_ref', 'principal_id', 'db_role', 'owner', 'backend', 'space',
                      'authenticated_device_id', 'credential_epoch', 'principal_epoch', 'device_epoch', 'device_id'):
            self.error('routing_override_forbidden', lambda: self.plan(request_fields={field: 'injected'}))

    def test_stale_epochs_and_incomplete_acl_are_fail_closed(self):
        self.error('route_epoch_changed', lambda: self.plan(expected_route_epoch=10))
        self.error('policy_epoch_changed', lambda: self.plan(expected_policy_epoch=6))
        for changes in ({'acl_state': 'updating'}, {'authority_state': 'freeze_and_reconcile'}, {'authority_state': 'pg_staging'}):
            self.catalog.library = dataclasses.replace(self.library, **changes)
            self.error('library_unavailable', lambda: self.plan())

    def test_writer_cannot_commit_to_non_authoritative_backend(self):
        self.catalog.permission = 'write'
        self.catalog.library = dataclasses.replace(self.library, writer_backend='sqlite')
        self.error('write_authority_mismatch', lambda: self.plan('memory_add'))

    def test_credential_changes_invalidate_previously_authenticated_identity(self):
        for changes in ({'epoch': 4}, {'active': False}, {'principal_id': B}, {'device_id': B}):
            self.catalog.binding = dataclasses.replace(self.binding, **changes)
            self.error('unauthorized', lambda: self.plan())

    def test_catalog_failure_does_not_expose_connection_or_silently_allow(self):
        self.catalog.available = False
        err = self.error('catalog_unavailable', lambda: self.plan())
        self.assertNotIn('private', str(err))

    def test_many_routes_are_read_only_bounded_and_all_authorized(self):
        self.assertEqual(1, len(self.router.plan_many(self.identity, 'context_search', [LIBRARY])))
        self.error('invalid_library_selection', lambda: self.router.plan_many(self.identity, 'context_search', [LIBRARY, LIBRARY]))
        self.error('invalid_library_selection', lambda: self.router.plan_many(self.identity, 'memory_add', [LIBRARY]))
        self.error('library_access_denied', lambda: self.router.plan_many(self.identity, 'context_search', [LIBRARY, B]))

    def test_reference_registry_rejects_cross_principal_or_cross_library_binding(self):
        route = self.plan()
        binding = self.api.ConnectionBinding(connection_ref='pg.synthetic', library_id=LIBRARY,
            principal_id=A, credential_id=CREDENTIAL, device_id=DEVICE,
            host='127.0.0.1', port=55439, database='synthetic_a', role='synthetic_a',
            password_ref='secrets.synthetic.a')
        registry = self.api.ConnectionRegistry([binding])
        self.assertEqual('synthetic_a', registry.resolve(route).role)
        for changes in ({'principal_id': B}, {'library_id': B}):
            bad = self.api.ConnectionRegistry([dataclasses.replace(binding, **changes)])
            self.error('connection_binding_unavailable', lambda: bad.resolve(route))

    def test_reference_registry_freezes_input_and_redacts_secret_reference(self):
        binding = self.api.ConnectionBinding(connection_ref='pg.synthetic', library_id=LIBRARY,
            principal_id=A, credential_id=CREDENTIAL, device_id=DEVICE,
            host='127.0.0.1', port=55439, database='synthetic_a', role='synthetic_a',
            password_ref='secrets.synthetic.a')
        self.assertNotIn('secrets.synthetic.a', repr(binding))
        for name in ('postgresql://user:secret@host/db', 'x; SET ROLE owner'):
            with self.assertRaises(ValueError):
                dataclasses.replace(binding, connection_ref=name)


if __name__ == '__main__':
    unittest.main()
