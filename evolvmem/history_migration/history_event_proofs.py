"""Owner-side bounded proof of existing original experience events.

Reads a supplied consistent SQLite snapshot and existing encrypted archives.
It never initializes ContextStore, creates a key, writes SQL, or re-extracts.
Returned records are hashes/identifiers only; raw events remain in their archive.
"""
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from evolvmem.experience_sources import ExperienceSourceResolver
from evolvmem.session_archive import SessionArchiver

MAX_EVENTS=240

class ProofUnavailable(ValueError):
    """Content-free reason; callers retain original raw evidence unchanged."""


class SnapshotArchiveRows:
    def __init__(self,connection):
        if connection.execute('PRAGMA query_only').fetchone()[0]!=1:
            raise ProofUnavailable('read_only_snapshot_required')
        self.connection=connection
    def get_session_archive(self,archive_id):
        if type(archive_id) is not int or archive_id<1:raise ProofUnavailable('invalid_archive_id')
        cursor=self.connection.execute('SELECT * FROM session_archives WHERE id=?',(archive_id,))
        row=cursor.fetchone()
        return dict(zip((c[0] for c in cursor.description),row)) if row else None


class ReadOnlySnapshotArchiver(SessionArchiver):
    """Use the real archive safe-read API while making missing-key creation impossible."""
    def _load_or_create_key(self):
        key=self._key_path.read_bytes()
        if len(key)!=32:raise ProofUnavailable('archive_key_invalid')
        return key


def snapshot_archiver(connection,data_dir):
    path=Path(data_dir).resolve(strict=True)
    if not path.is_dir():raise ProofUnavailable('archive_directory_unavailable')
    return ReadOnlySnapshotArchiver(SimpleNamespace(data_dir=path),SnapshotArchiveRows(connection))


def verify_event_binding(*,library_id,item,source,evidence,l1_hash,archiver):
    """Validate one existing event with canonical original role/hash/task rules."""
    try:
        if (source['item_id']!=item['id'] or evidence['item_id']!=item['id']
                or evidence['source_id']!=source['id'] or evidence['experience_version']!=1
                or item['status']!='active' or item['revision']<1):
            raise ProofUnavailable('event_binding_mismatch')
        if source['source_kind'] not in ('user_confirmation','tool_result'):
            raise ProofUnavailable('native_event_proof_required')
        if not re.fullmatch(r'experience-v1:[0-9a-f]{64}',source['extraction_version']):
            raise ProofUnavailable('original_event_hash_missing')
        match=re.fullmatch(r'archive:([1-9][0-9]*)#([1-9][0-9]*)',source['source_ref'])
        if match is None or source['archive_id']!=int(match[1]):
            raise ProofUnavailable('archive_event_reference_required')
        key=json.loads(evidence['event_key'])
        if (not isinstance(key,list) or len(key)!=3 or key[0]!=evidence['task_id'] or key[2]!=1
                or not isinstance(key[1],str) or not key[1]):raise ProofUnavailable('canonical_event_key_required')
        conditions=json.loads(evidence['conditions_json'])
        if (not isinstance(conditions,dict) or len(conditions)>20
                or any(not isinstance(k,str) or not isinstance(v,str) for k,v in conditions.items())):
            raise ProofUnavailable('invalid_event_conditions')
        case=item.get('experience_payload') or {}
        if isinstance(case,str):case=json.loads(case)
        if evidence['outcome'] in ('success','confirmed','failure','contradicted') and conditions!=case.get('conditions',{}):
            raise ProofUnavailable('event_conditions_mismatch')
        if evidence['outcome']=='inapplicable' and not conditions:raise ProofUnavailable('event_conditions_mismatch')
        if (source['source_kind']=='user_confirmation' and evidence['outcome'] in ('success','confirmed')
                and evidence['verification_level']!='user_confirmed'):
            raise ProofUnavailable('invalid_event_level')
        if source['source_kind']=='tool_result' and evidence['verification_level']=='user_confirmed':
            raise ProofUnavailable('invalid_event_level')
        archive=archiver.store.get_session_archive(source['archive_id'])
        if archive is None or archive['state']!='available':raise ProofUnavailable('archive_unavailable')
        payload=archiver.read_payload(source['archive_id'])
        if not isinstance(payload,str) or hashlib.sha256(payload.encode()).hexdigest()!=archive['payload_sha256']:
            raise ProofUnavailable('archive_payload_hash_mismatch')
        marker='来源摘录：';note=evidence['note']
        if marker not in note:raise ProofUnavailable('source_snapshot_missing')
        quote=note.split(marker,1)[1].strip()
        if not quote or len(quote)>1000:raise ProofUnavailable('source_snapshot_missing')
        # Supplying no adapter roots prevents filesystem fallback or scans. The
        # exact original archive ref is the only source this resolver can read.
        resolver=ExperienceSourceResolver(codex_roots=(),kimi_roots=(),dsh_roots=(),archiver=archiver)
        resolved=resolver.resolve(source_kind=source['source_kind'],source_ref=source['source_ref'],
            task_id=evidence['task_id'],quote=quote)
        if (resolved.source_ref!=source['source_ref'] or resolved.archive_id!=source['archive_id']
                or resolved.task_id!=evidence['task_id'] or source['extraction_version']!='experience-v1:'+resolved.digest):
            raise ProofUnavailable('original_event_binding_changed')
        if not re.fullmatch(r'[0-9a-f]{64}',l1_hash):raise ProofUnavailable('item_content_hash_required')
        proof=dict(library_id=library_id,source_id=source['id'],item_id=item['id'],
            source_ref=source['source_ref'],extraction_version=source['extraction_version'],archive_id=source['archive_id'],
            archive_payload_sha256=archive['payload_sha256'],task_id=evidence['task_id'],
            role='user' if source['source_kind']=='user_confirmation' else 'tool',event_hash=resolved.digest,
            event_ref=resolved.source_ref,state='verified',evidence_id=evidence['id'],evidence_revision=evidence['revision'],
            experience_version=evidence['experience_version'],evidence_note_hash=hashlib.sha256(note.encode()).hexdigest(),
            item_revision=item['revision'],item_content_hash=l1_hash)
        proof['proof_hash']=hashlib.sha256(json.dumps(proof,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
        return proof
    except ProofUnavailable:raise
    except (KeyError,TypeError,ValueError,OSError):raise ProofUnavailable('original_event_unverifiable') from None


def verify_batch(*,library_id,rows,archiver):
    """Bounded helper only; the trusted migration owner persists resulting proofs."""
    if not isinstance(rows,(list,tuple)) or len(rows)>MAX_EVENTS:raise ProofUnavailable('event_proof_budget')
    proofs=[];held=[]
    for row in rows:
        try:proofs.append(verify_event_binding(library_id=library_id,archiver=archiver,**row))
        except ProofUnavailable as exc:
            held.append(dict(item_id=row['item']['id'],evidence_id=row['evidence']['id'],reason=str(exc)))
    return dict(proofs=proofs,held=held)
