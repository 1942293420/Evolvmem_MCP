"""Project knowledge operations shared by the Web workbench and AI CLI."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid

from evolvmem.context_layers import layers_from_legacy_value
from evolvmem.context_models import ContextContentType, ContextItemDraft, ContextLayer, ContextScope, ContextStatus
from evolvmem.context_store import _now_iso
from evolvmem.knowledge_rules import KnowledgeRules
from evolvmem.project_ownership import load_ownership, UNREVIEWED_FACT


class KnowledgeBase:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.rules = KnowledgeRules(service.config.data_dir)

    @property
    def conn(self):
        return self.store._connection()

    def registry(self):
        rows = [dict(r) for r in self.conn.execute("SELECT project,display_name,status,revision FROM context_project_registry ORDER BY project")]
        for row in rows:
            row['aliases'] = [r[0] for r in self.conn.execute('SELECT alias FROM context_project_aliases WHERE project=? ORDER BY alias', (row['project'],))]
        return rows

    def projects(self):
        counts = {}
        updated = {r['project']: r['updated_at'] for r in self.conn.execute('SELECT project,updated_at FROM context_project_registry')}
        for r in self.conn.execute("SELECT project,status,COUNT(*) n,MAX(updated_at) updated_at FROM context_items WHERE status NOT IN ('deleted','superseded') GROUP BY project,status"):
            counts.setdefault(r['project'], {})[r['status']] = r['n']
            updated[r['project']] = max(updated.get(r['project']) or '', r['updated_at'] or '')
        pending = {}
        rows = self.conn.execute("SELECT i.id,i.project,i.status FROM context_items i WHERE i.status IN ('active','candidate')").fetchall()
        facts = load_ownership(self.store, [r['id'] for r in rows])
        for r in rows:
            if r['status'] == 'candidate' or facts.get(r['id'], UNREVIEWED_FACT).excluded:
                pending[r['project']] = pending.get(r['project'], 0) + 1
        projects = self.registry()
        known = {r['project'] for r in projects}
        projects += [{'project': p, 'display_name': p, 'status': 'unregistered', 'aliases': [], 'revision': 0} for p in counts if p and p not in known]
        for row in projects:
            c = counts.get(row['project'], {})
            row.update(counts=c, total=sum(c.values()), active=c.get('active', 0), pending=pending.get(row['project'], 0), updated_at=updated.get(row['project'], ''))
        return {'projects': projects, 'unassigned': counts.get('', {}),
                'pending': sum(pending.values()), 'total': sum(sum(c.values()) for c in counts.values())}

    def save_project(self, body):
        project = str(body.get('project', '')).strip()
        with self.store.transaction():
            ps = self.service._project_store()
            ps.register_project(project)
            if 'display_name' in body:
                self.conn.execute('UPDATE context_project_registry SET display_name=?,revision=revision+1,updated_at=? WHERE project=?', (str(body['display_name']).strip()[:100], _now_iso(), project))
            if 'aliases' in body:
                aliases = body['aliases']
                if not isinstance(aliases, list) or any(not isinstance(a, str) or not a.strip() for a in aliases):
                    raise ValueError('invalid_aliases')
                self.conn.execute('DELETE FROM context_project_aliases WHERE project=?', (project,))
                for alias in aliases:
                    ps.add_alias(alias.strip(), project)
        return {'ok': True, 'project': project}

    def _row(self, item_id):
        row = self.conn.execute('SELECT * FROM context_items WHERE id=?', (int(item_id),)).fetchone()
        if row is None:
            raise ValueError('item_not_found')
        return dict(row)

    def detail(self, item_id):
        row = self._row(item_id)
        layers = {r['layer']: r['content'] for r in self.conn.execute('SELECT layer,content FROM context_layers WHERE item_id=?', (item_id,))}
        meta = self.conn.execute('SELECT * FROM knowledge_metadata WHERE item_id=?', (item_id,)).fetchone()
        resolution = self.conn.execute('SELECT * FROM context_project_resolutions WHERE item_id=?', (item_id,)).fetchone()
        facts = load_ownership(self.store, [item_id])
        body = layers.get('l2') or layers.get('l1') or layers.get('l0', '')
        managed = row['content_type'] in ('workstream_checkpoint', 'project_summary') or bool(row.get('experience_payload'))
        if managed:
            body = layers.get('l1') or layers.get('l0', '')
        title = meta['title'] if meta and meta['title'] else (layers.get('l0') or row['identity_key'])[:120]
        # Retrieval updates usage counters without editing knowledge. Such
        # reads must not invalidate a user's open editor.
        revision_row = {k: v for k, v in row.items() if k not in ('access_count', 'last_accessed')}
        learning = self.service.learning().metadata(item_id, row)
        revision_data = [revision_row, layers, dict(meta) if meta else None, dict(resolution) if resolution else None, learning]
        revision = hashlib.sha256(json.dumps(revision_data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        sources = [dict(r) for r in self.conn.execute(
            'SELECT s.source_kind,s.source_ref,s.archive_id,s.created_at,a.adapter,a.project AS source_project,a.state AS source_state '
            'FROM context_sources s LEFT JOIN session_archives a ON a.id=s.archive_id WHERE s.item_id=? ORDER BY s.id', (item_id,))]
        workstream = re.search(r':workstream:(ws_[a-z0-9]+):checkpoint$', row['identity_key'])
        return {**row, 'title': title, 'body': body, 'summary': layers.get('l0', ''), 'revision': revision, 'learning': learning,
                'tags': json.loads(row['tags'] or '[]'), 'ownership': facts.get(item_id, UNREVIEWED_FACT).public(),
                'resolution': dict(resolution) if resolution else None, 'sources': sources,
                'ingestion_reason': meta['ingestion_reason'] if meta else '',
                'rule_revision': meta['rule_revision'] if meta else '',
                'managed_content': managed, 'workstream_id': workstream.group(1) if workstream else '',
                'legacy_ids': list(self.store.legacy_ids_mapped_to_items([item_id]))}

    def list_items(self, params=None):
        p = params or {}
        where, args = ['1=1'], []
        for field in ('status', 'content_type'):
            if p.get(field) and p[field] != 'all':
                where.append(f'i.{field}=?'); args.append(p[field])
        if p.get('queue') and not p.get('status'):
            where.append("i.status IN ('active','candidate')")
        elif not p.get('status'):
            where.append("i.status NOT IN ('deleted','superseded')")
        if p.get('project') and p['project'] != '__all__':
            if p['project'] == '__global__':
                where.append("i.scope='global' AND i.project=''")
            elif p['project'] == '__none__':
                where.append("i.project='' AND i.scope!='global'")
            else:
                where.append('i.project=?'); args.append(p['project'])
        if p.get('q'):
            where.append('(i.identity_key LIKE ? OR EXISTS(SELECT 1 FROM context_layers l WHERE l.item_id=i.id AND l.content LIKE ?) OR EXISTS(SELECT 1 FROM knowledge_metadata k WHERE k.item_id=i.id AND k.title LIKE ?))')
            args.extend(['%' + str(p['q']) + '%'] * 3)
        rows = self.conn.execute('SELECT i.id,i.status FROM context_items i WHERE ' + ' AND '.join(where) + ' ORDER BY i.updated_at DESC,i.id DESC', args).fetchall()
        if p.get('category'):
            rows = [r for r in rows if self.service.learning().metadata(r['id'])['category'] == p['category']]
        if p.get('queue'):
            facts = load_ownership(self.store, [r['id'] for r in rows])
            queue = p['queue']
            rows = [r for r in rows if (r['status'] == 'candidate' if queue == 'candidate' else facts.get(r['id'], UNREVIEWED_FACT).excluded if queue == 'ownership' else r['status'] == 'candidate' or facts.get(r['id'], UNREVIEWED_FACT).excluded)]
        page, size = max(1, int(p.get('page', 1))), min(100, max(1, int(p.get('page_size', 30))))
        items = []
        for r in rows[(page-1)*size:page*size]:
            item = self.detail(r['id'])
            item['body'] = item['body'][:350]
            item.pop('experience_payload', None)
            items.append(item)
        return {'items': items, 'total': len(rows), 'page': page, 'page_size': size}

    def _check(self, item_id, body):
        row = self.detail(item_id)
        if body.get('expected_revision') != row['revision']:
            raise ValueError('revision_conflict')
        return row

    def _stamp(self, item_id, *, title=None, reason=None, rule_revision=None):
        self.conn.execute('INSERT INTO knowledge_metadata(item_id) VALUES (?) ON CONFLICT(item_id) DO UPDATE SET revision=revision+1', (item_id,))
        for field, value in (('title', title), ('ingestion_reason', reason), ('rule_revision', rule_revision)):
            if value is not None:
                self.conn.execute(f'UPDATE knowledge_metadata SET {field}=? WHERE item_id=?', (value, item_id))

    def _sync(self, item_ids):
        item_ids = set(item_ids)
        from evolvmem.project_memory import refresh
        projects = {self._row(cid)['project'] for cid in item_ids}
        # A project move must refresh the old document as well as the new one.
        for cached in self.conn.execute('SELECT project,source_ids FROM project_memory_documents'):
            if item_ids.intersection(json.loads(cached['source_ids'])):
                projects.add(cached['project'])
        for project in projects:
            if project and any(p['project']==project and p['status']=='active' for p in self.registry()):
                refresh(self.service, project)
        from evolvmem.context_service import _VectorAftermath
        active, removed, oldactive, oldremoved = [], [], [], []
        for cid in set(item_ids):
            row = self.detail(cid)
            if row['status'] == 'active':
                active.append((cid, row['summary']))
            else:
                removed.append(cid)
            # Legacy index retains all non-deleted rows; its read path filters
            # status. Context vectors contain only current active knowledge.
            if row['status'] != 'deleted':
                oldactive.extend((lid, row['body']) for lid in row['legacy_ids'])
            else:
                oldremoved.extend(row['legacy_ids'])
        self.service._apply_vector_aftermath(_VectorAftermath(
            context_upserts=tuple(active), context_removals=tuple(removed),
            legacy_upserts=tuple(oldactive), legacy_removals=tuple(oldremoved)))

    def _project(self, project):
        if project and not self.conn.execute("SELECT 1 FROM context_project_registry WHERE project=? AND status='active'", (project,)).fetchone():
            raise ValueError('project_not_found')

    def _layers(self, title, text, kind):
        from dataclasses import replace
        layers = layers_from_legacy_value(text, content_type=kind, config=self.service.config)
        return replace(layers,
            l0=(title + ' · ' + layers.l0)[:self.service.config.context_l0_max_chars],
            l1=(title + '\n' + layers.l1)[:self.service.config.context_l1_max_chars],
            generator='knowledge-edit.v1')

    def create(self, body):
        title, text = str(body.get('title', '')).strip(), str(body.get('body', '')).strip()
        if not title or not text or len(title) > 200 or len(text) > 100000:
            raise ValueError('invalid_content')
        if body.get('scope') == 'global' and body.get('project'):
            raise ValueError('global_project_conflict')
        project = str(body.get('project', ''))
        self._project(project)
        kind = ContextContentType(body.get('content_type', 'reference'))
        if kind in (ContextContentType.WORKSTREAM_CHECKPOINT, ContextContentType.PROJECT_SUMMARY):
            raise ValueError('managed_content_type')
        status = ContextStatus.ACTIVE if body.get('action') == 'publish' else ContextStatus.CANDIDATE
        if status == ContextStatus.ACTIVE and not project and body.get('scope') != 'global':
            raise ValueError('project_required')
        scope = ContextScope.GLOBAL if body.get('scope') == 'global' else ContextScope.PROJECT
        layers = self._layers(title, text, kind)
        with self.service._cutover_lock.shared(), self.store.transaction():
            item = self.store._create_legacy_item(ContextItemDraft(identity_key=f'knowledge:{uuid.uuid4().hex}', content_type=kind,
                layers=layers, project=project, scope=scope, status=status, confidence=float(body.get('confidence', .9)), tags=tuple(body.get('tags', []))))
            self._stamp(item.id, title=title, reason='用户录入')
            if project:
                self.service._project_store().review_item_project(item.id, project, expected_revision=0)
            elif scope == ContextScope.GLOBAL:
                from evolvmem.project_models import ProjectResolutionDecision
                self.service._project_store().record_resolution(item.id, ProjectResolutionDecision.global_decision('knowledge.v1'))
            self.conn.execute("INSERT INTO context_sources(item_id,source_kind,source_ref,extraction_version,created_at) VALUES(?,'manual',?,'knowledge.v1',?)", (item.id, str(body.get('source') or '用户录入'), _now_iso()))
            self.store.recompute_source_states([item.id])
            if body.get('action') == 'auto':
                row = self.detail(item.id)
                self._apply_decision(row, self.preview({**row, 'source': True}))
        self._sync([item.id])
        return self.detail(item.id)

    def update(self, item_id, body):
        with self.service._cutover_lock.shared(), self.store.transaction():
            row = self._check(item_id, body)
            title = str(body.get('title', row['title'])).strip()
            text = str(body.get('body', row['body'])).strip()
            if not title or not text or len(title) > 200 or len(text) > 100000:
                raise ValueError('invalid_content')
            if row['managed_content'] and text != row['body']:
                raise ValueError('managed_content_create_correction')
            now = _now_iso()
            if not row['managed_content'] and (text != row['body'] or title != row['title']):
                layers = self._layers(title, text, ContextContentType(row['content_type']))
                for layer in ('l0', 'l1', 'l2'):
                    value = getattr(layers, layer)
                    self.conn.execute('UPDATE context_layers SET content=?,content_hash=?,generator=?,updated_at=? WHERE item_id=? AND layer=?',
                        (value, hashlib.sha256(value.encode()).hexdigest(), 'knowledge-edit.v1', now, item_id, layer))
                for lid in row['legacy_ids']:
                    self.conn.execute('UPDATE memories SET value=?,updated_at=? WHERE id=?', (text, now, lid))
            tags = body.get('tags', row['tags'])
            if not isinstance(tags, list) or any(not isinstance(x, str) for x in tags):
                raise ValueError('invalid_tags')
            self.conn.execute('UPDATE context_items SET tags=?,updated_at=? WHERE id=?', (json.dumps(tags, ensure_ascii=False), now, item_id))
            for lid in row['legacy_ids']:
                self.conn.execute('UPDATE memories SET tags=?,updated_at=? WHERE id=?', (','.join(tags), now, lid))
            self._stamp(item_id, title=title)
        self._sync([item_id])
        return self.detail(item_id)

    def assign(self, item_id, body):
        project = str(body.get('project', ''))
        self._project(project)
        touched = [item_id]
        with self.service._cutover_lock.shared(), self.store.transaction():
            row = self._check(item_id, body)
            if row.get('experience_payload') and project != row['project']:
                if not project:
                    raise ValueError('project_required')
                case = json.loads(row['experience_payload'])
                case['project'] = project
                _, key, layers = self.service.experiences().prepare_case(case)
                self.conn.execute('UPDATE context_items SET identity_key=?,experience_payload=? WHERE id=?', (key, layers.l2, item_id))
                for layer in ('l0', 'l1', 'l2'):
                    value = getattr(layers, layer)
                    self.conn.execute('UPDATE context_layers SET content=?,content_hash=?,updated_at=? WHERE item_id=? AND layer=?', (value, hashlib.sha256(value.encode()).hexdigest(), _now_iso(), item_id, layer))
            if row['workstream_id'] and project != row['project']:
                if not body.get('move_workstream'):
                    raise ValueError('workstream_move_confirmation_required')
                touched = self._move_workstream(row, project)
            elif project:
                revision = row['resolution']['revision'] if row['resolution'] else 0
                self.service._project_store().review_item_project(item_id, project, expected_revision=revision)
            else:
                from evolvmem.project_models import ProjectResolutionDecision
                self.conn.execute("UPDATE context_items SET project='',scope='global',updated_at=? WHERE id=?", (_now_iso(), item_id))
                self.service._project_store().record_resolution(item_id, ProjectResolutionDecision.global_decision('knowledge-review.v1'))
                self.conn.execute("UPDATE context_project_resolutions SET decision_source='human',review_state='accepted',reviewed_at=? WHERE item_id=?", (_now_iso(), item_id))
            if project:
                self.conn.execute("UPDATE context_items SET scope='project' WHERE id=?", (item_id,))
            self._stamp(item_id, reason='用户确认归属')
        self._sync(touched)
        return self.detail(item_id)

    def _move_workstream(self, row, project):
        if not project:
            raise ValueError('project_required')
        ws = row['workstream_id']
        task = self.conn.execute('SELECT * FROM continuity_workstreams WHERE id=?', (ws,)).fetchone()
        if task is None:
            raise ValueError('workstream_not_found')
        if task['parent_id'] or self.conn.execute('SELECT 1 FROM continuity_workstreams WHERE parent_id=?', (ws,)).fetchone():
            raise ValueError('workstream_has_related_tasks')
        ids = [r[0] for r in self.conn.execute('SELECT id FROM context_items WHERE identity_key=?', (row['identity_key'],))]
        key = f'project:{project}:workstream:{ws}:checkpoint'
        self.conn.execute('UPDATE continuity_workstreams SET project=?,state_version=state_version+1,checkpoint_revision=checkpoint_revision+1,updated_at=? WHERE id=?', (project, _now_iso(), ws))
        focused = self.conn.execute('SELECT 1 FROM continuity_focus WHERE workstream_id=?', (ws,)).fetchone()
        self.conn.execute('UPDATE continuity_focus SET workstream_id=NULL,revision=revision+1,updated_at=? WHERE workstream_id=?', (_now_iso(), ws))
        binding = self.conn.execute('SELECT 1 FROM context_project_workspace_bindings WHERE workspace_fingerprint=? AND project=? AND state=\'active\'', (task['workspace_fingerprint'], project)).fetchone()
        if not binding:
            self.service._project_store().bind_workspace(task['workspace_fingerprint'], project, method='human_task_move', make_default=False)
        if focused:
            self.conn.execute('UPDATE continuity_focus SET workstream_id=?,revision=revision+1,updated_at=? WHERE project=? AND workspace_fingerprint=? AND workstream_id IS NULL', (ws, _now_iso(), project, task['workspace_fingerprint']))
        for cid in ids:
            item = self.detail(cid)
            self.conn.execute('UPDATE context_items SET project=?,identity_key=?,updated_at=? WHERE id=?', (project, key, _now_iso(), cid))
            raw = self.store.get_layer(cid, ContextLayer.L2)
            try:
                payload = json.loads(raw)
                payload['project'] = project
                if cid == task['current_context_id']:
                    payload['checkpoint_revision'] = task['checkpoint_revision'] + 1
                    payload['state_version'] = task['state_version'] + 1
                value = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                self.conn.execute("UPDATE context_layers SET content=?,content_hash=?,updated_at=? WHERE item_id=? AND layer='l2'", (value, hashlib.sha256(value.encode()).hexdigest(), _now_iso(), cid))
            except (ValueError, TypeError):
                raise ValueError('invalid_workstream_payload')
            self.service._project_store().record_trusted_resolution(cid, project, method='human_workstream_move')
            self.conn.execute("UPDATE context_project_resolutions SET decision_source='human',review_state='accepted',reviewed_at=? WHERE item_id=?", (_now_iso(), cid))
            self._stamp(cid, reason='用户整组迁移任务归属')
        return ids

    def transition(self, item_id, body, *, refresh_qa=True):
        actions = {'publish': 'active', 'archive': 'archived', 'restore': 'active', 'reject': 'archived', 'delete': 'deleted', 'review': 'candidate'}
        if body.get('action') not in actions:
            raise ValueError('invalid_action')
        target = actions[body['action']]
        with self.service._cutover_lock.shared(), self.store.transaction():
            row = self._check(item_id, body)
            if row['workstream_id']:
                raise ValueError('workstream_lifecycle_managed_by_task')
            if target == 'active' and row['scope'] != 'global' and row['ownership']['state'] != 'confirmed':
                raise ValueError('confirm_project_first')
            qa = self.conn.execute('SELECT * FROM knowledge_qa WHERE item_id=?', (item_id,)).fetchone() if refresh_qa and target == 'active' else None
            if qa and qa['question']:
                from evolvmem import qa_memory
                if qa['source_fingerprint'] != qa_memory.fingerprint(row):
                    raise ValueError('qa_source_changed')
                if qa_memory.conflicts(self.service, row['project'], row['scope'], qa['question'], qa['answer'], row['learning']['category'], row['learning'].get('trigger',''), exclude=(item_id,)):
                    raise ValueError('qa_conflict_requires_confirmation')
            self.store.set_item_status(item_id, ContextStatus(target))
            for lid in row['legacy_ids']:
                self.conn.execute('UPDATE memories SET status=?,updated_at=? WHERE id=?', (target, _now_iso(), lid))
            self._stamp(item_id, reason={'publish': '用户确认入库', 'reject': '用户暂不入库'}.get(body['action'], '用户调整资料状态'))
            if qa and qa['question']:
                qa_memory.record(self.service, item_id, {'question':qa['question'],'answer':qa['answer']}, origin='manual', approved=True)
        self._sync([item_id])
        return self.detail(item_id)

    def preview(self, body):
        policy = self.rules.prepare(body['rules']) if body.get('rules') else None
        return self.rules.evaluate(body, [r for r in self.registry() if r['status'] == 'active'], policy=policy)

    def apply_ingestion(self, item_id, *, source=None):
        """Apply the current policy inside the caller's atomic write transaction."""
        row = self.detail(item_id)
        if row['resolution'] and row['resolution']['decision_source'] == 'human':
            return row['status']
        decision = self.preview({**row, 'key': row['identity_key'], 'source': source or bool(row['sources'])})
        if row.get('experience_payload'):
            # Cases carry immutable evidence semantics. Never infer a new case
            # project from its free text; explicit reassignment handles both.
            self._stamp(item_id, reason=decision['reason'], rule_revision=decision['rule_revision'])
            return row['status']
        self._apply_decision(row, decision)
        return {'auto': 'active', 'review': 'candidate', 'ignore': 'archived'}[decision['action']]

    def _apply_decision(self, row, decision):
        from evolvmem.project_models import ProjectResolutionDecision
        cid, project = row['id'], decision['project']
        ps = self.service._project_store()
        if decision['scope'] == 'global':
            resolution = ProjectResolutionDecision.global_decision('knowledge-rules.v1')
        elif project:
            resolution = ProjectResolutionDecision.resolved(project, 'knowledge_rules', 'knowledge-rules.v1', ())
        else:
            resolution = ProjectResolutionDecision.unresolved('knowledge-rules.v1', ())
        ps.record_resolution(cid, resolution)
        status = {'auto': 'active', 'review': 'candidate', 'ignore': 'archived'}[decision['action']]
        self.conn.execute('UPDATE context_items SET project=?,scope=?,status=?,updated_at=? WHERE id=?', (project, decision['scope'], status, _now_iso(), cid))
        for lid in row['legacy_ids']:
            self.conn.execute('UPDATE memories SET status=?,updated_at=? WHERE id=?', (status, _now_iso(), lid))
        self._stamp(cid, reason=decision['reason'], rule_revision=decision['rule_revision'])

    def organize(self, body):
        ids = body.get('ids', [])
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(type(i) is not int or i < 1 for i in ids):
            raise ValueError('invalid_items')
        proposals, applied, ignored = [], 0, 0
        for cid in dict.fromkeys(ids):
            row = self.detail(cid)
            if row['managed_content'] or row['status'] not in ('active', 'candidate'):
                decision = {'action': 'review', 'project': row['project'], 'reason': '系统生成资料，需核对关联任务或来源', 'rule_revision': self.rules.read()['revision']}
            elif row['resolution'] and row['resolution']['decision_source'] == 'human':
                decision = {'action': 'review', 'project': row['project'], 'reason': '保留人工决定，可手动确认入库', 'rule_revision': self.rules.read()['revision']}
            else:
                sample = {**row, 'key': row['identity_key'], 'source': bool(row['sources'])}
                if row['ownership']['state'] == 'excluded':
                    # An old project column/key is the claim under review,
                    # not independent evidence that can confirm itself.
                    sample['project'] = ''
                    sample['key'] = ''
                decision = self.preview(sample)
            if decision['action'] == 'auto' and self.conn.execute(
                "SELECT 1 FROM context_items WHERE identity_key=? AND project=? AND scope=? AND status='active' AND id!=?",
                (row['identity_key'], decision['project'], decision['scope'], cid),
            ).fetchone():
                decision = {**decision, 'action': 'review', 'reason': '目标项目已存在相同标识的资料，请先核对或归档重复资料'}
            proposal = {'id': cid, 'title': row['title'], 'expected_revision': row['revision'], **decision, 'applied': False}
            if body.get('apply') and decision['action'] in ('auto', 'ignore'):
                # Each item is an atomic unit; revisions protect changes between
                # suggestion generation and acceptance. No guessed task moves.
                try:
                    with self.service._cutover_lock.shared(), self.store.transaction():
                        self._check(cid, {'expected_revision': row['revision']})
                        self._apply_decision(row, decision)
                except (ValueError, sqlite3.IntegrityError):
                    proposal.update(action='review', reason='资料已变化或存在重复标识，请重新核对后处理')
                    proposals.append(proposal)
                    continue
                self._sync([cid])
                applied += decision['action'] == 'auto'
                ignored += decision['action'] == 'ignore'
                proposal['applied'] = True
            proposals.append(proposal)
        return {'items': proposals, 'applied': applied, 'ignored': ignored, 'pending': sum(p['action'] == 'review' for p in proposals)}

    def batch(self, body):
        items = body.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError('invalid_items')
        results = []
        from evolvmem.project_store import ProjectStoreError
        for entry in items:
            if not isinstance(entry, dict) or type(entry.get('id')) is not int:
                raise ValueError('invalid_items')
            try:
                params = {**body, **entry}
                method = self.assign if body.get('action') == 'assign' else self.transition
                row = method(entry['id'], params)
                results.append({'id': row['id'], 'ok': True})
            except (ValueError, ProjectStoreError, sqlite3.IntegrityError) as error:
                results.append({'id': entry['id'], 'ok': False, 'error': str(error) if not isinstance(error, sqlite3.IntegrityError) else 'identity_conflict'})
        return {'items': results, 'succeeded': sum(r['ok'] for r in results), 'failed': sum(not r['ok'] for r in results)}
