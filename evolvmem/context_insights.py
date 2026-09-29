"""Read-only projections for the existing local memory management console.

No legacy-ID substitution, vector initialization, LLM calls or usage writes.
An operator can inspect Core in compat mode without enabling Agent serving.
"""
import json

from evolvmem.context_models import ContextLayer
from evolvmem.context_store import _now_iso


_CASES = "i.experience_payload != '' AND i.status != 'deleted'"
_CURRENT = "(i.expires_at IS NULL OR i.expires_at > datetime('now'))"
_VERIFIED = f"i.status='active' AND i.success_count>0 AND {_CURRENT}"
_CASE_FIELDS = ('id', 'project', 'problem', 'conditions', 'status', 'validation_level',
                'success_count', 'failure_count', 'transferable', 'parent_experience_id',
                'updated_at')


def _page_params(params, statuses):
    status = params.get('status', 'all') or 'all'
    if status not in statuses:
        raise ValueError('invalid status')
    try:
        page = max(1, int(params.get('page', 1)))
        size = max(1, min(100, int(params.get('page_size', 20))))
    except (ValueError, TypeError):
        raise ValueError('invalid page') from None
    query = params.get('q', '').strip()
    project = params.get('project', '').strip()
    if len(query) > 500 or len(project) > 100:
        raise ValueError('filter too long')
    return status, page, size, query, project


class ContextInsights:
    def __init__(self, core):
        self.core = core
        self.store = core.store
        self.conn = self.store._connection()

    def _rows(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def _page(self, select, clause, args, page, size, order):
        total = self.conn.execute('SELECT count(*) ' + clause, args).fetchone()[0]
        rows = self._rows(select + ' ' + clause + f' ORDER BY {order} LIMIT ? OFFSET ?',
                          (*args, size, (page - 1) * size))
        return dict(rows=rows, total=total, page=page, page_size=size)

    def overview(self):
        counts = self.conn.execute(
            f"SELECT count(*) FILTER (WHERE {_VERIFIED}), "
            f"count(*) FILTER (WHERE i.status='candidate' AND {_CURRENT}), "
            f"count(*) FILTER (WHERE {_VERIFIED} AND i.success_count>=2) "
            f"FROM context_items i WHERE {_CASES}").fetchone()
        summaries = self.conn.execute(
            "SELECT count(*) FROM context_project_rollups r JOIN context_items i "
            "ON i.id=r.current_context_id WHERE r.status='ready' AND i.status='active'").fetchone()[0]
        unfinished = self.conn.execute(
            "SELECT count(*) FROM continuity_workstreams WHERE status IN ('open','paused','blocked')").fetchone()[0]
        projects = self._rows(
            "SELECT DISTINCT i.project,COALESCE(r.display_name,'') AS display_name "
            "FROM context_items i LEFT JOIN context_project_registry r ON r.project=i.project "
            "WHERE i.project != '' AND i.status != 'deleted' ORDER BY i.project")
        return dict(verified_experiences=counts[0], candidate_experiences=counts[1],
                    repeated_experiences=counts[2], ready_summaries=summaries,
                    unfinished_workstreams=unfinished, projects=projects, as_of=_now_iso())

    def experiences(self, params):
        status, page, size, query, project = _page_params(
            params, {'all', 'verified', 'candidate', 'inactive'})
        where, args = [_CASES], []
        if status == 'verified':
            where.append(_VERIFIED)
        elif status == 'candidate':
            where.append(f"i.status='candidate' AND {_CURRENT}")
        elif status == 'inactive':
            where.append(f"(i.status NOT IN ('active','candidate') OR NOT {_CURRENT})")
        if project:
            where.append('i.project=?')
            args.append(project)
        if query:
            where.append('instr(lower(i.experience_payload),lower(?))>0')
            args.append(query)
        result = self._page('SELECT i.id,i.content_type,NOT ' + _CURRENT + ' AS expired',
                            'FROM context_items i WHERE ' + ' AND '.join(where),
                            args, page, size, 'i.updated_at DESC,i.id DESC')
        for n, row in enumerate(result['rows']):
            case = self.core.experiences().describe(row['id'])
            result['rows'][n] = {**{k: case[k] for k in _CASE_FIELDS},
                                 'content_type': row['content_type'], 'expired': bool(row['expired'])}
        return result

    def _case_row(self, item_id):
        rows = self._rows(f'SELECT i.id,i.content_type,NOT {_CURRENT} AS expired '
                          f'FROM context_items i WHERE {_CASES} AND i.id=?', (item_id,))
        if not rows:
            raise LookupError('experience not found')
        return rows[0]

    def _case_link(self, item_id):
        rows = self._rows(f'SELECT i.id,i.project,json_extract(i.experience_payload,\'$.problem\') AS problem '
                          f'FROM context_items i WHERE {_CASES} AND i.id=?', (item_id,))
        return rows[0] if rows else None

    def experience(self, item_id):
        row = self._case_row(item_id)
        result = self.core.experiences().describe(item_id)
        result['content_type'] = row['content_type']
        result['expired'] = bool(row['expired'])
        result['parent_case'] = self._case_link(result['parent_experience_id'])
        result['derived_cases'] = self._rows(
            "SELECT i.id,i.project,json_extract(i.experience_payload,'$.problem') AS problem "
            f"FROM context_items i WHERE {_CASES} "
            "AND json_extract(i.experience_payload,'$.parent_experience_id')=? ORDER BY i.id", (item_id,))
        result['evidence'] = [dict(event, conditions=json.loads(event['conditions_json'] or '{}'))
                              for event in result['evidence']]
        return result

    def summaries(self, params):
        status, page, size, query, project = _page_params(
            params, {'all', 'ready', 'pending', 'failed', 'vector_dirty'})
        where, args = ["(i.id IS NULL OR i.status != 'deleted')"], []
        if status != 'all':
            where.append('r.status=?')
            args.append(status)
        if project:
            where.append('r.project=?')
            args.append(project)
        if query:
            where.append("(instr(lower(r.project),lower(?))>0 OR EXISTS "
                         "(SELECT 1 FROM context_layers s WHERE s.item_id=i.id "
                         "AND s.layer IN ('l0','l1') AND instr(lower(s.content),lower(?))>0))")
            args.extend((query, query))
        return self._page(
            "SELECT r.project,r.current_context_id AS id,r.status,r.updated_at,r.covered_through,"
            "i.updated_at AS content_updated_at,COALESCE(l.content,'尚未生成摘要') AS summary",
            "FROM context_project_rollups r LEFT JOIN context_items i ON i.id=r.current_context_id "
            "LEFT JOIN context_layers l ON l.item_id=i.id AND l.layer='l0' WHERE " + ' AND '.join(where),
            args, page, size, 'r.updated_at DESC,r.project')

    def _sources(self, item_id):
        sources = self.store.list_item_sources(item_id)
        for source in sources:
            if source['source_kind'] in {'context_reference', 'experience'} and source['source_ref'].isdigit():
                target_id = int(source['source_ref'])
                target = self.store.get_item(target_id, include_layers=False)
                if target is not None and target.status.value != 'deleted':
                    source['summary'] = self.store.get_layer(target_id, ContextLayer.L0) or ''
                    source['context_id'] = target_id
        return sources

    def summary(self, item_id):
        rows = self._rows(
            "SELECT r.project,r.status,r.updated_at,r.covered_through,i.id,i.updated_at AS content_updated_at "
            "FROM context_project_rollups r JOIN context_items i ON i.id=r.current_context_id "
            "WHERE i.id=? AND i.status != 'deleted'", (item_id,))
        if not rows:
            raise LookupError('summary not found')
        return dict(rows[0], l1=self.store.get_layer(item_id, ContextLayer.L1) or '',
                    l2=self.store.get_layer(item_id, ContextLayer.L2) or '', sources=self._sources(item_id))

    def workstreams(self, params):
        status, page, size, query, project = _page_params(
            params, {'all', 'unfinished', 'open', 'paused', 'blocked', 'completed', 'cancelled'})
        where, args = ['1=1'], []
        if status == 'unfinished':
            where.append("w.status IN ('open','paused','blocked')")
        elif status != 'all':
            where.append('w.status=?')
            args.append(status)
        if project:
            where.append('w.project=?')
            args.append(project)
        if query:
            where.append("(instr(lower(w.project),lower(?))>0 OR EXISTS "
                         "(SELECT 1 FROM context_layers s WHERE s.item_id=w.current_context_id "
                         "AND s.layer IN ('l0','l1') AND instr(lower(s.content),lower(?))>0))")
            args.extend((query, query))
        return self._page(
            "SELECT w.id,w.project,w.status,w.updated_at,w.checkpoint_revision,"
            "COALESCE(l.content,'断点内容暂不可用') AS summary,"
            "EXISTS (SELECT 1 FROM continuity_focus f WHERE f.workstream_id=w.id) AS is_focus",
            "FROM continuity_workstreams w LEFT JOIN context_layers l "
            "ON l.item_id=w.current_context_id AND l.layer='l0' WHERE " + ' AND '.join(where),
            args, page, size, 'w.updated_at DESC,w.id DESC')

    def workstream(self, workstream_id):
        rows = self._rows(
            "SELECT id,project,status,current_context_id,checkpoint_revision,state_version,"
            "updated_at,completed_at FROM continuity_workstreams WHERE id=?", (workstream_id,))
        if not rows:
            raise LookupError('workstream not found')
        row = rows[0]
        item_id = row.pop('current_context_id')
        raw = self.store.get_layer(item_id, ContextLayer.L2)
        try:
            payload = json.loads(raw or '{}')
        except ValueError:
            payload = {}
        fields = ('objective', 'accepted_decisions', 'completed_steps', 'current_step',
                  'next_action', 'blockers', 'parent_workstream_id', 'source_context_ids')
        return dict(row, **{k: payload.get(k) for k in fields},
                    content_available=bool(payload), sources=self._sources(item_id))
