"""Canonical source verdicts; storage callbacks stay lazy and transaction-owned."""
import json
from evolvmem.knowledge_scope import projects as _projects


def currently_backed(rows, conversation_archives, sources):
    """The existing unit_derivations verdict, with named lazy read seams."""
    if not rows:
        return True
    if any(r['status'] != 'superseded' and r['decision'] in ('auto', 'manual')
           and r['disposition'] == 'keep' and (
               (r['knowledge_scope'] == r['item_scope'] == 'global') or
               (r['knowledge_scope'] == r['item_scope'] == 'project' and
                set(_projects(dict(r))) & set(_projects({'project':r['item_project'],'projects':r['item_projects']}))))
           for r in rows):
        return True
    keys = {r['source_key'] for r in rows}
    archives = {int(k.split(':')[1]) for k in keys if k.startswith('archive:')}
    for key in keys:
        if key.startswith('conversation:'):
            archives.update(conversation_archives(key))
    for source in sources():
        if source['source_kind'] == 'session' and source['archive_id'] in archives:
            continue
        if source['source_kind'] in ('organization', 'extraction') and any(
                source['source_ref'] == key or source['source_ref'].startswith(key + '#') for key in keys):
            continue
        if source['archive_id'] in archives:
            continue
        return True
    return False


def unconfirmed_rule(category, payload, human_accepted, manual_source):
    """Preserve old malformed/unknown payload behavior; not a general proof."""
    if category not in ('habit', 'project_convention', 'task_requirement') or not payload:
        return False
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    evidence = data.get('evidence', [])
    if (data.get('basis') != 'inferred' or not evidence
            or not isinstance(evidence, list)
            or any(not isinstance(e, dict) or e.get('role') not in ('assistant', 'tool') for e in evidence)):
        return False
    # Both reads occur in the original implementation; don't cache a verdict.
    protected, manual = human_accepted(), manual_source()
    return not (protected or manual)


def eligible_row(row, unconfirmed, summary_current):
    if row is None:
        return False
    if unconfirmed(row['category'], row['learning_payload']):
        return False
    if row['content_type'] == 'project_summary':
        return summary_current()
    if row['content_type'] != 'experience' and row['category'] != 'experience':
        return True
    return row['status'] == 'active' and row['success_count'] > 0 and bool(row['experience_payload'])


def rollup_content_signature(rows):
    """Canonical signature over sorted (id, project, type, L1 content) rows."""
    import hashlib
    return 'content-v1:' + hashlib.sha256(
        json.dumps([tuple(row) for row in rows], ensure_ascii=False).encode()).hexdigest()


def summary_current(summary, ids, rows, *, now, backed, unconfirmed,
                    signature, content_signature):
    """The existing rollup verdict, with lazy storage seams for SQLite and PG.

    It is a content qualification rule, never an ACL or promotion decision.
    In particular legacy summaries with no derived links retain old behavior.
    """
    from evolvmem.context_temporal import window_contains
    if not ids:
        return True
    rows = rows()
    if summary is None or len(rows) != len(ids):
        return False
    for row in rows:
        if (row['status'] != 'active' or row['project'] != summary['project']
                or (row['expires_at'] and row['expires_at'] <= now)
                or not window_contains(row['effective_from'], row['effective_until'], now)
                or not backed(row['id']) or unconfirmed(row['id'])):
            return False
    value = signature()
    if value.startswith('content-v1:'):
        return value == content_signature()
    return all(row['updated_at'] <= summary['created_at'] for row in rows)
