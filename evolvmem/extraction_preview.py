"""Run the real extraction contract on an editor draft without persisting it."""
from __future__ import annotations

from evolvmem.auto_extractor import AutoExtractor
from evolvmem.extraction_policy import evaluate_candidate, rank_candidates, redact_messages
from evolvmem.learning_extraction import plan, related


def preview(service, body, *, llm=None):
    messages = body.get('messages')
    if (not isinstance(messages, list) or not 1 <= len(messages) <= 100
            or any(not isinstance(m, dict) or m.get('role') not in ('user', 'assistant', 'tool')
                   or not isinstance(m.get('content'), str) for m in messages)
            or sum(len(m['content']) for m in messages) > 20000):
        raise ValueError('invalid_preview_messages')
    project = str(body.get('project') or '')
    service.knowledge()._project(project)
    if not project:
        raise ValueError('project_required')
    from evolvmem.conversation import clean_messages
    rules = service.knowledge().rules
    current = rules.read()
    draft = rules.prepare(body['rules']) if body.get('rules') else current
    cleaned = clean_messages(messages, policy=current)
    safe, redacted = redact_messages(cleaned)
    old = related(service, project, safe) if body.get('include_related') is True else []
    extractor = AutoExtractor()
    if llm is None:
        from evolvmem.kimi_hooks import _load_llm_config, _call_llm_with_retry
        config = _load_llm_config()
        if config is None:
            raise ValueError('extraction_provider_unavailable')
        llm = lambda prompt: _call_llm_with_retry(prompt, config)
    # ``calls`` counts every real provider callback of this preview, including
    # the independent answer review (one extraction + one review per version).
    calls = []

    def provider(prompt):
        calls.append(prompt)
        return llm(prompt)

    def run(policy):
        started = len(calls)
        safe, _ = redact_messages(clean_messages(messages, policy=policy))
        prompt = extractor.build_extraction_prompt(safe, policy=policy, related=old)
        candidates = extractor.parse_response(provider(prompt))
        # Same shared independent review as the ingestion batch, before ``plan``,
        # so the preview shows the same verdict, correction and reasons. Keys are
        # normalized to the selected project first, exactly like ingestion.
        from evolvmem import answer_support
        for candidate in candidates:
            answer_support.normalize_key(candidate, project)
        _, replacements = answer_support.support(safe, candidates, provider)
        candidates = [replacements.get(id(c), c) for c in candidates]
        # The same history-only drop as ingestion: an un-adopted assistant
        # suggestion is not shown as a preview candidate either.
        candidates = answer_support.drop_history_only(candidates)
        if not any(c.key.strip().upper() == 'SESSION_SUMMARY' for c in candidates):
            raise ValueError('extraction_summary_missing')
        results, seen, writes = [], set(), 0
        for item in rank_candidates(candidates, limit=None):
            if item.key.strip().upper() == 'SESSION_SUMMARY':
                continue
            check = evaluate_candidate(item, value_min_chars=service.config.value_min_chars,
                                       value_max_chars=service.config.value_max_chars)
            if not check.accepted:
                results.append({'body': item.value, 'action': 'skip', 'status': 'ignored', 'reason': check.reason})
                continue
            if item.key.startswith('project:'):
                parts = item.key.split(':')
                if len(parts) < 4:
                    continue
                parts[1] = project
                item.key = ':'.join(parts)
            elif not item.key.startswith('user:'):
                continue
            result = plan(service, item, safe, policy=policy)
            # ``action`` mirrors the real ingestion decision (review included);
            # the raw model action stays in ``decision``.
            result['action'] = result['decision']['action'] if result['action'] != 'skip' else 'skip'
            result['question'] = (item.learning or {}).get('question', '')
            result['answer'] = (item.learning or {}).get('answer', '')
            result['category'] = (item.learning or {}).get('category', '')
            from evolvmem.learning_extraction import evidence
            quote = evidence((item.learning or {}).get('quote'), safe, ('user',))
            result['evidence'] = [quote] if quote else []
            signature = (item.key.startswith('user:'), item.value, result['category'], (item.learning or {}).get('trigger', ''))
            if signature in seen:
                result.update(action='skip', reason='同一批次已有同范围相同内容与条件')
            elif result['action'] != 'skip':
                if writes >= 8:
                    result.update(action='skip', reason='达到本次 8 条原子知识写入上限')
                else:
                    writes += 1
                    seen.add(signature)
            results.append(result)
        return {'rule_revision': policy['revision'], 'skill': policy['skill'], 'prompt': prompt,
                'candidates': results, 'related_ids': [r['id'] for r in old], 'cleaned_messages':safe,
                'model_calls': len(calls) - started}

    before = run(current)
    after = run(draft) if draft['revision'] != current['revision'] else before
    return {'current': before, 'draft': after, 'redacted': redacted,
            'model_calls': len(calls), 'persisted': 0,
            'cleaning': {'input_messages': len(messages), 'dialogue_messages': len(cleaned), 'removed': len(messages)-len(cleaned)}}
