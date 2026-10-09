"""Per-unit extraction through the existing session/learning pipeline.

A unit is extracted with the real provider contract (``session_extraction`` →
``kimi_hooks._extract_candidates``) over only its own messages, with the saved
cleaning draft as reviewed cleaning and the shared related-memory lookup in the
prompt. Persistence then goes through ``persist_legacy_extraction`` so duplicate
sharing, source links and the add/supplement/replace/skip validation are the
same as any other source. The deterministic, source-linked history record is
written without a model call, so history is available before extraction ends.

Every stage is idempotent: a unit keeps a stage and a signature, an unchanged
completed extraction is skipped, and only incomplete stages are retried.
"""
from __future__ import annotations

import hashlib
import json
import re

from evolvmem.context_store import _now_iso
from evolvmem.legacy_models import LegacyExtractionItem, LegacyExtractionRequest
from evolvmem import topic_segmentation

UNIT_STAGES = ('pending', 'running', 'done', 'skipped', 'failed')
# Experience can only be promoted by the existing ExperienceService evidence
# resolver, and only from a real tool/user-confirmation event in the source.
EVIDENCE_ROLES = ('tool',)


def signature(unit, project, rule_revision) -> str:
    """What an extraction result depends on; a change means re-extract.

    The unit's same-session context basis is part of the signature: the same
    text attributed by a different predecessor is a different result.
    """
    return hashlib.sha256(json.dumps(
        [unit.get('digest'), unit.get('text'), unit.get('cleaned_text'), unit.get('title'),
         unit.get('category'), project, rule_revision, unit.get('context_basis') or ''],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]


def _summary_item(unit, project, digest) -> LegacyExtractionItem:
    """The deterministic, source-linked history record for one unit."""
    return LegacyExtractionItem(
        key=f'project:{project}:progress:log:org-{digest}',
        value=(unit.get('cleaned_text') or unit.get('text') or '').strip(),
        attribute='fact', tags=('日志', f'分类:{project}', '自动整理'),
        project_hint=project)


def write_history(service, task, unit, messages) -> int | None:
    """Store the unit's project-history record without any model call."""
    from evolvmem.unit_derivations import record_derivation
    archive_id = _archive_id(task)
    request = LegacyExtractionRequest(
        summary=_summary_item(unit, unit['project'], unit['digest']), candidates=(),
        max_writes=1, source_session=task['source_key'], project_hint=unit['project'])
    result = service.persist_legacy_extraction(
        request, source_archive_id=archive_id, source_messages=messages)
    item_id = result.summary.context_id if result.summary else None
    if item_id is None:
        existing = service.store._connection().execute(
            "SELECT id FROM context_items WHERE identity_key=? AND project=? AND status!='deleted' "
            'ORDER BY id DESC LIMIT 1', (request.summary.key, unit['project'])).fetchone()
        item_id = existing['id'] if existing else None
    if item_id is None:
        return None
    _link_provenance(service, item_id, archive_id, task, unit)
    record_derivation(service, task['id'], unit['digest'], item_id, kind='history',
                      project=unit['project'])
    # Persistence runs before the unit provenance exists. A history-only unit
    # can therefore have been indexed while it still looked independently
    # backed. Reconcile after attaching the source, without re-encoding valid
    # history or waiting for the whole-cache recovery interval.
    from evolvmem.memory_eligibility import eligible
    if not eligible(service.store, item_id):
        service._sync_context_vector_aftermath((), (item_id,))
    return item_id


def _archive_id(task):
    return int(task['source_key'].split(':')[1]) if task['source_key'].startswith('archive:') else None


def _link_provenance(service, item_id, archive_id, task, unit):
    source_ref = f'{task["source_key"]}#{unit["digest"]}@{unit["source_start"]}-{unit["source_end"]}'
    with service.store.transaction():
        conn = service.store._connection()
        conn.execute(
            "INSERT OR IGNORE INTO context_sources(item_id,archive_id,source_kind,source_ref,"
            "extraction_version,created_at) VALUES(?,?,'organization',?,'auto-organization.v1',?)",
            (item_id, archive_id, source_ref, _now_iso()))
        conn.execute('UPDATE context_items SET source_count=(SELECT COUNT(*) FROM context_sources '
                     'WHERE item_id=?) WHERE id=?', (item_id, item_id))
        service.store.recompute_source_states([item_id])


def extract_unit(service, task, unit, spans, *, llm_config=None):
    """Run the real extraction stage for one assigned unit.

    Returns ``{'stage': ..., 'item_ids': [...], 'candidates': n, 'error': ''}``.
    The provider is only ever the existing one; tests substitute ``kimi_hooks``.
    """
    from evolvmem import kimi_hooks
    from evolvmem.unit_derivations import record_derivation
    messages = topic_segmentation.unit_messages(unit, spans)
    if not messages:
        return {'stage': 'failed', 'item_ids': [], 'candidates': 0,
                'error': 'unit_has_no_verifiable_message'}
    want = signature(unit, unit['project'], task['rule_revision'])
    if unit.get('extraction_stage') == 'done' and unit.get('extraction_signature') == want:
        return {'stage': 'skipped', 'item_ids': [], 'candidates': 0, 'error': ''}
    _set_stage(service, task['id'], unit['digest'], 'running', want, '')
    if llm_config is None:
        llm_config = kimi_hooks._load_llm_config()
    if llm_config is None:
        _set_stage(service, task['id'], unit['digest'], 'failed', want, 'extraction_provider_unavailable')
        return {'stage': 'failed', 'item_ids': [], 'candidates': 0,
                'error': 'extraction_provider_unavailable'}
    from evolvmem.auto_organization import _unit_revision
    from evolvmem.organization_guidance import rows
    before = _unit_revision(unit)
    guidance_before = rows(service)
    phase = 'related_lookup'
    try:
        from evolvmem.learning_extraction import related
        from evolvmem.session_extraction import prepare_extraction
        peers = related(service, unit['project'], messages)
        phase = 'model_parse'
        request = prepare_extraction(
            service.config, unit['project'], f'{task["source_key"]}#{unit["digest"]}', messages,
            llm_config, reviewed_cleaning=unit.get('cleaned_text') or None, related_context=peers)
        # The history record stays the deterministic, source-linked one; the
        # model's session summary is only the extraction anchor.
        # A correction or re-assignment during the model call invalidates this
        # unit's result: the stale answer must not be written under the new state.
        from evolvmem.auto_organization import _assert_current
        phase = 'persistence'
        with service.store.transaction():
            _assert_current(service, task)
            current = service.store._connection().execute(
                'SELECT * FROM organization_units WHERE task_id=? AND digest=?',
                (task['id'], unit['digest'])).fetchone()
            if current is None or _unit_revision(current) != before or rows(service) != guidance_before:
                return {'stage': 'stale', 'item_ids': [], 'candidates': 0, 'error': 'unit_changed'}
            # An inherited project must still rest on a live predecessor at the
            # moment the model's answer is written, not only when it was asked.
            inherited = _decode_context(current['context_basis'])
            if inherited and not _context_current(service, inherited):
                # The predecessor changed while the provider was answering. The
                # answer is discarded and the unit goes back to review inside the
                # same transaction, so the task settles instead of being retried
                # forever against a dead context.
                conn = service.store._connection()
                conn.execute(
                    "UPDATE organization_units SET decision='review',project='',context_basis='',"
                    "reason=?,revision=revision+1,updated_at=? WHERE task_id=? AND digest=?",
                    ('前文依据已变化，退回核对', _now_iso(), task['id'], unit['digest']))
                from evolvmem.organization_context import clear_dependency
                clear_dependency(service, task['id'], unit['digest'], conn=conn)
                return {'stage': 'stale', 'item_ids': [], 'candidates': 0,
                        'error': 'context_basis_changed'}
            request = _with_history_summary(request, task, unit)
            result = service.persist_legacy_extraction(
                request, source_archive_id=_archive_id(task), source_messages=messages)
            item_ids, experience_ids = [], []
            for mutation in result.candidates:
                if mutation is None or mutation.context_id is None:
                    continue
                item_ids.append(mutation.context_id)
                record_derivation(service, task['id'], unit['digest'], mutation.context_id,
                                  kind='knowledge', project=unit['project'])
                detail = service.knowledge().detail(mutation.context_id)
                if detail['content_type'] == 'experience':
                    experience_ids.append(mutation.context_id)
            # A matched duplicate is reused, not rewritten: the unit still points
            # at the existing knowledge so one withdrawal cannot silently drop it.
            for mutation in result.reused:
                if mutation is None or mutation.context_id is None:
                    continue
                item_ids.append(mutation.context_id)
                record_derivation(service, task['id'], unit['digest'], mutation.context_id,
                                  kind='knowledge', project=unit['project'])
            for item_id in experience_ids:
                _bind_experience(service, item_id, messages, archive_id=_archive_id(task))
            _set_stage(service, task['id'], unit['digest'], 'done', want, '')
    except Exception as error:  # provider/parse/persistence failure is visible per unit
        from evolvmem.organization_diagnostics import capture
        diagnostic = capture(error, phase)
        code = str(error) if isinstance(error, ValueError) else type(error).__name__
        if code in ('extraction_provider_unavailable', 'extraction_summary_missing',
                    'extraction_summary_rejected'):
            _set_stage(service, task['id'], unit['digest'], 'failed', want, code, diagnostic)
            return {'stage': 'failed', 'item_ids': [], 'candidates': 0, 'error': code}
        _set_stage(service, task['id'], unit['digest'], 'failed', want, 'extraction_failed', diagnostic)
        return {'stage': 'failed', 'item_ids': [], 'candidates': 0, 'error': 'extraction_failed'}
    return {'stage': 'done', 'item_ids': item_ids, 'candidates': len(item_ids), 'error': ''}


def _decode_context(raw):
    from evolvmem.organization_context import decode
    return decode(raw)


def _context_current(service, basis):
    from evolvmem.organization_context import validate
    try:
        return validate(service, basis, depth=1)
    except Exception:
        # An unreadable predecessor is not proof that the context is live.
        return False


def _with_history_summary(request, task, unit):
    """Replace the model's batch anchor with the deterministic history record."""
    from dataclasses import replace
    summary = _summary_item(unit, unit['project'], unit['digest'])
    return replace(request, summary=summary)


def _set_stage(service, task_id, digest, stage, want, error, diagnostic=''):
    with service.store.transaction():
        service.store._connection().execute(
            'UPDATE organization_units SET extraction_stage=?,extraction_signature=?,extraction_error=?,'
            'extraction_diagnostic=?,updated_at=? WHERE task_id=? AND digest=?',
            (stage, want, str(error or '')[:300], diagnostic, _now_iso(), task_id, digest))


def pending_units(service, task_id):
    return [dict(row) for row in service.store._connection().execute(
        "SELECT * FROM organization_units WHERE task_id=? AND project!='' AND decision!='review' "
        "AND disposition NOT IN ('set_aside','history_only') AND extraction_stage IN ('pending','running') "
        'ORDER BY ordinal', (task_id,))]


def _bind_experience(service, item_id, messages, *, quote='', archive_id=None):
    """Bind an explicit successful result to a native event in this archive only.

    A candidate result must be quoted in its own unit and explicitly describe
    success. The existing resolver then checks the original native tool/user
    event; cleaned prose and assistant claims cannot serve as proof. Unsupported
    archive formats remain candidates without searching unrelated local logs.
    """
    experience = service.experiences()
    payload = experience._payload(item_id)
    proof = str(payload.get('result') or '').strip()
    explicit = bool(re.search(r'成功|通过|验收.{0,8}(正确|完成)|确认.{0,8}(正确|生效)|\bpassed\b|\bsuccess(?:ful)?\b', proof, re.I))
    negative = bool(re.search(
        r'失败|未通过|不正确|未成功|没有成功|尚未|还没|\bfailed?\b|'
        r'(?:如果|希望|预计|预期|计划|应该|应当|期望|目标).{0,20}(?:成功|通过)|'
        r'\b(?:expect|should|would|will|if)\b', proof, re.I))
    if not archive_id or not proof or not explicit or negative:
        _candidate_reason(service, item_id, '缺少可绑定的明确验证结果，保留候选')
        return False
    quoted = [message for message in messages if proof in str(message.get('content', ''))]
    kinds = []
    if quoted:
        # Tool output is removed from the clean history view, but the unit can
        # quote its result. Verify against the native event in the raw archive.
        kinds.append(('tool_result', 'technical'))
    if any(message.get('role') == 'user' for message in quoted):
        kinds.append(('user_confirmation', 'user_confirmed'))
    for kind, level in kinds:
        try:
            bound = experience.source_resolver.resolve(
                source_kind=kind, source_ref=f'archive:{archive_id}', task_id='current', quote=proof)
            experience.outcome(item_id, {
                'task_id': bound.task_id, 'event_id': bound.source_ref,
                'source_ref': bound.source_ref, 'source_kind': kind,
                'outcome': 'success', 'level': level, 'quote': proof,
                'conditions': payload.get('conditions') or {},
                'note': '自动整理：核对本单元明确结果与同源原始事件', 'revision': 1})
            return True
        except (ValueError, KeyError):
            continue
    _candidate_reason(service, item_id, '验证依据无法绑定同源原始事件，保留候选')
    return False


def _candidate_reason(service, item_id, reason):
    with service.store.transaction():
        service.store._connection().execute(
            'INSERT INTO knowledge_metadata(item_id,revision,ingestion_reason) VALUES(?,1,?) '
            'ON CONFLICT(item_id) DO UPDATE SET ingestion_reason=excluded.ingestion_reason',
            (int(item_id), str(reason)[:500]))
