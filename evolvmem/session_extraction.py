"""Codex adapter for the existing extraction policy and ContextService writes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

from evolvmem import kimi_hooks
from evolvmem.extraction_policy import evaluate_candidate, rank_candidates, redact_messages, sanitize_summary
from evolvmem.legacy_models import LegacyExtractionItem, LegacyExtractionRequest
from evolvmem.lan_sharing import LanError


def prepare_extraction(config, project, source_session, messages, llm_config):
    """Only provider calls happen here; persistence uses the shared core later."""
    safe_messages, _ = redact_messages(messages)
    summary, candidates = kimi_hooks._split_summary_candidate(
        kimi_hooks._extract_candidates(safe_messages, llm_config))
    if summary is None:
        raise LanError('extraction_summary_missing')
    value, _ = sanitize_summary(summary.value)
    if value is None or not kimi_hooks._summary_value_is_persistable(config, value):
        raise LanError('extraction_summary_rejected')
    stable_session = hashlib.sha256(source_session.encode()).hexdigest()[:32]
    summary_item = LegacyExtractionItem(
        key=f'project:{project}:progress:log:codex-{stable_session}',
        value=value, attribute='fact', tags=('日志', f'分类:{project}'),
        confidence=1.0, importance=5.0, tier='normal',
        expires_at=(datetime.now(timezone.utc) + timedelta(days=config.context_session_summary_ttl_days)).strftime('%Y-%m-%d'))
    eligible = [c for c in candidates if evaluate_candidate(c, value_min_chars=config.value_min_chars,
                                                          value_max_chars=config.value_max_chars).accepted]
    items = []
    for candidate in rank_candidates(eligible, limit=None):
        key = candidate.key.casefold()
        if key.startswith('project:'):
            parts = key.split(':')
            if len(parts) < 4:
                continue
            parts[1] = project
            key = ':'.join(parts)
        elif not key.startswith('user:'):
            continue
        case = {**candidate.experience_case, 'project': project} if candidate.experience_case else None
        items.append(LegacyExtractionItem(
            key=key, value=candidate.value.strip(), attribute=candidate.attribute,
            tags=tuple(candidate.tags), confidence=candidate.confidence,
            importance=candidate.importance, tier=candidate.tier, experience_case=case))
    return LegacyExtractionRequest(summary=summary_item, candidates=tuple(items),
                                   max_writes=8, source_session=source_session)
