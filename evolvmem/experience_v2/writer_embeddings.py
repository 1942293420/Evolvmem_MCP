"""Projection preparation only; all experience_notes mutations remain in writer.py."""
from __future__ import annotations

import math
import os
import struct

from evolvmem.experience_v2.embedding_client import note_content_sha256 as content_sha256

FIELDS = ('category', 'problem', 'solution', 'reusable_experience')


def _valid_vector(values, dimension):
    if (not isinstance(values, (list, tuple)) or len(values) != dimension
            or not all(type(v) in (float, int) and math.isfinite(v) for v in values)):
        return False
    try:
        stored = struct.unpack('<' + 'f' * dimension, struct.pack('<' + 'f' * dimension, *values))
    except (OverflowError, struct.error):
        return False
    return all(math.isfinite(v) for v in stored) and any(v != 0 for v in stored)


def target_note(op, before):
    """Use exact values that the writer will persist, including restored snapshots."""
    if op.action in ('create', 'import_manual', 'update'):
        return {key: ((getattr(op, key) or '').strip() if key != 'category' else op.category)
                for key in FIELDS}
    if op.action == 'rollback':
        return op.restore_snapshot
    return before


def prepare_projection(conn, op, before, client):
    """Return validated projection, or None only for explicitly unconfigured fixtures."""
    from evolvmem.experience_v2.writer import ExperienceWriteError
    note = target_note(op, before)
    if note is None:
        return None
    if op.action not in ('create', 'import_manual', 'update', 'restore', 'reconcile_state', 'rollback', 'reembed'):
        return None
    status = (op.status if op.action in ('create', 'import_manual', 'reconcile_state') else note.get('status', 'active'))
    deleted = (op.deleted_at if op.action in ('create', 'import_manual') else
               None if op.action == 'restore' else note.get('deleted_at'))
    if op.action != 'reembed' and (status != 'active' or deleted is not None):
        return None
    policy = conn.execute('SELECT required,retrieval_contract_id,legacy_contract_id '
                          'FROM experience_embedding_policy WHERE singleton = true').fetchone()
    required = bool(policy and policy[0])
    configured = bool(os.environ.get('EVOLVMEM_EXPERIENCE_EMBEDDING_CONFIG', '').strip())
    try:
        if client is None and configured:
            from evolvmem.experience_v2.embedding_client import EmbeddingClient
            client = EmbeddingClient.from_environment()
        if client is None:
            if required or configured or op.action == 'reembed':
                raise ExperienceWriteError('experience_embedding_unavailable', 503)
            return None
        if required and (client.retrieval_contract_id != policy[1] or client.legacy_contract_id != policy[2]):
            raise ExperienceWriteError('experience_embedding_contract_mismatch', 503)
        projection = client.encode_note(note)
        if (projection.contract_id != client.retrieval_contract_id
                or projection.content_sha256 != content_sha256(note)):
            raise ExperienceWriteError('experience_embedding_projection_invalid', 503)
        for values, dimension in ((projection.legacy_embedding, 768), (projection.retrieval_embedding, 1024)):
            if not _valid_vector(values, dimension):
                raise ExperienceWriteError('experience_embedding_projection_invalid', 503)
        return projection
    except ExperienceWriteError:
        raise
    except Exception as exc:
        code = getattr(exc, 'code', '')
        if not isinstance(code, str) or not code or not all(c.isascii() and (c.isalnum() or c == '_') for c in code):
            code = 'experience_embedding_unavailable'
        raise ExperienceWriteError(code, 503) from None


def projection_changed(op, before):
    if before is None:
        return False
    target = target_note(op, before)
    return target is not None and content_sha256(target) != content_sha256(before)
