"""Logical identity of one archived source version.

Two Codex shapes share the ``codex`` adapter but not their version semantics:

* A Windows client-reported upload is a whole snapshot of one session. Every
  version carries the same session head before the first ``:`` and only the
  newest is the current one, so versions must collapse to that head.
* The local Linux collector writes independent incremental batches of one
  session. Each batch embeds its own source range and content digest in the
  identity itself (``sha256(session)[:32]:start-end:digest[:16]``), so splitting
  at the first ``:`` would collapse distinct batches into one version and let a
  newer batch hide an older, still-queued one.

Recognition here is strictly structural: it only inspects the identity string
that ``local_codex_capture.batch_external_id`` writes and never reads a payload.
Any value that does not match that exact shape keeps the original session-head
deduplication, so other adapters and legacy identities are untouched.
"""
from __future__ import annotations

import re

# sha256(session_id)[:32] + ':' + start_line + '-' + end_line + ':' + digest[:16]
INCREMENTAL_BATCH_PATTERN = re.compile(r'[0-9a-f]{32}:[0-9]{1,10}-[0-9]{1,10}:[0-9a-f]{16}')


def is_incremental_batch(external_session_id):
    """Whether this identity is one independent local incremental batch."""
    value = '' if external_session_id is None else str(external_session_id)
    return INCREMENTAL_BATCH_PATTERN.fullmatch(value) is not None


def logical_identity(adapter, external_session_id):
    """The version key one archive shares with its own earlier/later versions."""
    value = '' if external_session_id is None else str(external_session_id)
    if adapter == 'codex' and not is_incremental_batch(value):
        return value.split(':')[0]
    return value


def identity_key(adapter, external_session_id):
    """Adapter-scoped logical identity, for dedup and visibility oracles."""
    value = '' if external_session_id is None else str(external_session_id)
    return (adapter, logical_identity(adapter, value))


# SQL mirror of :func:`logical_identity`, for candidate filtering only. SQLite
# has no regex, so the incremental-batch shape is recognized structurally
# (exactly two colons, a 32-character head, a range dash, and a final colon
# before the 16-character digest). It is deliberately conservative in the safe
# direction: what it classifies as an independent batch is never retired, and
# anything it cannot classify keeps the session-head rule.
SQL_LOGICAL_IDENTITY = (
    "CASE "
    "WHEN {alias}.adapter='codex' "
    "AND (length({alias}.external_session_id)-length(replace({alias}.external_session_id,':','')))=2 "
    "AND instr({alias}.external_session_id,':')=33 "
    "AND instr(substr({alias}.external_session_id,34),'-')>0 "
    "AND substr({alias}.external_session_id,-17,1)=':' "
    "THEN {alias}.external_session_id "
    "WHEN {alias}.adapter='codex' "
    "THEN substr({alias}.external_session_id,1,instr({alias}.external_session_id||':',':')-1) "
    "ELSE {alias}.external_session_id END"
)
