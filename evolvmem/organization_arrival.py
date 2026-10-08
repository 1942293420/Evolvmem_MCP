"""Autonomous arrival of new sources and the explicit historical backlog.

Arrival mode is persisted: enabling it records an activation baseline, so the
historical backlog stays an explicit, bounded job while only sources that arrive
afterwards are discovered automatically. Pausing stops discovery without
touching work that is already queued.
"""
from __future__ import annotations

from evolvmem.context_store import _now_iso

BACKLOG_LIMIT = 20
DISCOVER_LIMIT = 5


def settings(service):
    row = service.store._connection().execute(
        'SELECT * FROM organization_settings WHERE id=1').fetchone()
    if row:
        return dict(row)
    return {'id': 1, 'auto_new': 0, 'activation_at': '', 'baseline_id': 0, 'revision': 0}


def update_settings(service, body):
    if 'auto_new' not in body:
        raise ValueError('invalid_request')
    enabled = bool(body['auto_new'])
    current = settings(service)
    if body.get('expected_revision') is not None and int(body['expected_revision']) != current['revision']:
        raise ValueError('revision_conflict')
    conn = service.store._connection()
    baseline = current['baseline_id']
    if enabled and not current['auto_new']:
        # Activation baseline: the historical backlog stays explicit, only
        # sources that arrive from now on are picked up automatically.
        baseline = conn.execute('SELECT COALESCE(MAX(id),0) m FROM session_archives').fetchone()['m']
    with service.store.transaction():
        conn.execute(
            'INSERT INTO organization_settings(id,auto_new,activation_at,baseline_id,revision,updated_at) '
            "VALUES(1,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET auto_new=excluded.auto_new,"
            'activation_at=excluded.activation_at,baseline_id=excluded.baseline_id,'
            'revision=revision+1,updated_at=excluded.updated_at',
            (1 if enabled else 0, current['activation_at'] or _now_iso(), baseline, _now_iso()))
    return settings(service)


def discover_new(service, *, limit=DISCOVER_LIMIT):
    """Enqueue sources that arrived after activation while arrival mode is on.

    Each tick also runs one bounded, model-free reconciliation of automatic
    tasks whose source is now excluded, empty or superseded, so old
    pending/failed/review rows do not keep spending retries.
    """
    current = settings(service)
    if not current['auto_new']:
        return {'enabled': False, 'items': [], 'created': 0}
    from evolvmem.auto_organization import (enqueue, link_superseded_successors,
                                            reconcile_sources)
    reconcile = reconcile_sources(service)
    keys = [ref['key'] for ref in _pending_sources(service)
            if ref['key'].startswith('archive:') and int(ref['key'].split(':')[1]) > current['baseline_id']]
    result = enqueue(service, keys[:limit]) if keys else {'items': [], 'created': 0, 'duplicates': 0}
    # A version retired moments before its successor's task existed is linked
    # inside the same bounded tick instead of waiting for the next one.
    reconcile['linked'] += link_superseded_successors(service)
    return {'enabled': True, **result, 'reconcile': reconcile}


def backlog(service, body=None):
    """Bounded explicit processing of the historical queue with visible progress."""
    body = body or {}
    limit = min(BACKLOG_LIMIT, max(1, int(body.get('limit', BACKLOG_LIMIT) or BACKLOG_LIMIT)))
    pending = [ref['key'] for ref in _pending_sources(service)]
    keys = pending[:limit]
    from evolvmem.auto_organization import enqueue
    result = enqueue(service, keys) if keys else {'items': [], 'created': 0, 'duplicates': 0}
    return {**result, 'remaining': max(0, len(pending) - len(keys)), 'queue_total': len(pending)}


def _pending_sources(service):
    """Unassigned sources without a current task, newest first; never derived items."""
    from evolvmem.history_organization import source_refs
    return source_refs(service)


