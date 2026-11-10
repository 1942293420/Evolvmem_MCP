"""Windows DSH native transcript archiver. No model calls or history extraction.

Reads complete stable native JSONL/zstd snapshots. Source bytes are never rewritten.
Durable compressed DPAPI queue, per-session source order, resumable MCP chunks,
bounded queue/backoff and exact current archive receipts. Uses existing Codex
connection config and the named Windows CurrentUser environment credential.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import io
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid
import zstandard

FORMAT = 'dsh-session-jsonl-v1'
MAX_BYTES = 128 * 1024 * 1024
MAX_SAFE_INT = 9007199254740991
DEFAULT_CONFIG = Path.home()/'AppData/Local/EvolvMem/Codex/config.json'
DEFAULT_ROOT = DEFAULT_CONFIG.parent.resolve().parent/'DSH'


class ArchiveError(Exception):
    """Only fixed non-sensitive error codes reach status files or stdout."""


def utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return default


def atomic(path, value):
    path=Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    data=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
    try:
        with tmp.open('xb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if tmp.exists():tmp.unlink()


def dpapi(raw, decrypt=False):
    if os.name != 'nt':raise ArchiveError('windows_dpapi_required')
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_byte))]
    buf=ctypes.create_string_buffer(raw)
    source=Blob(len(raw),ctypes.cast(buf,ctypes.POINTER(ctypes.c_byte))); target=Blob()
    dll=ctypes.WinDLL('crypt32',use_last_error=True)
    fun=dll.CryptUnprotectData if decrypt else dll.CryptProtectData
    fun.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,
                  ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    fun.restype=wintypes.BOOL
    if not fun(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target)):
        raise ArchiveError('dpapi_failed')
    try:return ctypes.string_at(target.data,target.size)
    finally:
        free=ctypes.WinDLL('kernel32').LocalFree
        free.argtypes=[ctypes.c_void_p];free.restype=ctypes.c_void_p
        free(target.data)


def decompress(data):
    output=io.BytesIO();frame=None
    for start in range(0,len(data),1024):
        chunk=data[start:start+1024]
        while chunk:
            if frame is None or frame.eof:
                frame=zstandard.ZstdDecompressor(max_window_size=MAX_BYTES).decompressobj()
            try:part=frame.decompress(chunk)
            except zstandard.ZstdError:raise ArchiveError('invalid_compressed_source') from None
            if output.tell()+len(part)>MAX_BYTES:raise ArchiveError('source_size_limit')
            output.write(part);chunk=frame.unused_data if frame.eof else b''
    if frame is None or not frame.eof:raise ArchiveError('incomplete_compressed_source')
    return output.getvalue()


def decode_source(path):
    before=path.stat()
    if before.st_size>MAX_BYTES:raise ArchiveError('source_size_limit')
    compressed=path.read_bytes();after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise ArchiveError('source_changed_during_read')
    return decompress(compressed) if path.suffix in ('.zstd','.zst') else compressed


def validate_native(raw):
    def integer(n, minimum=0):return type(n)is int and minimum<=n<=MAX_SAFE_INT
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result:raise ValueError()
            result[k]=v
        return result
    def nonfinite(_):raise ValueError()
    if not raw or not raw.endswith(b'\n'):raise ArchiveError('incomplete_native_source')
    header=None;events=0;counts={}
    try:
        for i,line in enumerate(io.BytesIO(raw)):
            row=json.loads(line,object_pairs_hook=unique,parse_constant=nonfinite)
            if not isinstance(row,dict):raise ValueError()
            if i==0:
                header=row
                if row.get('version')!=4:raise ArchiveError('unsupported_native_version')
                required={'type','version','id','createdAt','isSeeded','delegationDepth'}
                optional={'cwd','parentSession','origin','agentPreset'}
                if (not required<=row.keys() or set(row)-required-optional or row['type']!='session'
                    or type(row['version']) is not int or not isinstance(row['id'],str) or not row['id']
                    or not integer(row['createdAt']) or type(row['isSeeded'])is not bool
                    or not integer(row['delegationDepth'])
                    or any(k in row and not isinstance(row[k],str) for k in ('cwd','parentSession','agentPreset'))
                    or ('origin'in row and row['origin']!='subagent')):raise ValueError()
                if row['isSeeded']:raise ArchiveError('unsupported_dsh_seeded_session')
            else:
                required={'type','seq','time','data'};optional={'ignorable','sourceEventSeqs','surfaceOp'}
                if (not required<=row.keys() or set(row)-required-optional
                    or not isinstance(row['type'],str) or not row['type'] or row['type']=='session'
                    or not integer(row['seq']) or row['seq']!=i-1
                    or not integer(row['time'],-MAX_SAFE_INT) or not isinstance(row['data'],dict)
                    or ('ignorable'in row and row['ignorable'] is not True)):raise ValueError()
                events+=1;counts[row['type']]=counts.get(row['type'],0)+1
    except (ValueError,UnicodeError,TypeError,KeyError):raise ArchiveError('invalid_native_source') from None
    return {'session_id':header['id'],'workspace_path':header.get('cwd',''),
            'events':events,'subagent':bool(header.get('parentSession') or header.get('origin')=='subagent' or header['delegationDepth']),
            'event_counts':counts}


def dialogue_and_tools(rows):
    """Compare native migration's visible dialogue/tools, never rewrite an archive.

    DSH v0 tool results use a single tool-result content wrapper; v4 moves
    those fields onto the tool message. System/injected context and streams
    are deliberately not claimed as byte-equivalent by this comparison.
    """
    values=[]
    for row in rows[1:]:
        kind=row['type'];data=row['data']
        if kind=='user/message' and data.get('source',{}).get('kind')=='user':
            values.append(('user',data.get('content')))
        elif kind=='assistant/message':values.append(('assistant',data.get('message',{}).get('content')))
        elif kind=='tool/call':values.append(('call',data))
        elif kind=='tool/result':
            message=data['message'];content=message.get('content',[])
            if len(content)==1 and content[0].get('type')=='tool-result':
                wrapper=content[0]
                values.append(('result',{'content':wrapper.get('content'),'isError':wrapper.get('isError'),
                                         'callId':wrapper.get('toolCallId')}))
            else:
                values.append(('result',{'content':content,'isError':message.get('isError'),
                                         'callId':message.get('toolCallId')}))
    return values


class MCP:
    def __init__(self, config_path):
        self.config=read_json(config_path)
        if not self.config:raise ArchiveError('connection_config_missing')
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,'Environment') as key:
                self.token=winreg.QueryValueEx(key,self.config['token_env_var'])[0]
        except OSError:raise ArchiveError('credential_unavailable') from None
        if not self.token:raise ArchiveError('credential_unavailable')
        # LAN traffic must not inherit a desktop/system proxy.
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def call(self, name, args, request_id=None):
        req=urllib.request.Request(self.config['url'],method='POST',headers={
            'Authorization':'Bearer '+self.token,'Content-Type':'application/json',
            'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25',
            'X-EvolvMem-Expected-User':self.config['expected_user']},data=json.dumps({
                'jsonrpc':'2.0','id':request_id or uuid.uuid4().hex,'method':'tools/call',
                'params':{'name':name,'arguments':args}},ensure_ascii=False).encode())
        try:
            with self.opener.open(req,timeout=45) as response:
                raw=response.read(2*1024*1024)
            envelope=json.loads(raw)
            if 'error'in envelope:raise ArchiveError('mcp_rpc_failed')
            result=envelope['result']; value=json.loads(result['content'][0]['text'])
            if result.get('isError') or value.get('error'):
                allowed={'invalid_dsh_transcript','unsupported_dsh_seeded_session','invalid_transcript',
                         'transcript_fork','upload_metadata_conflict','source_order_conflict',
                         'invalid_source_order','invalid_chunk','archive_encryption_unavailable',
                         'request_indeterminate','source_format_conflict','identity_mismatch'}
                code=value.get('error','')
                raise ArchiveError(code if code in allowed else 'mcp_tool_failed')
            if value.get('authenticated_user')!=self.config['expected_user']:raise ArchiveError('identity_mismatch')
            return value
        except (urllib.error.URLError,TimeoutError,OSError):raise ArchiveError('transport_unavailable') from None
        except (ValueError,KeyError,IndexError,TypeError):raise ArchiveError('invalid_mcp_response') from None


class Collector:
    def __init__(self, source, root, device_id, remote, chunk_bytes=256*1024,
                 max_pending_per_session=2, max_queue_bytes=2*1024**3):
        self.source=Path(source);self.root=Path(root);self.device_id=device_id;self.remote=remote
        self.chunk_bytes=chunk_bytes;self.max_pending=max_pending_per_session;self.max_queue=max_queue_bytes
        for name in ['queue','sessions','receipts']: (self.root/name).mkdir(parents=True,exist_ok=True)
    def session_file(self,sid):return self.root/'sessions'/(digest(sid.encode())+'.json')
    def manifest_files(self):return sorted((self.root/'queue').glob('*.json'))
    def summary(self):
        manifests=[read_json(p)for p in self.manifest_files()]
        sessions=[read_json(p)for p in (self.root/'sessions').glob('*.json')]
        scan=read_json(self.root/'scan.json',{})
        return dict(checked_utc=utc(),pending=len(manifests),pending_bytes=sum(m['total_bytes']for m in manifests),
                    confirmed_sessions=sum(s.get('confirmed_sha256')==s.get('current_sha256') and bool(s.get('confirmed_sha256'))for s in sessions),
                    known_sessions=len(sessions),unsupported=sum(x['code'].startswith('unsupported')for x in scan.get('issues',[])),
                    deferred=sum(x['code']=='pending_snapshot_limit'for x in scan.get('issues',[])),
                    legacy_versions=len(scan.get('legacy_versions',[])),
                    legacy_with_archived_native_v4=sum(n['archived_v4']for n in scan.get('legacy_versions',[])),
                    legacy_version_notes=scan.get('legacy_versions',[]),
                    issues=scan.get('issues',[]),failed_uploads=sum(bool(m.get('last_error'))for m in manifests))
    def legacy_upgrade(self,path,raw):
        if path.name!='session.jsonl.zstd':return None
        sibling=path.with_name('session.v4.jsonl.zstd')
        if not sibling.exists():return None
        try:
            old=[json.loads(line)for line in io.BytesIO(raw)]
            current=decode_source(sibling);details=validate_native(current)
            new=[json.loads(line)for line in io.BytesIO(current)]
            if old[0].get('version')!=0 or old[0].get('id')!=details['session_id']or old[0].get('cwd')!=new[0].get('cwd'):
                return None
            if dialogue_and_tools(old)!=dialogue_and_tools(new):return None
            sha=digest(current);state=read_json(self.session_file(details['session_id']),{})
            archived=state.get('confirmed_sha256')==sha and state.get('current_sha256')==sha and bool(state.get('archive_id'))
            return dict(session_id=details['session_id'],path=str(path),native_upgrade_available=True,
                        archived_v4=archived,visible_dialogue_and_tools_equal=True,raw_bytes_equal=False,
                        old_raw_bytes=len(raw),old_sha256=digest(raw),v4_sha256=sha,source_retained=True)
        except (ArchiveError,ValueError,TypeError,KeyError,OSError):return None
    def scan(self):
        if not self.source.is_dir():raise ArchiveError('source_root_unavailable')
        prior=read_json(self.root/'scan.json',{});cache=prior.get('files',{});seen={};issues=[];legacy=[]
        pending={}
        # Include encrypted payloads whose manifest commit was interrupted.
        # They are retained, but may not bypass the total disk budget.
        queue_bytes=sum(p.stat().st_size for p in (self.root/'queue').glob('*.dpapi'))
        for p in self.manifest_files():
            m=read_json(p);pending[m['session_id']]=pending.get(m['session_id'],0)+1
        paths=sorted((p for p in self.source.rglob('session*') if p.is_file() and p.name in
                      ['session.v4.jsonl.zstd','session.v4.jsonl','session.jsonl.zstd','session.jsonl']),
                     key=lambda p:p.stat().st_mtime_ns,reverse=True)
        for path in paths:
            key=str(path);st=path.stat();fingerprint=[st.st_size,st.st_mtime_ns]
            old=cache.get(key,{})
            if old.get('fingerprint')==fingerprint and old.get('captured'):
                seen[key]=old;continue
            try:
                raw=decode_source(path);details=validate_native(raw);sha=digest(raw);sid=details['session_id']
                statepath=self.session_file(sid);state=read_json(statepath,{})
                if state.get('current_sha256')==sha:
                    seen[key]=dict(fingerprint=fingerprint,captured=True,sha256=sha,session_id=sid);continue
                if pending.get(sid,0)>=self.max_pending:raise ArchiveError('pending_snapshot_limit')
                compressed=zstandard.ZstdCompressor(level=3).compress(raw)
                if queue_bytes+len(compressed)>self.max_queue:raise ArchiveError('queue_size_limit')
                encrypted=dpapi(compressed)
                if queue_bytes+len(encrypted)>self.max_queue:raise ArchiveError('queue_size_limit')
                order=max(int(time.time()*1000),int(state.get('source_order',0))+1)
                ident=digest((self.device_id+'|'+sid+'|'+str(order)+'|'+sha).encode())
                manifest=self.root/'queue'/(ident+'.json');blob=manifest.with_suffix('.dpapi')
                # Payload and manifest precede current-state update. Scan reconciliation
                # below repairs an interrupted enqueue without deleting unacked data.
                atomic(blob,encrypted)
                record=dict(version=1,session_id=sid,sha256=sha,total_bytes=len(raw),source_order=order,
                            source_format=FORMAT,next_offset=0,created_utc=utc(),attempts=0,next_attempt=0,
                            source_path=key,source_fingerprint=fingerprint,**{k:v for k,v in details.items()if k!='session_id'})
                atomic(manifest,record)
                state.update(session_id=sid,current_sha256=sha,source_order=order,source_path=key,
                             source_fingerprint=fingerprint,captured_utc=utc())
                atomic(statepath,state);pending[sid]=pending.get(sid,0)+1;queue_bytes+=blob.stat().st_size
                seen[key]=dict(fingerprint=fingerprint,captured=True,sha256=sha,session_id=sid)
            except ArchiveError as e:
                note=self.legacy_upgrade(path,raw) if str(e)=='unsupported_native_version' else None
                if note:
                    legacy.append(note)
                    if not note['archived_v4']:issues.append(dict(path=key,code='native_upgrade_awaiting_archive'))
                else:issues.append(dict(path=key,code=str(e)))
                seen[key]=dict(fingerprint=fingerprint,captured=False)
            except (OSError,ValueError,TypeError):
                issues.append(dict(path=key,code='source_read_failed'));seen[key]=dict(fingerprint=fingerprint,captured=False)
        atomic(self.root/'scan.json',dict(checked_utc=utc(),source_files=len(paths),files=seen,issues=issues,legacy_versions=legacy))
    def reconcile(self):
        for path in self.manifest_files():
            m=read_json(path);statepath=self.session_file(m['session_id']);state=read_json(statepath,{})
            if m['source_order']>state.get('source_order',0):
                state.update(session_id=m['session_id'],source_order=m['source_order'],
                             current_sha256=m['sha256'],source_path=m['source_path'],
                             source_fingerprint=m['source_fingerprint'],captured_utc=m['created_utc'])
                atomic(statepath,state)
    def upload(self, max_chunks=256, seconds=45, ignore_backoff=False):
        self.reconcile();deadline=time.monotonic()+seconds;used=0
        manifests=[(p,read_json(p))for p in self.manifest_files()]
        # Finish one immutable version per session before starting its successor.
        # A shorter rewritten successor must not overtake an unacked old version.
        oldest={}
        for path,m in manifests:
            if m['session_id']not in oldest or m['source_order']<oldest[m['session_id']][1]['source_order']:
                oldest[m['session_id']]=(path,m)
        manifests=list(oldest.values())
        manifests.sort(key=lambda pm:(bool(pm[1].get('last_error')),pm[1]['total_bytes'],pm[1]['source_order']))
        for path,m in manifests:
            if used>=max_chunks or time.monotonic()>=deadline:break
            if not ignore_backoff and m.get('next_attempt',0)>time.time():continue
            try:
                raw=decompress(dpapi(path.with_suffix('.dpapi').read_bytes(),decrypt=True))
                if len(raw)!=m['total_bytes']or digest(raw)!=m['sha256']:raise ArchiveError('queue_checksum_mismatch')
                count=0
                while used<max_chunks and count<32 and time.monotonic()<deadline:
                    state=read_json(self.session_file(m['session_id']),{})
                    current=state.get('current_sha256')==m['sha256']and state.get('source_order')==m['source_order']
                    offset=m['next_offset'];chunk=raw[offset:offset+self.chunk_bytes]
                    # Last-chunk replay is safe after a lost final local receipt.
                    if not chunk:
                        offset=max(0,len(raw)-min(self.chunk_bytes,len(raw)));chunk=raw[offset:]
                    # Include the declaration in the idempotency key: an uncertain
                    # current chunk retried after a new scan is a different request.
                    declaration=m['sha256'] if current else ''
                    req='dsh-'+digest((self.device_id+'|'+m['session_id']+'|'+m['sha256']+'|'+str(m['source_order'])+'|'+str(offset)+'|'+declaration).encode())
                    args=dict(device_id=self.device_id,session_id=m['session_id'],project='',sha256=m['sha256'],
                              total_bytes=len(raw),offset=offset,content_b64=base64.b64encode(chunk).decode(),
                              extract=False,source_format=FORMAT,source_order=m['source_order'],request_id=req)
                    if current:args['current_sha256']=m['sha256']
                    response=self.remote.call('session_archive_upload',args,req);used+=1;count+=1
                    if response.get('status')=='archived':
                        if (response.get('next_offset')!=len(raw)or response.get('source_sha256')!=m['sha256']
                            or response.get('source_format')!=FORMAT or not response.get('archive_id')
                            or response.get('total_bytes')!=len(raw) or response.get('source_order')!=m['source_order']
                            or (current and response.get('current')is not True)):
                            raise ArchiveError('archive_receipt_mismatch')
                        receipt=dict(session_id=m['session_id'],source_sha256=m['sha256'],total_bytes=len(raw),
                                     source_order=m['source_order'],source_format=FORMAT,archive_id=response['archive_id'],
                                     current=response.get('current',False),acknowledged_utc=utc(),
                                     extraction_status=response.get('extraction_status'),extract=False)
                        atomic(self.root/'receipts'/path.name,receipt)
                        if current:
                            state.update(confirmed_sha256=m['sha256'],confirmed_utc=utc(),archive_id=response['archive_id'])
                            atomic(self.session_file(m['session_id']),state)
                        # Only exact verified archived payloads may leave the queue.
                        path.unlink();path.with_suffix('.dpapi').unlink(missing_ok=True);break
                    if response.get('status')=='stale':raise ArchiveError('archive_not_current')
                    nxt=response.get('next_offset')
                    if type(nxt)is not int or not offset<nxt<=offset+len(chunk):raise ArchiveError('invalid_chunk_receipt')
                    m.update(next_offset=nxt,last_error=None,attempts=0,next_attempt=0)
                    atomic(path,m)
            except ArchiveError as e:
                m.update(last_error=str(e),attempts=m.get('attempts',0)+1,last_attempt_utc=utc())
                m['next_attempt']=time.time()+min(3600,30*2**min(m['attempts']-1,7));atomic(path,m)
            except (OSError,ValueError,TypeError,KeyError):
                m.update(last_error='local_queue_failure',next_attempt=time.time()+60);atomic(path,m)
        return used


class RunLock:
    def __init__(self,path):self.path=path;self.file=None
    def __enter__(self):
        import msvcrt
        self.path.parent.mkdir(parents=True,exist_ok=True);self.file=self.path.open('a+b')
        self.file.seek(0)
        if not self.file.read(1):self.file.write(b'1');self.file.flush()
        self.file.seek(0)
        try:msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:self.file.close();raise ArchiveError('worker_already_running') from None
        return self
    def __exit__(self,*args):
        import msvcrt
        self.file.seek(0);msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1);self.file.close()


def main():
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=['once','watch','scan','status'])
    ap.add_argument('--config',type=Path,default=DEFAULT_CONFIG);ap.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    ap.add_argument('--source',type=Path,default=Path.home()/'.dsh/sessions')
    ap.add_argument('--interval',type=int,default=60);ap.add_argument('--seconds',type=int,default=45)
    ap.add_argument('--max-chunks',type=int,default=256);args=ap.parse_args()
    config=read_json(args.config)
    if not config:raise ArchiveError('connection_config_missing')
    collector=Collector(args.source,args.root,config['device_id'],None)
    if args.action=='status':print(json.dumps(collector.summary(),ensure_ascii=False));return
    with RunLock(args.root/'worker.lock'):
        while True:
            started=time.monotonic();status=dict(started_utc=utc(),pid=os.getpid(),extract=False)
            try:
                collector.reconcile();collector.scan()
                if args.action!='scan':
                    remote=MCP(args.config);remote.call('memory_status',{'detail':'revision'})
                    collector.remote=remote;status['chunks']=collector.upload(args.max_chunks,args.seconds)
                status.update(collector.summary(),state='healthy')
                if status['failed_uploads']or status['pending']:status['state']='pending'
                elif status['issues']:status['state']='needs_attention'
            except ArchiveError as e:status.update(state='error',error=str(e))
            except Exception:status.update(state='error',error='worker_failed')
            status['finished_utc']=utc();atomic(args.root/'worker-status.json',status)
            if args.action!='watch':print(json.dumps(status,ensure_ascii=False));return
            time.sleep(max(1,args.interval-(time.monotonic()-started)))


if __name__=='__main__':
    try:main()
    except ArchiveError as e:
        print(json.dumps({'state':'error','error':str(e)}));raise SystemExit(1)
