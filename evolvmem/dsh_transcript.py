"""Read native DSH v4 JSONL without reconstructing its source archive.

Physical contract verified against installed DSH 0.2.0-rc.2 format catalog.
The caller retains the original bytes; dialogue is only a derived read view.
The bridge additionally validates with DSH persistence after idle + flush.
"""
from __future__ import annotations
import json
from evolvmem.codex_transcript import SUBAGENT_SESSION_REASON, same_client_workspace
from evolvmem.lan_sharing import LanError

SOURCE_FORMAT = 'dsh-session-jsonl-v1'
_HEADER_REQUIRED = {'type','version','id','createdAt','isSeeded','delegationDepth'}
_HEADER_OPTIONAL = {'cwd','parentSession','origin','agentPreset'}
_EVENT_REQUIRED = {'type','seq','time','data'}
_EVENT_OPTIONAL = {'ignorable','sourceEventSeqs','surfaceOp'}

def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 9007199254740991

def _object(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate key')
        result[key]=value
    return result

def _invalid_constant(value):
    raise ValueError('nonfinite number')

def parse_transcript(raw: bytes, session_id: str):
    if not raw or not raw.endswith(b'\n'):
        raise LanError('invalid_transcript')
    try:
        rows=[json.loads(line,object_pairs_hook=_object,parse_constant=_invalid_constant)
              for line in raw.decode('utf-8').split('\n')[:-1]]
        if not rows or any(not isinstance(row,dict) for row in rows): raise ValueError()
        header=rows[0]
        if (not _HEADER_REQUIRED <= header.keys() or set(header)-_HEADER_REQUIRED-_HEADER_OPTIONAL
                or header['type']!='session' or type(header['version']) is not int or header['version']!=4
                or header['id']!=session_id or not _integer(header['createdAt'])
                or type(header['isSeeded']) is not bool or not _integer(header['delegationDepth'])
                or any(key in header and not isinstance(header[key],str) for key in ('cwd','parentSession','agentPreset'))
                or ('origin' in header and header['origin']!='subagent')):
            raise ValueError()
        # Seeded/forked logs need their native inherited-cut protocol. This first
        # format supports fresh logs only; never silently discard inherited rows.
        if header['isSeeded']: raise LanError('unsupported_dsh_seeded_session')
        for seq,row in enumerate(rows[1:]):
            if (not _EVENT_REQUIRED <= row.keys() or set(row)-_EVENT_REQUIRED-_EVENT_OPTIONAL
                    or not isinstance(row['type'],str) or not row['type'] or row['type']=='session'
                    or not _integer(row['seq']) or row['seq']!=seq
                    or not _integer(row['time'],-9007199254740991)
                    or not isinstance(row['data'],dict)
                    or ('ignorable' in row and row['ignorable'] is not True)):
                raise ValueError()
    except (ValueError,TypeError,UnicodeError):
        raise LanError('invalid_dsh_transcript') from None
    return rows,dialogue_messages(rows)

def workspace_details(rows):
    header=rows[0];cwd=header.get('cwd','')
    parent=header.get('parentSession','')
    native=header.get('origin')=='subagent'
    subagent=bool(parent) or native or header['delegationDepth']>0
    reason='' if cwd and same_client_workspace(cwd,cwd) else 'workspace_unknown'
    if subagent: reason=SUBAGENT_SESSION_REASON
    return dict(cwd=cwd,attribution_reason=reason,parent_session_id=parent,subagent=subagent,native_subagent=native)

def dialogue_messages(rows):
    """Derived text view; original system/tool/stream events remain in raw bytes."""
    messages=[]
    roles={'user/message':'user','assistant/message':'assistant','tool/result':'tool',
           'system/message':'system','developer/message':'developer'}
    for row in rows[1:]:
        role=roles.get(row['type'])
        if role is None: continue
        data=row['data'];message=data if role=='user' else data.get('message')
        if not isinstance(message,dict) or message.get('role')!=role: continue
        content=message.get('content');source=message.get('source',{})
        # User-role context injections are not attributed to the actual user.
        if role=='user' and (not isinstance(source,dict) or source.get('kind')!='user'):continue
        if not isinstance(content,list):continue
        text='\n'.join(part['text'] for part in content if isinstance(part,dict)
                       and part.get('type')=='text' and isinstance(part.get('text'),str))
        if text:messages.append(dict(role=role,content=text))
    return messages
