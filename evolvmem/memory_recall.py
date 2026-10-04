"""Separate history and experience recall, with an explicit intent override."""
from __future__ import annotations

import re

from evolvmem import history_memory, qa_memory
from evolvmem.memory_learning import CATEGORIES


def intent(query):
    history = bool(re.search(r'历史|以前|之前|上次|曾经|讨论过|说过|聊过|做到哪|进展|history|last time', query, re.I))
    experience = bool(re.search(r'经验|习惯|约定|规范|类似|如何|怎么|调研|开发流程|排查|配置|experience|how to', query, re.I))
    return 'both' if history and experience else 'history' if history else 'experience' if experience else 'both'


def relevance(query, text):
    terms = set(re.findall(r'[a-zA-Z_]{3,}|[\u4e00-\u9fff]{2}', query.casefold()))
    return sum(term in text.casefold() for term in terms)


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
    blocks = [header] if budget >= len(header) else []
    remaining = budget - sum(map(len, blocks))
    if kind in ('history', 'both') and project:
        histories = history_memory.sessions(service, project)
        histories.sort(key=lambda r: relevance(query, r['summary']), reverse=True)
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
                           "WHERE q.status='active' AND (i.project=? OR (i.project='' AND i.scope='global')) "
                           "ORDER BY q.updated_at DESC,q.item_id DESC", (project,)).fetchall()
        rows = [qa_memory.detail(service, r[0]) for r in ids]
        rows = [r for r in rows if r['effective']]
        rows.sort(key=lambda r: relevance(query, r['question']+' '+r['answer']+' '+r['trigger']), reverse=True)
        seen = set()
        for row in rows:
            signature = (row['project'],row['scope'],row['category'],row['trigger'],row['question'],row['answer'])
            if signature in seen:
                continue
            seen.add(signature)
            part = f"〔经验问答 #{row['id']} · {row['project'] or '全局'} · {CATEGORIES[row['category']]}〕\n问：{row['question']}\n答：{row['answer']}\n"
            if row['trigger']:
                part += '适用：'+row['trigger']+'\n'
            if len(part) > remaining:
                continue
            blocks.append(part); remaining -= len(part)
            result['qa'].append({k:row[k] for k in ('id','question','answer','category','trigger','project','scope')})
            result['selected_ids'].append(row['id'])
            if len(result['qa']) >= 8:
                break
    result['block'] = ''.join(blocks) if result['history'] or result['qa'] else ''
    result['used_chars'] = len(result['block'])
    result['selected_ids'] = list(dict.fromkeys(result['selected_ids']))
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
