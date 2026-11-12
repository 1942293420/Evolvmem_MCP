"""Private bridge for one pre-effect PostgreSQL row-conflict outcome.

The public foundation errors stay redacted. This module is intentionally not
exported from :mod:`evolvmem.library_foundation`; only the trusted vector apply
path and the session transaction machinery know these exact types.
"""


class PreEffectRowConflict(Exception):
    """Raised only by the two exact locked native entity reads.

    Those are ``memories m ... FOR SHARE OF m`` and
    ``context_items i ... FOR SHARE OF i`` in the vector repository.
    """


class ConfirmedPreEffectRollback(Exception):
    """Transaction rollback and content close both succeeded after the marker."""


class ProjectionConflictDeferred(Exception):
    """Trusted projection apply may idle only after the full authority scope."""


class _ProjectionApplyOperation:
    """Private opt-in wrapper: run() recognizes this exact type, nothing else."""
    __slots__ = ('_callback',)

    def __init__(self, callback):
        self._callback = callback

    def __call__(self, session):
        return self._callback(session)


class ProjectionBatchConflict(Exception):
    """One trusted batch apply entity hit a confirmed rollback-eligible conflict.

    Raised only inside the native bounded-batch apply callback, after converting
    either the existing :class:`PreEffectRowConflict` or a redacted
    ``FoundationError`` wrapping an immediate typed 40001. It is meaningful only
    for :class:`_ProjectionBatchApplyOperation`; the whole batch transaction is
    still uncommitted and must be rolled back in full.
    """


class ConfirmedBatchRollback(Exception):
    """Rollback, actual IDLE verification and content close all succeeded."""


class ProjectionBatchDeferred(Exception):
    """A fully cleaned-up batch may idle; nothing was committed or acknowledged.

    Distinct from :class:`ProjectionConflictDeferred` so the runtime can raise
    its own batch sentinel and the worker never mistakes it for the older
    single/pair pre-effect outcome.
    """


class _ProjectionBatchApplyOperation:
    """Private opt-in wrapper for one fresh native read-write batch transaction.

    No publisher, write/admin only. ``run()`` recognizes this exact type; the
    old single/pair wrapper and ordinary callables never gain batch semantics.
    """
    __slots__ = ('_callback',)

    def __init__(self, callback):
        self._callback = callback

    def __call__(self, session):
        return self._callback(session)


def is_serialization_failure(exc):
    """True only for the redacted wrapper around an immediate typed 40001.

    ``_PsycopgConnection.execute`` raises ``FoundationError`` with the actual
    driver exception in the immediate ``__context__`` (``from None`` keeps the
    public message redacted). We deliberately inspect only that one link; there
    is no text parsing and no arbitrary chain walk.
    """
    if getattr(exc, 'code', None) != 'database_operation_failed':
        return False
    if getattr(exc, '__cause__', None) is not None:
        return False
    actual = getattr(exc, '__context__', None)
    if actual is None or getattr(actual, 'sqlstate', None) != '40001':
        return False
    try:
        from psycopg.errors import SerializationFailure
    except ImportError:
        return False
    return type(actual) is SerializationFailure
