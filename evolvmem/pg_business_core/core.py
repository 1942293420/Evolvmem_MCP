"""Concrete project-scoped PostgreSQL repositories for the current business slice.

Only trusted code receives this object. The connection is a LibrarySession; no
DSN, SQL string conversion, embedding call or vector-success assertion exists.
"""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json

from evolvmem.context_layers import validate_layers
from evolvmem.context_migration import LegacyMemoryMigrator
from evolvmem.context_models import (ContextContentType, ContextScope, ContextStatus,
    ContextTier, ContextLayer, ContextLayers, ContextItem, ContextReadRequest,
    ContextReadResult, SupersedeOutcome)
from evolvmem.legacy_models import (LegacyAddRequest, LegacyReplaceRequest,
    LegacyRemoveRequest, LegacyMutationResult)
from evolvmem.library_foundation.sessions import LibrarySession
from evolvmem.project_models import ProjectRegistrySnapshot, WorkspaceBindingSnapshot, ProjectResolutionDecision
from evolvmem.library_project_acl.scope import ProjectScope
from .domain import UnsupportedBusiness, projection_payload, resolved_draft

ALL_LAYERS = (ContextLayer.L0, ContextLayer.L1, ContextLayer.L2)
SUPPORTED_TYPES = frozenset(('decision','fact','constraint','preference','user_profile','reference','session_summary'))


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class KeyLease:
    """In-process guard minted only after the executor acquired the SQL lock."""
    def __init__(self, library_id, identity_key, project_id):
        self.library_id, self.identity_key, self.active = library_id, identity_key, True
        self.project_id = project_id

    def require(self, library_id, key, project_id):
        identity = LegacyMemoryMigrator.identity_key_for(key, 1)
        if not self.active or self.library_id != library_id or self.identity_key != identity or self.project_id != project_id:
            raise UnsupportedBusiness('key_lease_required')


class PgBusinessCore:
    def __init__(self, session, config, *, lease=None, model_contract, project_scope):
        if not isinstance(session, LibrarySession):
            raise TypeError('LibrarySession required')
        if not isinstance(model_contract, str) or not model_contract.strip():
            raise ValueError('model contract required for pending work')
        self.session, self.config, self.lease = session, config, lease
        self.library = session.route.library
        if not isinstance(project_scope,ProjectScope):
            raise TypeError('verified ProjectScope required')
        project_scope.require(session)
        self.project_scope=project_scope
        self.lib,self.project_id=project_scope.library_id,project_scope.project_id
        self.model_contract = model_contract

    def _write(self, key):
        if self.session.route.action not in ('write','admin') or self.lease is None:
            raise UnsupportedBusiness('key_lease_required')
        self.lease.require(self.lib, key, self.project_id)

    def _require_transaction(self, operation):
        if self.session._closed or self.lease is None or not self.lease.active:
            raise UnsupportedBusiness('key_lease_required')

    def _one(self, sql, params):
        rows = self.session.execute(sql, params)
        return rows[0][0] if rows else None

    def get_by_id(self, legacy_id):
        return self._one('SELECT to_jsonb(m) FROM em_content.memories m WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id=%s', (self.lib,self.project_id, legacy_id))

    def get_by_key(self, key):
        return [r[0] for r in self.session.execute("SELECT to_jsonb(m) FROM em_content.memories m WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND key=%s ORDER BY updated_at DESC,id", (self.lib,self.project_id,key))]

    def resolve_legacy_mapping(self, legacy_id):
        return self._one('SELECT context_item_id FROM em_content.legacy_memory_migrations WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND legacy_memory_id=%s', (self.lib,self.project_id,legacy_id))

    def record_legacy_mapping(self, legacy_id, item_id):
        self.session.execute('INSERT INTO em_content.legacy_memory_migrations(library_id,project_id,legacy_memory_id,context_item_id,migrated_at) VALUES(%s,%s,%s,%s,%s)', (self.lib,self.project_id,legacy_id,item_id,now()))

    def project_snapshot(self):
        return ProjectRegistrySnapshot(projects=(() if self.project_scope.project_key is None else (self.project_scope.project_key,)),aliases=(),bindings=(),
            generic_names=(),revision=self.project_scope.policy_epoch)

    def _project_guard(self,project):
        self.project_scope.require(self.session)
        if project!=self.project_scope.project_key:
            raise UnsupportedBusiness('project_scope_mismatch')

    def _draft(self,row,request):
        if request.workspace_path or (request.project_hint and request.project_hint!=self.project_scope.project_key):
            raise UnsupportedBusiness('project_scope_mismatch')
        # This is an explicit authorized project selection, not a heuristic
        # classification of historical data. The original row/content is intact.
        decision=ProjectResolutionDecision.resolved(self.project_scope.project_key,'strong','project-acl.v2',
            (dict(source='authorized_project_registry',type='project_scope',source_version='content-acl.v3',
                  normalized_value=self.project_scope.project_key),))
        draft=LegacyMemoryMigrator(None,self.config).draft_from_projection_row(row,confidence=request.confidence,decision=decision)
        draft=replace(draft,identity_key='project:'+self.project_id+':'+draft.identity_key)
        self._project_guard(draft.project)
        return draft,decision

    def _resolution(self, item_id, decision):
        state = decision.state.value
        self.session.execute('''INSERT INTO em_content.context_project_resolutions
            (library_id,project_id,item_id,resolution_state,review_state,proposed_project,
             resolved_project,confidence,method,evidence_json,resolver_version,created_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)''',
            (self.lib,self.project_id,item_id,state,'pending' if state in ('conflict','unresolved') else 'not_required',
             decision.proposed_project,decision.resolved_project,decision.confidence,decision.method,
             json.dumps(decision.evidence,ensure_ascii=False),decision.resolver_version,now(),now()))

    def _insert_projection(self, payload, supersedes=None):
        stamp = now()
        return self._one('''INSERT INTO em_content.memories(library_id,project_id,key,value,attribute,tags,
             source_session,importance,tier,expires_at,supersedes,created_at,updated_at)
             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
             (self.lib,self.project_id,payload['key'],payload['value'],payload['attribute'],payload['tags'],
              payload['source_session'],payload['importance'],payload['tier'],payload['expires_at'],supersedes,stamp,stamp))

    def _create_legacy_item(self, draft):
        self._require_transaction('create')
        validate_layers(draft.layers,self.config,allow_legacy_overflow=True)
        return self._create_item(draft)

    def _create_item(self, draft):
        if draft.content_type.value not in SUPPORTED_TYPES:
            raise UnsupportedBusiness('unsupported_content_type')
        self._project_guard(draft.project)
        stamp=now()
        item_id=self._one('''INSERT INTO em_content.context_items(library_id,project_id,identity_key,content_type,
            project,scope,status,tier,tags,importance,confidence,expires_at,supersedes,effective_from,
            effective_until,occurred_at,mentioned_at,created_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (self.lib,self.project_id,draft.identity_key,draft.content_type.value,draft.project,draft.scope.value,
             draft.status.value,draft.tier.value,json.dumps(draft.tags),draft.importance,draft.confidence,draft.expires_at,
             draft.supersedes,draft.effective_from,draft.effective_until,draft.occurred_at,draft.mentioned_at,stamp,stamp))
        for layer in ALL_LAYERS:
            content=getattr(draft.layers,layer.value)
            self.session.execute('''INSERT INTO em_content.context_layers(library_id,project_id,item_id,layer,content,content_hash,generator,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (self.lib,self.project_id,item_id,layer.value,content,digest(content),draft.layers.generator,stamp,stamp))
        return self.get_item(item_id)

    def get_item(self, item_id, *, include_layers=True):
        row=self._one('SELECT to_jsonb(c) FROM em_content.context_items c WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id=%s', (self.lib,self.project_id,item_id))
        if row is None:
            return None
        layers=None
        if include_layers:
            rows=self.session.execute('SELECT layer,content,generator FROM em_content.context_layers WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND item_id=%s', (self.lib,self.project_id,item_id))
            values={r[0]:r[1] for r in rows}
            if len(values)==3:
                layers=ContextLayers(values['l0'],values['l1'],values['l2'],rows[0][2])
        kwargs={k:row[k] for k in ('id','identity_key','project','importance','confidence','source_state','source_count',
            'success_count','failure_count','access_count','last_accessed','last_verified_at','expires_at','supersedes',
            'superseded_by','created_at','updated_at','personal_general','effective_from','effective_until','occurred_at','mentioned_at')}
        return ContextItem(**kwargs,content_type=ContextContentType(row['content_type']),scope=ContextScope(row['scope']),
            status=ContextStatus(row['status']),tier=ContextTier(row['tier']),tags=tuple(row['tags']),projects=tuple(row['projects']),layers=layers)

    def _supported_item(self,item_id):
        row=self._one('SELECT to_jsonb(c) FROM em_content.context_items c WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id=%s', (self.lib,self.project_id,item_id))
        if row is None:
            raise UnsupportedBusiness('mapped_context_missing')
        if (row['content_type'] not in SUPPORTED_TYPES or row['experience_payload'] is not None
                or row['source_state']!='none' or row['projects'] or row['personal_general']):
            raise UnsupportedBusiness('unsupported_context_evidence')
        unsupported=self._one("SELECT count(*) FROM em_content.context_sources WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND item_id=%s AND (archive_id IS NOT NULL OR source_kind<>'migration')", (self.lib,self.project_id,item_id))
        if unsupported:
            raise UnsupportedBusiness('unsupported_context_evidence')
        return row

    def _apply_legacy_item_metadata(self,item_id,*,access_count,last_accessed,created_at,updated_at,original_l2):
        self.session.execute('UPDATE em_content.context_items SET access_count=%s,last_accessed=%s,created_at=%s,updated_at=%s WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id=%s', (access_count,last_accessed,created_at,updated_at,self.lib,self.project_id,item_id))
        self.session.execute("UPDATE em_content.context_layers SET content=%s,content_hash=%s WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND item_id=%s AND layer='l2'", (original_l2,digest(original_l2),self.lib,self.project_id,item_id))
        self.session.execute('UPDATE em_content.context_layers SET created_at=%s,updated_at=%s WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND item_id=%s', (created_at,updated_at,self.lib,self.project_id,item_id))

    def record_migration_source(self,item_id,*,source_ref,extraction_version):
        source_id=self._one('''INSERT INTO em_content.context_sources(library_id,project_id,item_id,source_kind,source_ref,extraction_version,created_at)
            VALUES(%s,%s,%s,'migration',%s,%s,%s) RETURNING id''', (self.lib,self.project_id,item_id,source_ref,extraction_version,now()))
        self.session.execute('''UPDATE em_content.context_items SET source_count=(SELECT count(*) FROM em_content.context_sources WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND item_id=%s)
            WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id=%s''',(self.lib,self.project_id,item_id,self.lib,self.project_id,item_id))
        return source_id

    def set_supersession_links(self,item_id,*,supersedes,superseded_by):
        self.session.execute('UPDATE em_content.context_items SET supersedes=%s,superseded_by=%s WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id=%s', (supersedes,superseded_by,self.lib,self.project_id,item_id))

    def _lazy(self,row):
        # Rich history must be promoted with source evidence first. A live
        # project write cannot relabel an unmapped old row as its selected project.
        raise UnsupportedBusiness('historical_project_scope_not_promoted')

    def _fresh_legacy_row(self,legacy_id,row):
        if row is None:
            return None
        if (row.get('id')!=legacy_id or row.get('library_id')!=self.lib
                or 'project_id' not in row or row.get('project_id')!=self.project_id):
            raise UnsupportedBusiness('fresh_legacy_row_mismatch')
        return row

    def _pending(self,index_kind,entity_id,action,*,fresh_row=None):
        if index_kind=='legacy':
            # Only the two fresh legacy upserts pass the row they just read
            # for their draft. Old, absorbed, unknown and remove calls keep
            # the original read because those rows may have just changed.
            if fresh_row is not None and action=='upsert':
                row=self._fresh_legacy_row(entity_id,fresh_row)
            else:
                row=self.get_by_id(entity_id)
            content=row['value']
        elif index_kind=='context':
            rows=self.session.execute('''SELECT to_jsonb(c),l.content
                FROM em_content.context_items c
                LEFT JOIN em_content.context_layers l
                  ON l.library_id=c.library_id
                 AND l.project_id IS NOT DISTINCT FROM c.project_id
                 AND l.item_id=c.id
                 AND l.layer='l0'
                WHERE c.library_id=%s
                  AND c.project_id IS NOT DISTINCT FROM %s
                  AND c.id=%s''',(self.lib,self.project_id,entity_id))
            row=rows[0][0] if rows else None
            content=rows[0][1] if rows else None
        else:
            raise ValueError('unknown fixed index')
        # A user mutation appends exactly one immutable job inside the same
        # business transaction. Older pending rows belong to the existing
        # background decide/apply path; never lock, retire or rewrite them
        # here. A worker that commits after this transaction's snapshot but
        # before this statement must not turn the request into a conflict.
        self.session.execute('''INSERT INTO em_content.vector_outbox(library_id,project_id,index_kind,entity_id,action,entity_revision,content_hash,model_contract,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(library_id,index_kind,entity_id,entity_revision,action,model_contract) DO NOTHING''',
            (self.lib,self.project_id,index_kind,entity_id,action,row['revision'],digest(content or ''),self.model_contract,now()))

    def legacy_add(self,request,*,record_source=True):
        if not isinstance(request,LegacyAddRequest):raise TypeError('LegacyAddRequest required')
        self._write(request.key)
        legacy_id=self._insert_projection(projection_payload(request))
        fresh_row=self._fresh_legacy_row(legacy_id,self.get_by_id(legacy_id))
        draft,decision=self._draft(fresh_row,request)
        item=self._create_legacy_item(draft)
        self.record_legacy_mapping(legacy_id,item.id)
        if record_source and request.source_session.strip():
            self.record_migration_source(item.id,source_ref=request.source_session,extraction_version='legacy-write.v1')
        self._resolution(item.id,decision)
        self._pending('legacy',legacy_id,'upsert',fresh_row=fresh_row);self._pending('context',item.id,'upsert')
        return LegacyMutationResult(legacy_id,item.id,None,None,ALL_LAYERS,True)

    def supersede_item(self,predecessor_id,draft):
        validate_layers(draft.layers,self.config)
        self._supported_item(predecessor_id)
        self.session.execute("UPDATE em_content.context_items SET status='superseded',updated_at=%s,revision=revision+1 WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id=%s",(now(),self.lib,self.project_id,predecessor_id))
        absorbed=tuple(r[0] for r in self.session.execute("SELECT id FROM em_content.context_items WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND identity_key=%s AND project=%s AND scope=%s AND status='active' AND id<>%s ORDER BY id", (self.lib,self.project_id,draft.identity_key,draft.project,draft.scope.value,predecessor_id)))
        for item_id in absorbed:
            self._supported_item(item_id)
            self.session.execute("UPDATE em_content.context_items SET status='superseded',superseded_by=%s,updated_at=%s,revision=revision+1 WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id=%s",(predecessor_id,now(),self.lib,self.project_id,item_id))
        item=self._create_item(replace(draft,status=ContextStatus.ACTIVE,supersedes=predecessor_id))
        self.session.execute('UPDATE em_content.context_items SET superseded_by=%s,updated_at=%s WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id=%s',(item.id,now(),self.lib,self.project_id,predecessor_id))
        return SupersedeOutcome(item,absorbed)

    def legacy_replace(self,request):
        if not isinstance(request,LegacyReplaceRequest):raise TypeError('LegacyReplaceRequest required')
        self._write(request.key)
        old_rows=self.session.execute("SELECT to_jsonb(m),mm.context_item_id FROM em_content.memories m LEFT JOIN em_content.legacy_memory_migrations mm ON mm.library_id=m.library_id AND mm.project_id IS NOT DISTINCT FROM m.project_id AND mm.legacy_memory_id=m.id WHERE m.library_id=%s AND m.project_id IS NOT DISTINCT FROM %s AND m.key=%s AND m.status='active' ORDER BY m.id LIMIT 1", (self.lib,self.project_id,request.key))
        old_row=old_rows[0] if old_rows else None
        old=old_row[0] if old_row else None
        old_context_id=old_row[1] if old_row else None
        old_id=old['id'] if old else None
        new_id=self._insert_projection(projection_payload(request,old),supersedes=old_id)
        if old:
            self.session.execute("UPDATE em_content.memories SET status='superseded',superseded_by=%s,updated_at=%s,revision=revision+1 WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id=%s",(new_id,now(),self.lib,self.project_id,old_id))
        fresh_row=self._fresh_legacy_row(new_id,self.get_by_id(new_id))
        draft,decision=self._draft(fresh_row,request)
        absorbed=()
        if old is None:
            item=self._create_legacy_item(draft)
        else:
            if old_context_id is None:old_context_id=self._lazy(self.get_by_id(old_id))
            outcome=self.supersede_item(old_context_id,draft)
            item,absorbed=outcome.successor,outcome.absorbed
            for absorbed_id in absorbed:
                mirrored=self.session.execute("""UPDATE em_content.memories SET status='superseded',superseded_by=%s,updated_at=%s,revision=revision+1
                    WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id IN (SELECT legacy_memory_id FROM em_content.legacy_memory_migrations WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND context_item_id=%s) RETURNING id""",(old_id,now(),self.lib,self.project_id,self.lib,self.project_id,absorbed_id))
                for legacy_row in mirrored:self._pending('legacy',legacy_row[0],'upsert')
        self.record_legacy_mapping(new_id,item.id)
        if request.source_session.strip():self.record_migration_source(item.id,source_ref=request.source_session,extraction_version='legacy-write.v1')
        self._resolution(item.id,decision)
        # Superseded legacy vectors stay present. A latest-revision job is
        # needed even if the original pending upsert was never processed;
        # otherwise a future worker would discard its stale revision and leave
        # this non-deleted legacy row without any valid vector work.
        if old_id is not None:self._pending('legacy',old_id,'upsert')
        self._pending('legacy',new_id,'upsert',fresh_row=fresh_row);self._pending('context',item.id,'upsert')
        for removed in ((old_context_id,) if old_context_id is not None else ())+absorbed:self._pending('context',removed,'remove')
        return LegacyMutationResult(new_id,item.id,old_id,old_context_id,ALL_LAYERS,True)

    def legacy_remove(self,request):
        if not isinstance(request,LegacyRemoveRequest):raise TypeError('LegacyRemoveRequest required')
        row=self.get_by_id(request.legacy_id)
        if row is None:return LegacyMutationResult(request.legacy_id,None,None,None,(),False)
        self._write(row['key'])
        item_id=self.resolve_legacy_mapping(request.legacy_id)
        if item_id is None:item_id=self._lazy(row)
        self._supported_item(item_id)
        self.session.execute("UPDATE em_content.memories SET status='deleted',updated_at=%s,revision=revision+1 WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id=%s",(now(),self.lib,self.project_id,request.legacy_id))
        self.session.execute("UPDATE em_content.context_items SET status='deleted',updated_at=%s,revision=revision+1 WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id=%s",(now(),self.lib,self.project_id,item_id))
        self._pending('legacy',request.legacy_id,'remove');self._pending('context',item_id,'remove')
        return LegacyMutationResult(request.legacy_id,item_id,None,None,ALL_LAYERS,True)

    def read(self,request):
        if not isinstance(request,ContextReadRequest):raise TypeError('ContextReadRequest required')
        item=self.get_item(request.id,include_layers=False)
        code='not_found' if item is None else 'not_readable' if item.status is not ContextStatus.ACTIVE else 'expired' if item.expires_at is not None and item.expires_at<=now() else None
        if code:return ContextReadResult(request.id,request.layer,'',code)
        self._supported_item(request.id)
        content=self._one('SELECT content FROM em_content.context_layers WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND item_id=%s AND layer=%s',(self.lib,self.project_id,request.id,request.layer.value))
        return ContextReadResult(request.id,request.layer,content or '', 'invalid_layer' if content is None else None)
