"""Privileged worker port; never expose this object directly as an MCP tool."""
from dataclasses import dataclass, field
import hashlib
import json
import re

from evolvmem.library_foundation import FoundationError, RequestIdentity
from .adapter import _HELD, _identity, _uuid


def _epoch(value):
    if type(value) is not int or not 1 <= value < 9223372036854775807:
        raise FoundationError('invalid_arguments')
    return value


@dataclass(frozen=True, slots=True)
class AclIntent:
    identity: RequestIdentity
    library_id: str
    target_principal_id: str
    permission: str | None
    expected_policy_epoch: int
    operation_id: str
    request_id: str

    def __post_init__(self):
        _identity(self.identity)
        for value in (self.library_id, self.target_principal_id, self.operation_id):
            _uuid(value)
        _epoch(self.expected_policy_epoch)
        if (self.permission not in (None, 'read', 'write', 'admin')
                or not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 128):
            raise FoundationError('invalid_arguments')

    @property
    def payload_hash(self):
        # A versioned hash of semantic intent, not caller-supplied proof.
        payload = dict(version=1, actor=list(_identity(self.identity)), library=self.library_id,
            target=self.target_principal_id, permission=self.permission,
            expected_epoch=self.expected_policy_epoch, operation=self.operation_id, request=self.request_id)
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                        ensure_ascii=False).encode('utf-8')).digest()


@dataclass(frozen=True, slots=True)
class AclOperation:
    operation_id: str
    library_id: str
    target_policy_epoch: int
    operation_state: str
    expected_route_epoch: int
    connection_ref: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class VerifiedProjection:
    """Result of trusted content worker's committed projection + database readback.

    Merely constructing this value does not prove a database write. The worker
    must verify every runtime role binding and the content authority row first.
    HTTP/MCP request fields must never be decoded into this type.
    """
    operation_id: str
    library_id: str
    target_policy_epoch: int
    expected_route_epoch: int
    connection_ref: str = field(repr=False)
    receipt_hash: bytes

    def __post_init__(self):
        _uuid(self.operation_id)
        _uuid(self.library_id)
        _epoch(self.target_policy_epoch)
        _epoch(self.expected_route_epoch)
        if not isinstance(self.connection_ref, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,127}', self.connection_ref):
            raise FoundationError('invalid_arguments')
        if not isinstance(self.receipt_hash, bytes) or len(self.receipt_hash) != 32:
            raise FoundationError('invalid_arguments')


class ControlAclCoordinator:
    def __init__(self, provider):
        self.provider = provider

    def _mutate(self, sql, params, *, expected=None):
        if _HELD.get() is not None:
            raise FoundationError('authority_lease_scope_mismatch')
        committed = False
        try:
            with self.provider.open(read_only=False) as session:
                rows = session.execute(sql, params)
                result = None
                if expected is not None:
                    result = self._operation(rows, expected)
                session.commit()
                committed = True
            return result
        except FoundationError:
            if committed:
                # Commit acknowledged but cleanup failed: caller must inspect by
                # the stable operation/request IDs before any follow-up phase.
                raise FoundationError('control_committed_cleanup_failed') from None
            raise

    @staticmethod
    def _operation(rows, expected):
        try:
            if len(rows) != 1:
                raise ValueError('ambiguous operation')
            row = rows[0]
            result = AclOperation(str(row['operation_id']), str(row['library_id']),
                row['target_policy_epoch'], row['operation_state'], row['expected_route_epoch'], row['connection_ref'])
            _epoch(result.expected_route_epoch)
            if ((result.operation_id, result.library_id, result.target_policy_epoch) != expected
                    or result.operation_state not in ('pending', 'applying', 'failed', 'complete')
                    or not isinstance(result.connection_ref, str)
                    or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,127}', result.connection_ref)):
                raise ValueError('operation mismatch')
            return result
        except (ValueError, TypeError, KeyError):
            raise FoundationError('catalog_unavailable') from None

    @staticmethod
    def _expected(intent):
        if not isinstance(intent, AclIntent):
            raise FoundationError('invalid_arguments')
        return (intent.operation_id, intent.library_id, intent.expected_policy_epoch + 1)

    def begin(self, intent):
        expected = self._expected(intent)
        return self._mutate('SELECT * FROM em_control.begin_acl_change(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            _identity(intent.identity) + (intent.library_id, intent.target_principal_id, intent.permission,
                intent.expected_policy_epoch, intent.operation_id, intent.request_id, intent.payload_hash),
            expected=expected)

    def lookup(self, intent):
        expected = self._expected(intent)
        if _HELD.get() is not None:
            raise FoundationError('authority_lease_scope_mismatch')
        with self.provider.open() as session:
            rows = session.execute('SELECT * FROM em_control.lookup_acl_operation(%s,%s,%s,%s,%s,%s,%s,%s)',
                _identity(intent.identity) + (intent.operation_id, intent.payload_hash))
            return None if not rows else self._operation(rows, expected)

    def confirm(self, intent, receipt):
        expected = self._expected(intent)
        if (not isinstance(receipt, VerifiedProjection)
                or (receipt.operation_id, receipt.library_id, receipt.target_policy_epoch) != expected):
            raise FoundationError('projection_receipt_mismatch')
        return self._mutate('SELECT * FROM em_control.confirm_acl_projection(%s,%s,%s,%s)',
            (intent.operation_id, receipt.expected_route_epoch, receipt.connection_ref, receipt.receipt_hash), expected=expected)

    def finish(self, intent):
        return self._mutate('SELECT * FROM em_control.finish_acl_change(%s)',
            (intent.operation_id,), expected=self._expected(intent))

    def fail(self, intent, code):
        if not isinstance(code, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code):
            raise FoundationError('invalid_arguments')
        return self._mutate('SELECT * FROM em_control.fail_acl_change(%s,%s)',
            (intent.operation_id, code), expected=self._expected(intent))

    def enroll_external_device(self, principal_id, device_id, external_device_id):
        _uuid(principal_id)
        _uuid(device_id)
        if not isinstance(external_device_id, str) or not 1 <= len(external_device_id) <= 256:
            raise FoundationError('invalid_arguments')
        self._mutate('SELECT em_control.enroll_external_device(%s,%s,%s)',
                     (principal_id, device_id, external_device_id))

    def apply(self, intent, project_and_verify):
        """Three durable control phases around a trusted, idempotent projector.

        A projector is intentionally NOT provided by this foundation candidate.
        It must be implemented against the real content role projection schema.
        Unknown commits are never retried here, including projection uncertainty.
        """
        operation = self.begin(intent)
        if operation.operation_state == 'complete':
            return operation
        try:
            receipt = project_and_verify(intent, operation)
            if (not isinstance(receipt, VerifiedProjection)
                    or (receipt.operation_id, receipt.library_id, receipt.target_policy_epoch)
                       != self._expected(intent)
                    or receipt.expected_route_epoch != operation.expected_route_epoch
                    or receipt.connection_ref != operation.connection_ref):
                raise ValueError('invalid trusted projection result')
        except Exception:
            # Content may have committed even if its reply was lost. Remain
            # blocked; a reconciler must read back before confirming/reopening.
            # If marking failed itself has an unknown outcome, propagate that
            # stable error rather than suggesting the block was persisted.
            self.fail(intent, 'content_projection_failed')
            raise FoundationError('content_projection_failed') from None
        self.confirm(intent, receipt)
        return self.finish(intent)
