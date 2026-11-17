"""Bounded, lossless-source recovery of large native Codex transcript archives.

Authenticated plaintext is spooled to private temporary files. Only selected
dialogue is retained in memory; tool output and unrelated JSON fields are
syntax-checked without constructing their values. Original files never change.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import zlib

from evolvmem.archive_storage import ArchiveStorage, BLOCK_SIZE, MAGIC, _BLOCK_AAD, _MANIFEST_AAD
from evolvmem.conversation import clean_messages, render
from evolvmem.codex_transcript import dialogue_messages, workspace_details
from evolvmem.extraction_policy import redact_messages
from evolvmem.stream_json import JsonReader, StringSink, ValueProbe

MAX_LOGICAL_BYTES = 2 * 1024**3
MAX_MANIFEST_BYTES = 16 * 1024**2
MAX_CLEAN_BYTES = 16 * 1024**2


def _decompress(raw, limit):
    decoder = zlib.decompressobj()
    value = decoder.decompress(raw, limit+1)
    if len(value) > limit or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
        raise ValueError('archive_stream_decompression_budget')
    return value


def _private_file(directory, name, *, text=False):
    path = directory/name
    path.touch(mode=0o600, exist_ok=False)
    return path


def authenticated_spool(config, archive, physical, output):
    root = config.data_dir.resolve()
    target = (root/archive['payload_path']).resolve(strict=True)
    if not target.is_relative_to(root) or (root/archive['payload_path']).is_symlink():
        raise ValueError('archive_path_outside_data_dir')
    size = physical.get('logical_bytes')
    if type(size) is not int or not 0 <= size <= MAX_LOGICAL_BYTES:
        raise ValueError('archive_stream_logical_budget')
    if target.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('archive_stream_manifest_budget')
    blob = target.read_bytes()
    if hashlib.sha256(blob).hexdigest() != physical['manifest_sha256'] or not blob.startswith(MAGIC):
        raise ValueError('archive_manifest_changed')
    key_path = root/'archive.key'
    if not key_path.is_file(): raise ValueError('archive_key_missing')
    storage = ArchiveStorage(config, key_path.read_bytes())
    encoded = blob[len(MAGIC):]
    raw_manifest = _decompress(storage.aes.decrypt(encoded[:12], encoded[12:], _MANIFEST_AAD), MAX_MANIFEST_BYTES)
    manifest = json.loads(raw_manifest)
    if (manifest.get('version') != 2 or not isinstance(manifest.get('parts'),list)
            or manifest.get('size') != size or manifest.get('sha256') != physical['content_sha256']):
        raise ValueError('archive_metadata_mismatch')
    digest = hashlib.sha256(); count = 0
    with output.open('wb') as stream:
        for part in manifest['parts']:
            if isinstance(part,str):
                chunk_path = storage._path(part)
                if chunk_path.is_symlink() or chunk_path.stat().st_size > 2*BLOCK_SIZE:
                    raise ValueError('archive_block_changed')
                chunk = chunk_path.read_bytes()
                raw = _decompress(storage.aes.decrypt(chunk[:12],chunk[12:],_BLOCK_AAD), BLOCK_SIZE)
                if storage._digest(raw) != part: raise ValueError('archive_block_changed')
            elif isinstance(part,dict) and set(part)=={'inline'} and isinstance(part['inline'],str) and len(part['inline']) <= 4*BLOCK_SIZE:
                raw = base64.b64decode(part['inline'],validate=True)
                if len(raw)>BLOCK_SIZE: raise ValueError('archive_block_changed')
            else:
                raise ValueError('invalid_archive_manifest')
            count += len(raw)
            if count > size: raise ValueError('archive_content_changed')
            digest.update(raw);stream.write(raw)
    if count != size or digest.hexdigest() != manifest['sha256']:
        raise ValueError('archive_content_changed')
    return {'logical_bytes':count,'sha256':digest.hexdigest(),'parts':len(manifest['parts'])}


def native_dialogue(transcript, session_id, policy, *, max_clean_bytes=MAX_CLEAN_BYTES):
    with transcript.open('rb') as stream:
        stream.seek(0,2)
        if not stream.tell(): raise ValueError('invalid_transcript')
        stream.seek(-1,2)
        if stream.read(1)!=b'\n': raise ValueError('invalid_transcript')
    selector = {'type':True,'payload':{'type':True,'id':True,'role':True,'channel':True,'recipient':True,
        'content':[{'type':True,'text':True}], 'message':True, 'source':True, 'parent_thread_id':True,
        'output':ValueProbe()}}
    messages=[]; previous=None; previous_clean=None; metadata=None; records=0; total=0
    with transcript.open('r',encoding='utf-8',newline='') as stream:
        reader=JsonReader(stream)
        while True:
            reader.white()
            if not reader.peek(): break
            reader.retained=0
            line=reader.line
            row=reader.value(selector);records+=1
            if reader.line!=line:raise ValueError('invalid_transcript')
            reader.white()
            if reader.peek() and reader.line==line:raise ValueError('invalid_transcript')
            if not isinstance(row,dict): raise ValueError('invalid_transcript')
            payload=row.get('payload')
            if row.get('type')=='session_meta' and metadata is None:
                metadata=row
            if not isinstance(payload,dict):continue
            if payload.get('type') in {'function_call_output','custom_tool_call_output'}:
                payload['output']='[tool output omitted from cleaned dialogue]' if payload.get('output') else ''
            for message in dialogue_messages([row]):
                marker=(message['role'],message['content'])
                if previous and previous[:2]==marker and previous[2]!=row.get('type'):
                    previous=None;continue
                previous=(*marker,row.get('type'))
                for cleaned in clean_messages([message],policy=policy):
                    if (policy or {}).get('settings',{}).get('cleaning_collapse_duplicates') and previous_clean==cleaned:
                        continue
                    previous_clean=cleaned
                    redacted,_=redact_messages([cleaned])
                    if not redacted:continue
                    entry=redacted[0]
                    total+=len(entry['content'].encode('utf-8'))+64
                    if total>max_clean_bytes:raise ValueError('cleaned_archive_budget_exceeded')
                    messages.append(entry)
    if metadata is None or not isinstance(metadata.get('payload'),dict) or metadata['payload'].get('id')!=session_id:
        raise ValueError('session_id_mismatch')
    if workspace_details([metadata])['subagent']:
        messages=[]
    return messages,{'native_records':records,'cleaned_messages':len(messages),'cleaned_bytes':total}


def derive(config, archive, physical, policy, *, max_clean_bytes=MAX_CLEAN_BYTES, temp_root=None):
    size=physical.get('logical_bytes')
    if type(size) is not int or not 0 <= size <= MAX_LOGICAL_BYTES:
        raise ValueError('archive_stream_logical_budget')
    if type(max_clean_bytes) is not int or not 0<max_clean_bytes<=MAX_CLEAN_BYTES:
        raise ValueError('invalid_cleaning_budget')
    # /tmp is commonly tmpfs: spooling there would still consume RAM and can
    # exhaust a memory-limited recovery process. Use a private disk cache.
    temp_root=Path(temp_root) if temp_root is not None else Path.home()/'.cache'/'evolvmem'/'streaming-recovery'
    temp_root.mkdir(mode=0o700,parents=True,exist_ok=True)
    if temp_root.is_symlink() or temp_root.stat().st_mode & 0o077:
        raise ValueError('archive_stream_temp_directory_not_private')
    if shutil.disk_usage(temp_root).free < 2*size+64*1024**2:
        raise ValueError('archive_stream_temp_space_low')
    with tempfile.TemporaryDirectory(prefix='evo-private-archive-stream-',dir=temp_root) as temporary:
        directory=Path(temporary)
        payload=_private_file(directory,'payload.json')
        transcript=_private_file(directory,'transcript.jsonl')
        proof=authenticated_spool(config,archive,physical,payload)
        with payload.open('r',encoding='utf-8',newline='') as source, transcript.open('w',encoding='utf-8',newline='') as destination:
            reader=JsonReader(source)
            outer=reader.value({'session_id':True,'transcript':StringSink(destination)})
            reader.finish()
            if reader.root_keys & {'conversation','messages'}:
                raise ValueError('archive_stream_unsupported_outer_dialogue')
        payload.unlink()
        if not isinstance(outer,dict) or not isinstance(outer.get('session_id'),str) or not isinstance(outer.get('transcript'),StringSink):
            raise ValueError('archive_stream_native_transcript_required')
        messages,counts=native_dialogue(transcript,outer['session_id'],policy,max_clean_bytes=max_clean_bytes)
    return messages,{**proof,**counts,'method':'authenticated_streaming_native_transcript'}


def repair(service, plan, source_key, *, apply=False):
    from evolvmem import governance_migration as migration
    from evolvmem.context_store import _now_iso
    migration._validate(plan)
    entries=[e for e in plan['cleaning_repairs'] if e['source_key']==source_key]
    if len(entries)!=1:raise ValueError('archive_not_in_cleaning_plan')
    entry=entries[0];conn=service.store._connection();policy=service.knowledge().rules.read()
    if policy['revision']!=plan['rule_revision']:raise ValueError('migration_revision_conflict')
    if ('cleaning',source_key) in migration._audit_rows(conn,plan):
        return {'source_key':source_key,'already_repaired':True}
    inventory=migration.Inventory(conn,plan['rule_revision'],keys={source_key})
    def check(current):
        live=current.entry(source_key)
        if (current.stamp(source_key)!=entry['expected'] or not live['current_archive']
                or live['raw']['state']!='available' or live['cleaning']['body_state']!='missing'
                or live['reason'] not in {'source_body_unverifiable','already_assigned_archive'}):
            raise ValueError('migration_revision_conflict')
    check(inventory);identity=int(source_key.split(':')[1])
    archive=inventory.archives[identity];physical=inventory.storage[identity]
    messages,proof=derive(service.config,archive,physical,policy)
    encoded=json.dumps(messages,ensure_ascii=False)
    report={'source_key':source_key,**proof,'content_hash':hashlib.sha256(encoded.encode()).hexdigest(),'applied':False}
    if apply:
        with service.store.transaction():
            if service.knowledge().rules.read()['revision']!=policy['revision']:
                raise ValueError('migration_revision_conflict')
            check(migration.Inventory(conn,plan['rule_revision'],keys={source_key}))
            target=service.config.data_dir/archive['payload_path']
            if target.is_symlink() or target.stat().st_size>MAX_MANIFEST_BYTES:
                raise ValueError('archive_manifest_changed')
            if hashlib.sha256(target.read_bytes()).hexdigest()!=physical['manifest_sha256']:
                raise ValueError('archive_manifest_changed')
            conn.execute('INSERT INTO conversation_history(archive_id,messages,body,content_hash,cleaned_at) VALUES(?,?,?,?,?)',
                         (identity,encoded,render(messages),report['content_hash'],_now_iso()))
            migration._ensure_audit(conn)
            migration._record_audit(conn,plan,'cleaning',source_key,{**report,'applied':True})
        report['applied']=True
    return report


def main():
    started=time.monotonic()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True);parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--policy-data-dir',type=Path);parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--source-key',required=True);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    from evolvmem.config import Config
    from evolvmem.context_store import ContextStore
    from evolvmem.knowledge_rules import KnowledgeRules
    conn=sqlite3.connect(args.db.resolve().as_uri()+('?mode=rw' if args.apply else '?mode=ro'),uri=True)
    conn.row_factory=sqlite3.Row;conn.execute('PRAGMA foreign_keys=ON')
    config=Config(data_dir=args.data_dir,apply_environment=False)
    store=ContextStore(config);store._conn=conn
    service=SimpleNamespace(config=config,store=store,knowledge=lambda:SimpleNamespace(rules=KnowledgeRules(args.policy_data_dir or args.data_dir)))
    try:
        report=repair(service,json.loads(args.plan.read_text()),args.source_key,apply=args.apply)
        report['elapsed_seconds']=round(time.monotonic()-started,3)
        try:
            import resource
            report['peak_rss_kib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except ImportError:
            pass
        print(json.dumps(report,ensure_ascii=False))
    finally:conn.close()

if __name__=='__main__':main()
