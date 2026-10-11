"""Separate history and experience recall, with an explicit intent override."""
from __future__ import annotations

import re
import math

from evolvmem import history_memory, qa_memory, knowledge_scope as ks
from evolvmem.memory_learning import CATEGORIES


def intent(query):
    history = bool(re.search(r'历史|以前|之前|上次|曾经|讨论过|说过|聊过|做到哪|进展|history|last time', query, re.I))
    experience = bool(re.search(r'经验|习惯|约定|规范|类似|如何|怎么|调研|开发流程|排查|配置|experience|how to', query, re.I))
    return 'both' if history and experience else 'history' if history else 'experience' if experience else 'both'


def relevance(query, text):
    from evolvmem.experience_service import _terms, _LOW_SIGNAL_TERMS
    terms = _terms(query) - _LOW_SIGNAL_TERMS - {'什么','以前','之前','上次','经验','类似','应该','需要','怎样','可以','进行','讨论','过什','么做'}
    return sum(term in text.casefold() for term in terms)


def _browse(query):
    return not query.strip() or bool(re.fullmatch(
        r'(?:以前|之前|上次|历史)?(?:讨论过什么|聊过什么|说过什么|有哪些历史|历史记录)[？?\s]*', query.strip()))


def _rank_lane(service, query, rows, text, ids, channels):
    """Rank only already-eligible lane rows; never admit raw/draft knowledge."""
    if _browse(query):
        channels.add('browse')
        return rows
    from evolvmem.context_models import ContextSearchRequest
    allowed = tuple(dict.fromkeys(item_id for row in rows for item_id in ids(row)))
    semantic = {}
    if allowed and service.retriever is not None:
        matches = service.retriever.search(ContextSearchRequest(query=query, top_k=20, cross_project=True), eligible_ids=allowed)
        for position, match in enumerate(matches):
            for item_id in (match.id, *match.duplicate_ids):
                semantic[item_id] = position
            channels.update(m.value for m in match.match_types)
    scored = [(row, relevance(query, text(row)), min((semantic[i] for i in ids(row) if i in semantic), default=9999)) for row in rows]
    scored.sort(key=lambda r: (r[2] == 9999, -r[1], r[2]))
    engine = service.embedding_engine
    rank_scores = None
    if getattr(engine, 'reranker_loaded', False) and scored:
        # Full eligible Q&A fits this bound; larger history pools first use
        # semantic and lexical candidates. Query, answer and trigger all rank.
        scored = scored[:200]
        try:
            values = engine.rerank(query, [text(row)[:4000] for row, _, _ in scored])
            if len(values) != len(scored) or any(not math.isfinite(v) or not 0 <= v <= 1 for v in values):
                raise ValueError('invalid_reranking_response')
            rank_scores = values
            channels.add('rerank')
        except Exception:
            pass
    kept = []
    for position, (row, lexical, semantic_rank) in enumerate(scored):
        rank_score = None if rank_scores is None else rank_scores[position]
        if lexical <= 0 and semantic_rank == 9999 and (rank_score is None or rank_score < service.config.context_reranker_min_score):
            continue
        if lexical <= 0 and semantic_rank == 9999:
            from evolvmem.query_anchors import supports_named_subject
            if not supports_named_subject(query, text(row) + '\n' + row.get('project', '')):
                continue
        if lexical > 0:
            channels.add('lexical')
        kept.append((row, rank_score, semantic_rank, lexical))
    kept.sort(key=lambda r: (-(r[1] or 0), r[2], -r[3]))
    return [row for row, *_ in kept]


def project_name(service, project):
    if not isinstance(project, str):
        raise ValueError('invalid_project')
    if not project:
        return ''
    for row in service.knowledge().registry():
        if row['status'] == 'active' and (row['project'] == project or project in row['aliases']):
            return row['project']
    raise ValueError('project_not_found')


def recall(service, options):
    query = options.get('query', '')
    if not isinstance(query, str) or len(query) > 4000:
        raise ValueError('invalid_query')
    project = project_name(service, options.get('project', ''))
    kind = options.get('kind', 'auto')
    if kind == 'auto':
        kind = intent(query)
    if kind not in ('history', 'experience', 'both'):
        raise ValueError('invalid_memory_kind')
    budget = min(16000, max(1, int(options.get('max_chars') or 4000)))
    header = '记忆仅作历史参考，当前用户要求优先；经验入库不代表验证成功。\n'
    result = {'kind': kind, 'project': project, 'history': [], 'qa': [], 'selected_ids': []}
    channels = set()
    blocks = [header] if budget >= len(header) else []
    remaining = budget - sum(map(len, blocks))
    if kind in ('history', 'both') and project:
        histories = history_memory.sessions(service, project)
        histories = [row for row in histories if row['summary']]
        histories = _rank_lane(service, query, histories, lambda r: r['summary'], lambda r: r['source_ids'], channels)
        allowance = remaining // 2 if kind == 'both' else remaining
        for row in histories[:8]:
            if not row['summary']:
                continue
            part = f"〔历史对话 #{row['id']} · {row['created_at'][:10]}〕\n{row['summary']}\n"
            if len(part) > allowance:
                continue
            blocks.append(part); allowance -= len(part); remaining -= len(part)
            result['history'].append({'archive_id':row['id'], 'summary':row['summary'], 'date':row['created_at'],
                                      'read_tool':'conversation_read', 'project':project})
            result['selected_ids'].extend(row['source_ids'])
    if kind in ('experience', 'both'):
        conn = service.store._connection()
        ids = conn.execute("SELECT q.item_id FROM knowledge_qa q JOIN context_items i ON i.id=q.item_id "
                           f"WHERE q.status='active' AND ({ks.match_sql('i')} OR (i.project='' AND i.scope='global')) "
                           "ORDER BY q.updated_at DESC,q.item_id DESC", (project,)).fetchall()
        rows = [qa_memory.detail(service, r[0]) for r in ids]
        rows = [r for r in rows if r['effective']]
        rows = _rank_lane(service, query, rows, lambda r: r['question']+'\n'+r['answer']+'\n'+r['trigger'], lambda r: (r['id'],), channels)
        seen = set()
        for row in rows:
            signature = (row['project'],row['scope'],row['category'],row['trigger'],row['question'],row['answer'])
            if signature in seen:
                continue
            seen.add(signature)
            part = f"〔经验问答 #{row['id']} · {ks.label(row)} · {CATEGORIES[row['category']]}〕\n问：{row['question']}\n答：{row['answer']}\n"
            if row['trigger']:
                part += '适用：'+row['trigger']+'\n'
            if len(part) > remaining:
                continue
            blocks.append(part); remaining -= len(part)
            result['qa'].append({k:row[k] for k in ('id','question','answer','category','trigger','project','scope','projects')})
            result['selected_ids'].append(row['id'])
            if len(result['qa']) >= 8:
                break
    result['block'] = ''.join(blocks) if result['history'] or result['qa'] else ''
    result['used_chars'] = len(result['block'])
    result['selected_ids'] = list(dict.fromkeys(result['selected_ids']))
    result['recall_channels'] = sorted(channels)
    return result


def read_conversation(service, options):
    project = project_name(service, options.get('project', ''))
    archive_id = int(options['archive_id'])
    offset = max(0, int(options.get('offset', 0)))
    limit = min(16000, max(1, int(options.get('max_chars') or 6000)))
    row = history_memory.read(service, project, archive_id)
    text = row['text']
    end = min(len(text), offset+limit)
    return {'archive_id':archive_id, 'project':project, 'available':row['available'], 'storage':row['storage'],
            'text':text[offset:end], 'offset':offset, 'next_offset':end if end < len(text) else None,
            'total_chars':len(text), 'used_chars':len(text[offset:end])}
