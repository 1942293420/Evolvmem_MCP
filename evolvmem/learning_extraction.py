"""Evidence-checked extraction decisions, shared by preview and atomic ingestion."""
from __future__ import annotations

import hashlib
import json
import re

from evolvmem.context_store import _now_iso
from evolvmem.extraction_policy import contains_sensitive_text

PROCESS_ROLES = {'goal': ('user',), 'understanding': ('assistant',),
                 'correction': ('user',), 'decision': ('user',),
                 'verification': ('user', 'tool', 'assistant')}


def evidence(quote, messages, roles=None):
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 3000 or contains_sensitive_text(quote):
        return None
    for index, message in enumerate(messages):
        role = message.get('role', 'unknown')
        if (roles is None or role in roles) and quote in str(message.get('content', '')):
            return {'quote': quote, 'role': role, 'message_index': index}
    return None


def process_evidence(raw, messages):
    process, errors = {}, []
    if not isinstance(raw, dict):
        return process, errors
    for stage, roles in PROCESS_ROLES.items():
        if stage not in raw:
            continue
        value = raw[stage]
        quote = value.get('quote') if isinstance(value, dict) else value
        found = evidence(quote, messages, roles)
        if found:
            if stage == 'verification':
                # A source role identifies an observation, not success of the method.
                found['verified'] = False
                found['reason'] = '保留原始验证陈述，经验成功仍需独立绑定相关结果'
            process[stage] = found
        else:
            errors.append(stage)
    return process, errors


def normalization(data, messages, value):
    """Derived requirement never replaces the original quote or invents acceptance."""
    raw = data.get('normalization')
    if raw is None:
        return None, []
    if not isinstance(raw, dict):
        return None, ['需求表达格式不完整']
    errors = []
    requirement = raw.get('requirement')
    if (not isinstance(requirement, str) or requirement != value
            or not evidence(data.get('quote'), messages, ('user',))):
        errors.append('需求表达必须与答案一致且有用户原话依据')
    result = {'requirement':value, 'acceptance':[], 'questions':[]}
    for key in ('acceptance', 'questions'):
        items = raw.get(key, [])
        if not isinstance(items, list) or len(items)>10:
            errors.append(key + ' 格式不完整')
            continue
        for text in items:
            if not isinstance(text, str) or not 1 <= len(text) <= 500 or contains_sensitive_text(text):
                errors.append(key + ' 包含不可用内容')
            elif key == 'acceptance' and not evidence(text, messages, ('user',)):
                errors.append('验收要求无法在用户原话中核对')
            else:
                result[key].append(text)
    return result, errors


def related(service, project, messages, *, max_chars=8000):
    """Bounded same-project facts; no vector writes, usage counts or provider call."""
    ids = service.store._connection().execute(
        "SELECT id FROM context_items WHERE status='active' AND "
        "(project=? OR (project='' AND scope='global')) AND content_type NOT IN "
        "('session_summary','project_summary','workstream_checkpoint') ORDER BY updated_at DESC,id DESC LIMIT 160",
        (project,)).fetchall()
    text = ' '.join(str(m.get('content', '')) for m in messages)[-12000:]
    terms = set(re.findall(r'[a-zA-Z_]{3,}|[\u4e00-\u9fff]{2}', text))
    rows = []
    for r in ids:
        row = service.knowledge().detail(r['id'])
        if not service.learning().usable(row) or contains_sensitive_text(row['body']):
            continue
        item = {k: row[k] for k in ('id', 'identity_key', 'project', 'scope', 'revision', 'body')}
        item['category'] = row['learning']['category']
        rows.append((sum(term in row['body'] for term in terms), item))
    result, used = [], 0
    for _, row in sorted(rows, key=lambda r: r[0], reverse=True):
        size = len(json.dumps(row, ensure_ascii=False))
        if used + size > max_chars or len(result) >= 20:
            continue
        used += size
        result.append(row)
    return result


def load_related(config, project, messages):
    if not (config.data_dir / 'memory.db').exists():
        return []
    from evolvmem.context_service import ContextService
    from evolvmem.web_server import _context_mode
    service = ContextService(config)
    try:
        service.initialize(mode=_context_mode(config), adapter='extraction-context')
        return related(service, project, messages)
    finally:
        service.close()


def _text(value) -> str:
    return value if isinstance(value, str) else ''


def _binding_key(item) -> str:
    """The identity key the reviewer saw, without the per-write ``:learn:`` suffix."""
    return str(getattr(item, 'key', '') or '').split(':learn:')[0].casefold()


def _hydrate_qa(service, item):
    """Restore the stored question/answer text for the deterministic gate.

    The learning payload deliberately omits the pair (it lives in
    ``knowledge_qa``), but the gate must compare the *same* text the reviewer
    verified. This reads only the row's own stored Q&A; it never invents text
    and never calls a model.
    """
    data = item.learning if isinstance(item.learning, dict) else None
    if data is None:
        return data
    if isinstance(data.get('question'), str) and data['question'].strip() \
            and isinstance(data.get('answer'), str) and data['answer'].strip():
        return data
    item_id = getattr(item, 'id', None)
    if type(item_id) is not int or service is None:
        return data
    saved = service.store._connection().execute(
        'SELECT question,answer FROM knowledge_qa WHERE item_id=?', (item_id,)).fetchone()
    if saved is None:
        return data
    # A stored row re-enters the gate through ``apply_ingestion`` with the
    # identity key's ``:learn:`` suffix and without the pair, so the stored Q&A
    # is authoritative here; the in-flight candidate keeps the model's fields.
    for field, value in (('question', saved['question']), ('answer', saved['answer'])):
        if value and (':learn:' in str(getattr(item, 'key', '') or '')
                      or not _text(data.get(field)).strip()):
            data[field] = value
    return data


def basis_gate(data, messages, item, service=None):
    """Deterministic basis check before a candidate may become active.

    Optional metadata must never be a bypass: a missing ``question``/``answer``
    pair means "unconfirmed", not "nothing to check", and no message role but a
    real user may supply the quote that makes a memory an explicit requirement.
    A provider that marks its own inference as ``inferred`` keeps the candidate
    isolated even when a user quote exists; the explicit/inferred separation
    stays with the reviewer. The ``normalization`` questions and errors keep
    their own check in ``plan``. Since P1 the explicit pair must additionally
    carry a current independent answer-support verdict (see
    ``answer_support``): a real user quote alone proves only that the user said
    that sentence, never that the answer stayed inside it. Returns
    ``(reason, qa)``; ``qa`` is the validated ``(question, answer)`` pair or
    ``None``.
    """
    from evolvmem.qa_memory import validate
    value = item.value
    if not value:
        return '', None
    if data.get('basis') != 'explicit':
        return '推断或缺少用户明确依据，待确认', None
    if not isinstance(data.get('question'), str) or not data['question'].strip():
        return '缺少完整问答，无法作为已确认用户决定入库', None
    if not isinstance(data.get('answer'), str) or not data['answer'].strip():
        return '缺少答案，无法作为已确认用户决定入库', None
    try:
        question, answer = validate(data.get('question'), data.get('answer'))
    except ValueError:
        return '问答缺失、过长或不可用，需要整理', None
    if answer != value:
        return '问答答案与记忆内容不一致', None
    if not evidence(data.get('quote'), messages, ('user',)):
        return '缺少可在用户对话中逐字核对的原话依据，待确认', None
    from evolvmem import answer_support
    # Pure validation: only a fresh verdict produced by the batch review in
    # ``kimi_hooks``/``preview`` counts. This function never calls a model and
    # never writes network work, so it is safe inside the persist transaction;
    # a direct persist without review metadata stays a candidate.
    # The reviewer saw the model's candidate key; an ingested row re-enters the
    # gate with the resolved identity key (including ``:learn:``), so the key
    # comparison uses the recorded binding while the messages and the stored
    # question/answer/quote text are re-verified against the current row.
    support_reason = answer_support.check_binding(
        data, messages, key=_text(data.get('key')) or _binding_key(item),
        category=data.get('category', ''), trigger=data.get('trigger', ''))
    if support_reason:
        return support_reason, None
    return '', (question, answer)


def plan(service, item, messages, *, policy=None):
    """Do not accept a model's replacement/skip instruction without checking it."""
    kb = service.knowledge()
    policy = policy or kb.rules.read()
    data = item.learning if isinstance(item.learning, dict) else {}
    key = item.key.casefold()
    project = key.split(':')[1] if key.startswith('project:') and len(key.split(':')) >= 4 else ''
    scope = 'global' if key.startswith('user:') else 'project'
    process, errors = process_evidence(data.get('process'), messages)
    quote = evidence(data.get('quote'), messages, ('user',))
    action = data.get('action', 'add')
    target = None
    if type(data.get('target_id')) is int:
        try:
            target = kb.detail(data['target_id'])
        except ValueError:
            pass
    relation = {'action': action, 'target_id': data.get('target_id'),
                'target_revision': data.get('target_revision', '')}
    decision = kb.rules.evaluate({'body': item.value, 'project': project, 'key': key,
        'scope': scope, 'source': bool(messages), 'confidence': item.confidence if item.confidence is not None else .9,
        'content_type': item.attribute}, kb.registry(), policy=policy)
    # Exact content plus scope is a duplicate even when the model changes the key.
    # History summaries are records, not knowledge peers: the same text in a
    # session summary must never make its own extracted candidate look like a
    # duplicate of itself.
    peers = service.store._connection().execute(
        "SELECT id FROM context_items WHERE project=? AND scope=? AND status NOT IN ('deleted','superseded') "
        "AND content_type NOT IN ('session_summary','project_summary','workstream_checkpoint')",
        (project, scope)).fetchall()
    # The explicit basis/answer-support gate is a pure check: run it before the
    # duplicate shortcut so a failed or missing review can never be bypassed by
    # "the same body already exists".
    gate_data = _hydrate_qa(service, item)
    qa_reason, qa = basis_gate(gate_data, messages, item, service)
    blocked_reuse = ''
    same_key = []
    for peer in peers:
        row = kb.detail(peer['id'])
        if (row['body'] == item.value and row['learning'].get('trigger', '') == data.get('trigger', '')
                and row['learning'].get('category') == data.get('category', row['learning'].get('category'))):
            if data.get('basis') == 'explicit' and qa_reason:
                # This round's own evidence did not pass the deterministic gate:
                # never re-use the old active row as the source of record for it.
                # The old row keeps its status; the round stays a candidate and
                # no new active re-use relation is created.
                blocked_reuse = qa_reason
                continue
            return {'action': 'skip', 'status': row['status'], 'reason': '已有同范围相同内容，保留现有版本',
                    'target_id': row['id'], 'relation': relation, 'process': process,
                    'rule_revision': policy['revision'], 'body': item.value}
        if row['identity_key'].split(':learn:')[0] == key and row['status'] == 'active':
            same_key.append(row)
    reason = None
    target_ok = target and target['project'] == project and target['scope'] == scope and service.learning().usable(target)
    if action not in ('add', 'supplement', 'replace', 'skip'):
        reason = '提炼动作不明确'
    elif action == 'skip':
        reason = '未找到可证明重复的相同资料，保留待确认'
    elif action in ('replace', 'supplement'):
        if not target_ok:
            reason = '关联资料不存在、已失效或超出当前范围'
        elif data.get('target_revision') != target['revision']:
            reason = '关联资料版本已变化，需要重新对照'
        elif action == 'replace':
            correction = process.get('correction', {}).get('quote', '')
            if not (quote and data.get('basis') == 'explicit' and target['body'] in correction
                    and item.value in correction and re.search(r'改为|改成|替换|不再', correction)):
                reason = '替代缺少同时指明旧内容和新要求的用户原话'
            elif target['managed_content'] or target['learning'].get('category') == 'experience':
                reason = '结构化经验或任务不能由提炼自动替代'
    elif same_key:
        reason = '同一标识已有不同内容，需要明确补充或替代关系'
    if errors or (data.get('quote') and not evidence(data['quote'], messages)):
        reason = '原话或协作过程无法核对：' + ', '.join(errors)
    if qa_reason:
        reason = qa_reason if blocked_reuse else (reason or qa_reason)
    if qa is not None:
        from evolvmem.qa_memory import conflicts
        if action != 'replace' and conflicts(service, project, scope, qa[0], qa[1], data.get('category', 'reference'), data.get('trigger', '')):
            reason = reason or '同一问题和条件已有不同答案，待确认'
    normalized, normalization_errors = normalization(data, messages, item.value)
    if normalization_errors or (normalized and normalized['questions']):
        reason = '需求表达存在待确认问题或缺少依据：' + '；'.join(normalization_errors or normalized['questions'])
    if data.get('category') == 'experience':
        reason = '经验方法须绑定实际验证结果，提炼和人工入库不能替代验证'
    if reason:
        decision.update(action='review', reason=reason)
    status = {'auto': 'active', 'review': 'candidate', 'ignore': 'archived'}[decision['action']]
    return {'action': action if action in ('add', 'supplement', 'replace') else 'add',
            'status': status, 'reason': decision['reason'], 'decision': decision,
            'target_id': target['id'] if target else None, 'relation': relation, 'process': process,
            'process_errors': errors, 'rule_revision': policy['revision'], 'body': item.value,
            'normalization': normalized, 'normalization_errors': normalization_errors,
            'answer_support': data.get('answer_support') if isinstance(data, dict) else None,
            'auto_explicit_rules': policy['settings']['project_overrides'].get(project, {}).get(
                'auto_explicit_rules', policy['settings']['auto_explicit_rules'])}


def persist(service, item, messages, source_session, archive_id):
    """Caller holds the extraction transaction; no vector I/O until it commits."""
    from evolvmem.context_models import ContextLayer
    from evolvmem.context_service import _VectorAftermath
    from evolvmem.legacy_models import LegacyMutationResult, LegacyAddRequest
    kb = service.knowledge()
    policy = kb.rules.read()
    metadata = item.learning or {}
    if metadata.get('rule_revision') and metadata['rule_revision'] != policy['revision']:
        raise ValueError('extraction_rules_changed')
    result = plan(service, item, messages, policy=policy)
    if result['action'] == 'skip':
        if archive_id is not None:
            service.store.record_session_source(result['target_id'], archive_id, extraction_version='learning-qa.v1')
        # Report the reused entity through ``reused`` (changed=False) instead of
        # None, so a per-unit caller can still track which knowledge backs it
        # without counting another write or rewriting its metadata.
        return (LegacyMutationResult(legacy_id=None, context_id=result['target_id'], changed=False,
                                     old_legacy_id=None, old_context_id=None, available_layers=()),
                _VectorAftermath())
    category = metadata.get('category', '')
    decision = result['decision']
    # ``result['body']`` is authoritative: a ``narrow`` verdict may have
    # replaced the answer with a real user fragment, and the stored row, the
    # Q&A and the source digest must all use that same corrected text.
    body = result['body']
    # Always append a versioned source; only a proven correction supersedes a predecessor.
    digest = hashlib.sha256(json.dumps([item.key, body, metadata.get('trigger', ''), result['relation']], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    added, aftermath = service._add_dual_in_transaction(LegacyAddRequest(
        key=item.key + ':learn:' + digest, value=body, attribute=item.attribute,
        tags=item.tags, confidence=item.confidence, importance=item.importance,
        tier='normal' if category == 'task_requirement' else item.tier, expires_at=item.expires_at,
        source_session=source_session, project_hint=decision['project']))
    cid = added.context_id
    removed, old_legacy = [], []
    if result['action'] == 'replace' and result['status'] == 'active':
        old = kb.detail(result['target_id'])
        removed.append(old['id'])
        old_legacy = old['legacy_ids']
        kb.conn.execute("UPDATE context_items SET status='superseded',superseded_by=?,updated_at=? WHERE id=?",
                        (cid, _now_iso(), old['id']))
        kb.conn.execute('UPDATE context_items SET supersedes=? WHERE id=?', (old['id'], cid))
        for lid in old_legacy:
            service.store.legacy_projection().mirror_superseded(lid, superseded_by=added.legacy_id)
        if old_legacy:
            kb.conn.execute('UPDATE memories SET supersedes=? WHERE id=?', (old_legacy[0], added.legacy_id))
    kb._apply_decision(kb.detail(cid), decision)
    kb.conn.execute("INSERT INTO context_sources(item_id,source_kind,source_ref,extraction_version,created_at) VALUES(?,'extraction',?,'learning-p1.v1',?)",
                    (cid, source_session, _now_iso()))
    captured = {**metadata, 'process': result['process'], 'process_errors': result['process_errors'],
                'relation': result['relation'], 'intake': result['status'], 'rule_revision': policy['revision'],
                'answer_support': result.get('answer_support'),
                'auto_explicit_rules': result['auto_explicit_rules']}
    service.learning().capture(cid, captured, messages=messages, source_session=source_session, archive_id=archive_id)
    return (LegacyMutationResult(legacy_id=added.legacy_id, context_id=cid, changed=True, old_legacy_id=old_legacy[0] if old_legacy else None,
                old_context_id=result['target_id'] if removed else None,
                context_status=result['status'], available_layers=(ContextLayer.L0, ContextLayer.L1, ContextLayer.L2)),
            _VectorAftermath(context_upserts=aftermath.context_upserts if result['status'] == 'active' else (),
                             context_removals=tuple(removed), legacy_upserts=aftermath.legacy_upserts))
