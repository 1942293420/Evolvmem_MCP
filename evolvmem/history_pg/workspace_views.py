"""Read-only folder projection of history, independent of business project bindings."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

from evolvmem.history_pg.candidate_reader import (
    PgHistoryCandidateReader, _LEGACY_VISIBLE_BLOCK_SQL, _VISIBLE_BLOCK_SQL,
)
from evolvmem.history_pg.workspace_paths import normalize_workspace_path, workspace_key, workspace_name

BLOCK_INDEX_PREFIX = 'workspace_projection:v1:block:'
FOLDER_INDEX_PREFIX = 'workspace_projection:v1:folder:'
_KEY_RE = re.compile(r'workspace-[0-9a-f]{32}')


class WorkspaceHistoryReader(PgHistoryCandidateReader):
    """Use only verified native metadata; never infer a folder from a project label."""

    def _blocks(self, *, include_workspace_trash: bool = False) -> list[dict[str, Any]]:
        visibility = _LEGACY_VISIBLE_BLOCK_SQL if include_workspace_trash else _VISIBLE_BLOCK_SQL
        with self._cfg.connect(self._dbname, role_kind='reader') as conn:
            rows = conn.execute(
                'SELECT b.block_id,b.title,b.adapter,b.message_count,b.turn_count,'
                'b.observed_first_at,b.observed_last_at,b.attachment_recovered,m.value,f.value,'
                'b.source_archive_ids,b.block_sha256 '
                'FROM em_history.blocks b '
                'LEFT JOIN em_history.candidate_meta m ON m.key = %s || b.block_id '
                'LEFT JOIN em_history.candidate_meta f ON f.key = %s || b.block_id '
                f'WHERE {visibility} ORDER BY b.observed_last_at DESC,b.block_id',
                (BLOCK_INDEX_PREFIX, FOLDER_INDEX_PREFIX),
            ).fetchall()
        result = []
        for row in rows:
            try:
                metadata = json.loads(row[8] or '{}')
            except (ValueError, TypeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            path = normalize_workspace_path(metadata.get('workspace_path'))
            key = workspace_key(path)
            reason = str(metadata.get('reason') or 'unknown')
            current = metadata.get('source_archive_ids') == row[10] and metadata.get('block_sha256') == row[11]
            if (metadata.get('version') != 1 or reason != 'verified'
                    or not key or row[9] != key
                    or not current):
                path, key = '', ''
                if reason == 'verified':
                    reason = 'index_stale' if not current else 'invalid_workspace_index'
            result.append({
                'block_id': str(row[0]), 'title': row[1], 'adapter': row[2],
                'message_count': int(row[3] or 0), 'turn_count': int(row[4] or 0),
                'observed_first_at': row[5], 'observed_last_at': row[6],
                'attachment_count': int(row[7] or 0),
                'workspace_path': path, 'projects': [key] if key else [],
                'workspace_reason': 'verified' if key else reason,
            })
        return result

    @staticmethod
    def _group(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        projects: dict[str, dict[str, Any]] = {}
        for block in blocks:
            if not block['projects']:
                continue
            key = block['projects'][0]
            project = projects.setdefault(key, {
                'project': key, 'display_name': workspace_name(block['workspace_path']),
                'workspace_path': block['workspace_path'], 'status': 'active',
                'block_count': 0, 'message_count': 0, 'turn_count': 0, 'attachment_count': 0,
                'first_at': block['observed_first_at'], 'last_at': block['observed_last_at'],
                'summary': '按会话记录中的实际工作文件夹归组。',
            })
            project['block_count'] += 1
            for field in ('message_count', 'turn_count', 'attachment_count'):
                project[field] += block[field]
            first, last = block['observed_first_at'], block['observed_last_at']
            if first and (not project['first_at'] or first < project['first_at']):
                project['first_at'] = first
            if last and (not project['last_at'] or last > project['last_at']):
                project['last_at'] = last
        return sorted(projects.values(), key=lambda p: (str(p['last_at'] or ''), p['project']), reverse=True)

    def projects(self) -> dict[str, Any]:
        blocks = self._blocks()
        return {
            'ok': True, 'grouping': 'workspace', 'projects': self._group(blocks),
            'unassigned_count': sum(not b['projects'] for b in blocks),
        }

    def unassigned(self) -> dict[str, Any]:
        return {'ok': True, 'grouping': 'workspace', 'blocks': [b for b in self._blocks() if not b['projects']]}

    def project_overview(self, project: str) -> dict[str, Any]:
        key = unquote(project)
        blocks = [b for b in self._blocks() if b['projects'] == [key]]
        groups = self._group(blocks)
        if not groups:
            return {'ok': False, 'error': 'project_not_found'}
        return {'ok': True, 'grouping': 'workspace', **groups[0], 'blocks': blocks}

    def block_page(self, block_id: str) -> dict[str, Any]:
        result = super().block_page(block_id)
        if result.get('ok'):
            block = next((b for b in self._blocks() if b['block_id'] == unquote(block_id)), None)
            if block is None:
                return {'ok': False, 'error': 'block_not_found'}
            result['block'].update({field: block[field] for field in ('projects', 'workspace_path', 'workspace_reason')})
        return result

    def hybrid_search(self, query: str, *, query_vec=None, project=None, limit=20, scope=''):
        blocks = self._blocks()
        if scope == 'unassigned':
            blocks = [b for b in blocks if not b['projects']]
        elif scope not in ('', 'projects'):
            raise ValueError('invalid_workspace_search_scope')
        if project:
            blocks = [b for b in blocks if b['projects'] == [project]]
        by_id = {b['block_id']: b for b in blocks}
        hits = super().hybrid_search(query, query_vec=query_vec, limit=limit, block_ids=list(by_id))
        for hit in hits:
            block = by_id[hit['block_id']]
            hit.update({field: block[field] for field in ('projects', 'workspace_path', 'workspace_reason')})
        return hits

    def manage(self, action: str, *, kind='block', project='', key='', title='', request_id=''):
        if action.strip().lower() == 'trash':
            result = super().manage('trash')
            groups = {p['project']: p for p in self._group(self._blocks(include_workspace_trash=True))}
            for item in result['items']:
                if item['kind'] != 'workspace':
                    continue
                group = groups.get(item['key'])
                item['workspace_path'] = group['workspace_path'] if group else ''
                if group:
                    for field in ('block_count', 'message_count', 'turn_count', 'attachment_count', 'first_at', 'last_at'):
                        item[field] = group[field]
            return result
        if kind == 'workspace' and action.strip().lower() != 'trash':
            if not _KEY_RE.fullmatch(key) or project != key:
                raise ValueError('invalid_workspace_key')
            if action.strip().lower() == 'delete':
                blocks = self._blocks(include_workspace_trash=True)
                if not any(b['projects'] == [key] for b in blocks):
                    raise ValueError('workspace_not_found')
        return super().manage(action, kind=kind, project=project, key=key, title=title, request_id=request_id)
