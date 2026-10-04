"""One project document derived from current admitted fragments and dialogue sources.

Fragments remain authoritative. Snapshots refresh on writes; reads assemble the
current version so a correction, expiry or project move can never expose a stale
cached sentence as current knowledge. No model is called by these read paths.
"""
from __future__ import annotations
import hashlib
import json
from collections import OrderedDict
from evolvmem.context_store import _now_iso
from evolvmem.context_temporal import window_contains
from evolvmem.project_ownership import load_ownership, UNREVIEWED_FACT
from evolvmem.extraction_policy import redact_messages

_LABELS = {'habit':'长期习惯','project_convention':'项目约定','task_requirement':'任务要求',
           'environment':'环境设置','decision':'决策依据','experience':'技术经验','reference':'参考知识'}


def document(service, project, *, include_sessions=True):
    kb = service.knowledge()
    kb._project(project)
    if not project:
        raise ValueError('project_required')
    conn = service.store._connection()
    rows = conn.execute("SELECT id,content_type,updated_at FROM context_items WHERE project=? AND scope='project' AND status='active' AND content_type IN ('session_summary','workstream_checkpoint') AND (expires_at IS NULL OR expires_at='' OR expires_at>?) ORDER BY updated_at DESC,id DESC", (project, _now_iso())).fetchall()
    facts = load_ownership(service.store, [r['id'] for r in rows])
    sections, ids, digest_parts, seen = OrderedDict(), [], [], {}
    for entry in rows:
        if facts.get(entry['id'], UNREVIEWED_FACT).excluded:
            continue
        # Previous rollups are derived historical snapshots; including them
        # would resurrect corrected or moved facts and duplicate the sources.
        if entry['content_type'] == 'project_summary':
            continue
        row = kb.detail(entry['id'])
        if not window_contains(row['effective_from'], row['effective_until'], _now_iso()):
            continue
        category = row['learning']['category']
        label = ('历次会话摘要' if entry['content_type']=='session_summary' else
                 '任务进展记录' if entry['content_type']=='workstream_checkpoint' else _LABELS.get(category,'参考知识'))
        text = redact_messages([{'role':'assistant','content':row['body']}])[0][0]['content'].strip()
        if not text:
            continue
        signature=(label,text,row['learning'].get('trigger',''))
        ids.append(row['id']);digest_parts.append(row['revision'])
        if signature in seen:
            seen[signature]['source'] += f" [#{row['id']}]"
            continue
        note = f"〔来源 #{row['id']} · {row['updated_at'][:10]}〕"
        trigger = row['learning'].get('trigger','')
        part = {'id':row['id'],'text':text,'trigger':trigger,'source':note}
        seen[signature] = part
        sections.setdefault(label, []).append(part)
    # Agreements come before dated summaries, which are history rather than
    # a permanent instruction. Keep one coherent document with clear headings.
    order = [v for v in _LABELS.values() if v in sections] + [v for v in ('任务进展记录','历次会话摘要') if v in sections]
    blocks, summary_lines = [], []
    for label in order:
        entries=sections[label]
        blocks.append('## '+label+'\n\n'+'\n\n'.join(e['text']+('\n适用：'+e['trigger'] if e['trigger'] else '')+'\n'+e['source'] for e in entries))
        summary_lines.append(label+'：'+'；'.join(e['text'].replace('\n',' ')[:140]+f" [#{e['id']}]" for e in entries[:2]))
    name=next((r['display_name'] or project for r in kb.registry() if r['project']==project),project)
    body='# '+name+' · 项目总记忆\n\n'+'\n\n'.join(blocks) if blocks else ''
    summary='\n'.join(summary_lines)
    if len(summary)>1800:
        summary=summary[:1760]+'\n…其余内容见总记忆正文。'
    revision=hashlib.sha256(json.dumps([project,name,digest_parts],ensure_ascii=False).encode()).hexdigest()
    result={'project':project,'name':name,'body':body,'summary':summary,'revision':revision,'source_ids':ids,'sessions':[]}
    from evolvmem import qa_memory, history_memory
    result['qa_count'] = qa_memory.list_items(service, {'project': project, 'state': 'active'})['total']
    if include_sessions:
        result['sessions'] = history_memory.sessions(service, project)
        # Product history owns rollups. Keep old derived prose out of current
        # recall; show it explicitly as a dated historical snapshot in the UI.
        rollups = service.insights().summaries({'project': project})['rows']
        result['project_summary'] = (service.insights().summary(rollups[0]['id'])
                                     if rollups and rollups[0]['id'] is not None else None)
    return result


def refresh(service, project):
    if not project:
        return
    doc=document(service,project,include_sessions=False)
    with service.store.transaction():
        service.store._connection().execute('INSERT INTO project_memory_documents(project,summary,body,source_ids,revision,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET summary=excluded.summary,body=excluded.body,source_ids=excluded.source_ids,revision=excluded.revision,updated_at=excluded.updated_at',
            (project,doc['summary'],doc['body'],json.dumps(doc['source_ids']),doc['revision'],_now_iso()))


def conversation(service, project, archive_id):
    from evolvmem.history_memory import read
    return read(service, project, archive_id)


def recall_summary(service, project, max_chars):
    """Bounded whole-project overview, without refreshing the saved snapshot."""
    if max_chars < 120:
        return '', ()
    try:
        doc = document(service, project, include_sessions=False)
    except (AttributeError, ValueError):
        return '', ()
    if not doc['summary']:
        return '', ()
    header = '项目总记忆（历史摘要，仅作参考，不能覆盖当前要求）：\n'
    text = header + doc['summary']
    if len(text) > max_chars:
        text = text[:max_chars-15]+'…〔其余内容已省略〕'
    import re
    ids = tuple(int(i) for i in re.findall(r'\[#(\d+)\]', text))
    return text, ids
