"""Evidence-backed cases shared by adapters; no extra model call for retrieval.

Case content is immutable: changed conditions/steps create a derived case.
Feedback events are revisioned, deduplicated and counted by independent task.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone

from evolvmem.context_models import (
    ContextContentType, ContextItemDraft, ContextLayers, ContextScope,
    ContextStatus, ContextSearchRequest, ContextLayer,
    ContextMatchType,
)
from evolvmem.experience_sources import ExperienceSourceResolver
from evolvmem.project_store import ProjectStore
from evolvmem.extraction_policy import contains_sensitive_text
from evolvmem.memory_eligibility import eligible as memory_eligible

OUTCOMES = {'success', 'failure', 'confirmed', 'contradicted', 'inapplicable', 'unknown', 'used'}
LEVELS = {'technical', 'business', 'user_confirmed'}

# A lone exact term in the case's problem statement is useful for terse CJK
# paraphrases. These generic terms cannot establish lexical support by
# themselves, though they still contribute to relevance after support exists.
from evolvmem.experience_ranking import (
    terms as _terms, task_verdicts, successful_verifications, describe_case,
    compact_verification, rank_cases,
)

def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _text(value, name, maximum=2000, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f'invalid {name}')
    return value.strip()


def _conditions(value):
    if not isinstance(value, dict) or len(value) > 20:
        raise ValueError('invalid conditions')
    return {_text(k, 'condition name', 80): _text(v, 'condition value', 250)
            for k, v in value.items()}


def _list(value, name):
    if not isinstance(value, list) or len(value) > 15:
        raise ValueError(f'invalid {name}')
    return [_text(v, name, 500) for v in value]




def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


class ExperienceService:
    def __init__(self, core, *, source_resolver=None):
        self.core = core
        self.store = core.store
        self.config = core.config
        from evolvmem.session_archive import SessionArchiver
        self.source_resolver = source_resolver or ExperienceSourceResolver(archiver=SessionArchiver(core.config, core.store))
        self._cache = {}

    def _conn(self):
        return self.store._connection()

    def _payload(self, item_id):
        if type(item_id) is not int or item_id <= 0:
            raise ValueError('invalid experience id')
        row = self._conn().execute('SELECT experience_payload FROM context_items WHERE id=?',
                                   (item_id,)).fetchone()
        if row is None or not row[0]:
            raise ValueError('experience not found')
        return json.loads(row[0])

    def prepare_case(self, case):
        """Validate one immutable case without writing it or touching vectors."""
        if not isinstance(case, dict):
            raise ValueError('invalid case')
        allowed = {'project','problem','conditions','steps','rationale','result','applicability',
                   'exclusions','transferable','parent_experience_id'}
        if set(case) - allowed:
            raise ValueError('unknown case fields')
        project = self.core._normalize_project(_text(case.get('project'), 'project', 100))
        if not project or project in self.core._generic_workspace_names():
            raise ValueError('explicit project required')
        payload = dict(project=project,
                       problem=_text(case.get('problem'), 'problem', 400),
                       conditions=_conditions(case.get('conditions', {})),
                       steps=_list(case.get('steps'), 'steps'),
                       rationale=_text(case.get('rationale', ''), 'rationale', required=False),
                       result=_text(case.get('result', ''), 'result', required=False),
                       applicability=_list(case.get('applicability', []), 'applicability'),
                       exclusions=_list(case.get('exclusions', []), 'exclusions'),
                       transferable=case.get('transferable', False),
                       parent_experience_id=case.get('parent_experience_id'))
        if not payload['steps'] or type(payload['transferable']) is not bool:
            raise ValueError('steps and transferable are invalid')
        if any(contains_sensitive_text(value) for value in _strings(payload)):
            raise ValueError('sensitive case content is not allowed')
        parent = payload['parent_experience_id']
        if parent is not None:
            self._payload(parent)
        full = _json(payload)
        if len(full) > self.config.context_l2_max_chars:
            raise ValueError('experience exceeds L2 budget')
        digest = hashlib.sha256(full.encode()).hexdigest()[:24]
        key = f'project:{project}:experience:{digest}'
        l0 = (payload['problem'] + '；条件：' + '，'.join(f'{k}={v}' for k,v in payload['conditions'].items()))[:self.config.context_l0_max_chars]
        l1 = '\n'.join([payload['problem'], '条件：'+_json(payload['conditions']),
                        '步骤：'+'；'.join(payload['steps']), '依据：'+payload['rationale'],
                        '结果：'+payload['result'], '不适用：'+'；'.join(payload['exclusions'])])[:self.config.context_l1_max_chars]
        return payload, key, ContextLayers(l0, l1, full, 'experience-v1')

    def record(self, case, *, evidence=None):
        self.core._require_serving()
        payload, key, layers = self.prepare_case(case)
        if evidence is not None:
            evidence = self._validate_evidence(evidence)
        with self.core._cutover_lock.shared(), self.store.transaction():
            row = self._conn().execute("SELECT id FROM context_items WHERE identity_key=? AND status != 'deleted' ORDER BY id DESC LIMIT 1", (key,)).fetchone()
            if row:
                item_id = row[0]
            else:
                item = self.store.create_item(ContextItemDraft(
                    identity_key=key, content_type=ContextContentType.EXPERIENCE,
                    layers=layers,
                    project=payload['project'], scope=ContextScope.PROJECT,
                    status=ContextStatus.CANDIDATE, confidence=.7,
                    tags=('经验案例',), importance=6))
                item_id = item.id
                self._conn().execute('UPDATE context_items SET experience_payload=? WHERE id=?',
                                     (layers.l2,item_id))
                # Explicit provenance: the experience payload carries its own
                # typed project; recorded so the confirmed case is not held as
                # uncertain ownership on the default injection surface.
                ProjectStore(self._conn(), self.store._require_transaction,
                             generic_names=()).record_trusted_resolution(
                    item_id, payload['project'])
            if evidence is not None:
                self._write_outcome(item_id, evidence)
        self._sync(item_id)
        return self.read(item_id)

    def _validate_evidence(self, evidence):
        if not isinstance(evidence, dict):
            raise ValueError('invalid evidence')
        permitted = {'task_id','event_id','outcome','level','source_kind','source_ref',
                     'quote','note','conditions','revision','experience_version','source_id'}
        if set(evidence) - permitted:
            raise ValueError('unknown evidence fields')
        result = dict(evidence)
        task_id = evidence.get('task_id')
        if task_id in (None, '', 'current'):
            task_id = 'current'
            if not evidence.get('source_ref') and evidence.get('source_id') is None:
                task_id = os.environ.get('CODEX_THREAD_ID') or os.environ.get(
                    'CODEX_SESSION_ID') or 'current'
        result['task_id'] = _text(task_id, 'task_id', 500)
        result['event_id'] = _text(evidence.get('event_id'), 'event_id', 500)
        result['source_ref'] = _text(
            evidence.get('source_ref', ''), 'source_ref', 1000, required=False)
        result['quote'] = _text(
            evidence.get('quote', ''), 'quote', 1000, required=False)
        if evidence.get('outcome') not in OUTCOMES:
            raise ValueError('invalid outcome')
        source_id = evidence.get('source_id')
        if source_id is not None and (type(source_id) is not int or source_id <= 0):
            raise ValueError('invalid source_id')
        result['source_id'] = source_id
        result['source_kind'] = _text(
            evidence.get('source_kind', ''), 'source_kind', 40, required=False)
        if source_id is None and result['source_kind'] not in {
            'tool_result','user_confirmation','historical_record'}:
            raise ValueError('verifiable source required')
        if source_id is None and not result['quote']:
            raise ValueError('evidence quote required')
        result['note'] = _text(evidence.get('note'), 'verification note', 2500)
        if any(contains_sensitive_text(value) for value in (
                result['note'], result['quote'], *_strings(
                    evidence.get('conditions', {})))):
            raise ValueError('sensitive evidence content is not allowed')
        if evidence['outcome'] in {'success','confirmed'} and evidence.get('level') not in LEVELS:
            raise ValueError('verification level required')
        result['conditions'] = _conditions(evidence.get('conditions', {}))
        for name in ('revision','experience_version'):
            result[name] = evidence.get(name, 1)
            if type(result[name]) is not int or result[name] < 1:
                raise ValueError(f'invalid {name}')
        if result['experience_version'] != 1:
            raise ValueError('experience content is immutable; create a derived case')
        return result

    def outcome(self, item_id, evidence):
        self.core._require_serving()
        self._payload(item_id)
        evidence = self._validate_evidence(evidence)
        with self.core._cutover_lock.shared(), self.store.transaction():
            self._write_outcome(item_id, evidence)
        self._sync(item_id)
        return self.read(item_id)

    def _write_outcome(self, item_id, evidence):
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        case_conditions = self._payload(item_id)['conditions']
        self._validate_evidence_scope(case_conditions, evidence)
        source_id, source_note = self._resolve_source(item_id, evidence, now)
        event_key = _json([evidence['task_id'], evidence['event_id'], evidence['experience_version']])
        old = self._conn().execute('SELECT * FROM context_evidence WHERE item_id=? AND event_key=? ORDER BY revision DESC LIMIT 1', (item_id,event_key)).fetchone()
        snapshot = (evidence['outcome'], source_note, _json(evidence['conditions']),
                    source_id, evidence.get('level',''), 1)
        if old:
            previous = (old['outcome'], old['note'], old['conditions_json'],
                        old['source_id'], old['verification_level'],
                        old['experience_version'])
            if evidence['revision'] == old['revision']:
                if snapshot != previous:
                    raise ValueError('verification changed: supply a higher revision')
                return
            if evidence['revision'] != old['revision'] + 1:
                raise ValueError('verification revision must be the next revision')
        elif evidence['revision'] != 1:
            raise ValueError('first verification revision must be 1')
        self._conn().execute('INSERT INTO context_evidence(item_id,source_id,outcome,note,observed_at,created_at,event_key,task_id,verification_level,conditions_json,revision,experience_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                             (item_id,source_id,evidence['outcome'],source_note,now,now,event_key,evidence['task_id'],evidence.get('level',''),_json(evidence['conditions']),evidence['revision'],1))
        events = self._events(item_id)
        verdicts = self._task_verdicts(events)
        successes = sum(v in {'success','confirmed'} for v in verdicts.values())
        failures = sum(v == 'failure' for v in verdicts.values())
        contradicted = 'contradicted' in verdicts.values()
        status = 'active' if successes and not contradicted and failures < successes else 'candidate'
        confidence = min(.95, .7 + successes*.05) if status == 'active' else .4
        self._conn().execute('UPDATE context_items SET status=?,success_count=?,failure_count=?,confidence=?,last_verified_at=?,updated_at=?,source_count=(SELECT count(*) FROM context_sources WHERE item_id=?) WHERE id=?',
                             (status,successes,failures,confidence,now if successes else None,now,item_id,item_id))
        if status != 'active':
            for dependent in self.store.list_dependent_playbook_ids(item_id):
                self.store.set_item_status(dependent, ContextStatus.CANDIDATE)

    def _validate_evidence_scope(self, case_conditions, evidence):
        conditions = evidence['conditions']
        outcome = evidence['outcome']
        same_scope = conditions == case_conditions
        if outcome in {'success', 'confirmed'} and not same_scope:
            raise ValueError('positive evidence must cover case conditions exactly; derive a case')
        if outcome in {'failure', 'contradicted'} and not same_scope:
            raise ValueError('failure evidence is outside case conditions')
        if outcome == 'inapplicable' and not conditions:
            raise ValueError('inapplicable evidence requires conditions')

    def _resolve_source(self, item_id, evidence, now):
        supplied = evidence.get('source_id')
        if supplied is not None:
            source = self._conn().execute(
                'SELECT * FROM context_sources WHERE id=? AND item_id=?',
                (supplied,item_id)).fetchone()
            if source is None:
                raise ValueError('source does not belong to experience')
            if evidence['source_kind'] and evidence['source_kind'] != source['source_kind']:
                raise ValueError('source_id does not match source_kind')
            if evidence['source_ref'] and evidence['source_ref'] != source['source_ref']:
                raise ValueError('source_id does not match source_ref')
            if evidence['task_id'] == 'current':
                tasks = self._conn().execute(
                    'SELECT DISTINCT task_id FROM context_evidence '
                    'WHERE item_id=? AND source_id=?', (item_id, source['id'])).fetchall()
                if len(tasks) != 1 or not tasks[0]['task_id'] or tasks[0]['task_id'] == 'current':
                    raise ValueError('stored source lacks canonical task binding')
                evidence['task_id'] = tasks[0]['task_id']
            self._ensure_source_task(item_id, source['id'], evidence['task_id'])
            if not (
                re.fullmatch(r'experience-v1:[0-9a-f]{64}',
                             source['extraction_version']) and
                self.source_resolver.validate_stored(
                    source_kind=source['source_kind'],
                    source_ref=source['source_ref'], task_id=evidence['task_id'],
                    **({'extraction_version': source['extraction_version']} if source['archive_id'] else {}))
            ):
                raise ValueError('stored source lacks canonical provenance')
            previous = self._conn().execute(
                "SELECT note FROM context_evidence WHERE source_id=? "
                "AND instr(note, '来源摘录：') > 0 ORDER BY id LIMIT 1",
                (source['id'],)).fetchone()
            if previous is None:
                raise ValueError('stored source has no bound evidence snapshot')
            suffix = '\n来源摘录：' + previous['note'].split('来源摘录：', 1)[1]
            note = evidence['note'][:max(0, 2500 - len(suffix))] + suffix
            self._validate_source_level(source['source_kind'], evidence)
            return source['id'], note
        resolved = self.source_resolver.resolve(
            source_kind=evidence['source_kind'], source_ref=evidence['source_ref'],
            task_id=evidence['task_id'], quote=evidence['quote'])
        if resolved.task_id:
            evidence['task_id'] = resolved.task_id
        extraction = f'experience-v1:{resolved.digest}'
        self._conn().execute(
            'INSERT OR IGNORE INTO context_sources(item_id,archive_id,source_kind,source_ref,extraction_version,created_at) VALUES(?,?,?,?,?,?)',
            (item_id,resolved.archive_id,resolved.source_kind,resolved.source_ref,extraction,now))
        source = self._conn().execute(
            'SELECT id,extraction_version FROM context_sources WHERE item_id=? AND source_kind=? AND source_ref=?',
            (item_id,resolved.source_kind,resolved.source_ref)).fetchone()
        if source['extraction_version'] != extraction:
            raise ValueError('stored source provenance changed')
        self._ensure_source_task(item_id, source['id'], evidence['task_id'])
        self._validate_source_level(resolved.source_kind, evidence)
        note = evidence['note']
        suffix = f'\n来源摘录：{resolved.snapshot}'
        if suffix not in note:
            note = note[:max(0, 2500 - len(suffix))] + suffix
        return source['id'], note

    def _ensure_source_task(self, item_id, source_id, task_id):
        other_task = self._conn().execute(
            'SELECT 1 FROM context_evidence WHERE item_id=? AND source_id=? '
            'AND task_id != ? LIMIT 1',
            (item_id, source_id, task_id)).fetchone()
        if other_task is not None:
            raise ValueError('physical source is already bound to another task')

    @staticmethod
    def _validate_source_level(source_kind, evidence):
        if evidence['outcome'] not in {'success', 'confirmed'}:
            return
        level = evidence.get('level', '')
        if source_kind == 'user_confirmation' and level != 'user_confirmed':
            raise ValueError('user confirmation requires user_confirmed level')
        if source_kind == 'tool_result' and level == 'user_confirmed':
            raise ValueError('tool result cannot claim user_confirmed level')

    @staticmethod
    def _task_verdicts(events):
        return task_verdicts(events)

    def _events(self, item_id):
        latest = {}
        for row in self.store.list_evidence(item_id):
            if row['event_key']:
                previous = latest.get(row['event_key'])
                if previous is None or row['revision'] > previous['revision']:
                    latest[row['event_key']] = row
        return sorted(latest.values(), key=lambda r:r['id'])

    def _sync(self, item_id):
        from evolvmem.context_service import _VectorAftermath
        item = self.store.get_item(item_id, include_layers=False)
        if item.status is ContextStatus.ACTIVE:
            aftermath = _VectorAftermath(context_upserts=((item_id,self.store.get_layer(item_id,ContextLayer.L0)),))
        else:
            removals = (item_id, *self.store.list_dependent_playbook_ids(item_id))
            aftermath = _VectorAftermath(context_removals=removals)
        self.core._apply_vector_aftermath(aftermath)
        self._cache.clear()

    def read(self, item_id):
        self.core._require_serving()
        return self.describe(item_id)

    def describe(self, item_id):
        """Read evidence for operator inspection, including compat-mode Web.

        This does not enable Agent serving, retrieve candidates, or record use.
        Both surfaces share the same revision and independent-task accounting.
        """
        payload = self._payload(item_id)
        item = self.store.get_item(item_id, include_layers=False)
        events = self._events(item_id)
        return describe_case(payload,item_id=item_id,status=item.status.value,
            success_count=item.success_count,failure_count=item.failure_count,
            sources=self.store.list_item_sources(item_id),events=events,updated_at=item.updated_at)

    def _successful_verifications(self, events, sources):
        return successful_verifications(events, sources)

    def recall(self, *, project, query, constraints=None, workstream_id=None, mode='full'):
        self.core._require_serving()
        if mode not in ('full', 'hook'):
            raise ValueError('invalid recall mode')
        project = self.core._normalize_project(_text(project, 'project', 300, required=False))
        query = _text(query,'query',2000)
        constraints = _conditions(constraints or {})
        fingerprint = (self._conn().total_changes,self._conn().execute('PRAGMA data_version').fetchone()[0])
        cache_key = _json([project,query,constraints,workstream_id,fingerprint,mode])
        if workstream_id and cache_key in self._cache:
            return json.loads(self._cache[cache_key])
        request = ContextSearchRequest(
            query=query,project=project,cross_project=True, top_k=20,
            content_types=(ContextContentType.EXPERIENCE,ContextContentType.PLAYBOOK))
        # Experience recall is a supporting lookup, so it uses the read-only
        # retriever and does not inflate generic served-access counters.
        # Eligibility is resolved before every lexical/vector budget. Only
        # active local cases or explicitly transferable methods may compete.
        predicates = ["experience_payload != ''", "status='active'",
                      "(expires_at IS NULL OR expires_at > datetime('now'))",
                      "(project=? OR json_extract(experience_payload,'$.transferable')=1)"]
        params = [project]
        for key,value in constraints.items():
            predicates.append("NOT EXISTS (SELECT 1 FROM json_each(experience_payload,'$.conditions') c WHERE c.key=? AND c.value != ?)")
            params.extend((key,value))
        eligible_ids = tuple(r['id'] for r in self._conn().execute(
            'SELECT id FROM context_items WHERE ' + ' AND '.join(predicates), params))
        if not eligible_ids:
            return {'results':[], 'used_chars':0}
        # Automatic hooks skip optional online reranking. Every eligibility,
        # strict vector fallback, condition and evidence gate stays in force.
        search_options = {'allow_rerank': False} if mode == 'hook' else {}
        matches = self.core.retriever.search(request, eligible_ids=eligible_ids, **search_options)
        semantic = {m.id:m for m in matches}
        # Chinese natural sentences rarely share a whole phrase. A bounded
        # term query complements the existing vector channel without an LLM.
        terms = sorted(_terms(query), key=lambda t:(-len(t),t))[:40]
        lexical = []
        if terms:
            checks = ["instr(lower(l.content),?) > 0" for _ in terms]
            score_sql = ' + '.join('CASE WHEN '+check+' THEN 1 ELSE 0 END' for check in checks)
            placeholders = ','.join('?' for _ in eligible_ids)
            lexical = self._conn().execute(
                f"SELECT l.item_id,max({score_sql}) AS matched FROM context_layers l "
                f"WHERE l.item_id IN ({placeholders}) AND l.layer IN ('l0','l1') "
                "GROUP BY l.item_id HAVING matched > 0 ORDER BY matched DESC,l.item_id LIMIT 80",
                (*terms,*eligible_ids)).fetchall()
        candidate_ids = list(dict.fromkeys([m.id for m in matches] + [r['item_id'] for r in lexical]))[:80]
        def cases():
            for item_id in candidate_ids:
                # The extra lexical lane retains the existing live source gate.
                if item_id not in semantic and not memory_eligible(self.store,item_id):
                    continue
                yield self.read(item_id)
        response=rank_cases(cases(),project=project,query=query,constraints=constraints,semantic=semantic,
            vector_weight=self.config.context_vector_weight,
            reranker_min_score=self.config.context_reranker_min_score,
            max_chars=self.config.context_inject_related_max_chars)
        if workstream_id:
            if len(self._cache) >= 64:
                self._cache.clear()
            self._cache[cache_key] = _json(response)
        return response

    @staticmethod
    def _compact_verification(verification):
        return compact_verification(verification)
