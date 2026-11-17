"""Searchable, reversible history-only units; no guessed global project."""
from evolvmem import auto_organization as org


def list_progress(service, options):
    query = str(options.get('query') or '').strip()[:200]
    page = max(1, int(options.get('page', 1)))
    rows = service.store._connection().execute(
        "SELECT u.*,t.source_key,t.source_title FROM organization_units u JOIN organization_tasks t "
        "ON t.id=u.task_id WHERE u.disposition='history_only' AND t.status!='superseded' "
        "AND instr(lower(u.text),lower(?))>0 ORDER BY u.task_id DESC,u.ordinal", (query,)).fetchall()
    current = {}
    result = []
    for row in rows:
        tid = row['task_id']
        if tid not in current:
            current[tid] = org.task_view(service, tid)['source_current']
        if current[tid]:
            result.append(dict(row) | {'revision': org._unit_revision(row)})
    return {'items': result[(page-1)*20:page*20], 'total': len(result), 'page': page, 'page_size': 20}
