"""Temporary real-HTTP console fixture. No OAuth network or auth bypass routes.

Launch from the isolated source tree; only --directory receives synthetic files.
Use test-owner-cookie/test-member-cookie as evolvmem_session cookie values.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time


def _source(path, label):
    from evolvmem.config import Config
    from evolvmem.context_service import ContextService
    from evolvmem.context_models import ContextMode
    from evolvmem.experience_service import ExperienceService
    from evolvmem.experience_sources import ExperienceSourceResolver
    from evolvmem.memory_store import MemoryStore
    config=Config(data_dir=path,apply_environment=False)
    config.ensure_dirs()
    with MemoryStore(config):
        pass
    core=ContextService(config)
    core.initialize(mode=ContextMode.SHADOW,adapter='codex')
    source_root=path/'synthetic-tool-results'
    source_root.mkdir()
    quote='Synthetic isolated verification passed for '+label
    transcript=source_root/'rollout-fixture-task.jsonl'
    transcript.write_text(json.dumps({'type':'response_item','payload':{
        'type':'function_call_output','output':quote}})+'\n')
    experience=ExperienceService(core,source_resolver=ExperienceSourceResolver(codex_roots=(source_root,)))
    case={'project':'demo','problem':label+' verified experience','conditions':{},
          'steps':['Inspect isolated fixture','Verify synthetic tool result'],'rationale':'Synthetic example',
          'result':'Temporary check passed','applicability':['Synthetic fixture'],
          'exclusions':['Production business data'],'transferable':False}
    item=experience.record(case,evidence={'task_id':'fixture-task','event_id':'fixture-event',
        'outcome':'success','level':'technical','source_kind':'tool_result',
        'source_ref':str(transcript.resolve())+'#1','quote':quote,
        'note':'Checked an isolated synthetic result','conditions':{}})
    experience.record({**case,'problem':label+' candidate experience'})
    return core,item['id']


def _history(directory,label):
    from tests.test_history_candidate_read import build_fixture
    directory.mkdir()
    db=directory/'candidate.db'
    build_fixture(db)
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6wAAAABJRU5ErkJggg==')
    sha=hashlib.sha256(png).hexdigest()
    (directory/'attachments').mkdir()
    (directory/'attachments'/'fixture.png').write_bytes(png)
    with sqlite3.connect(db) as conn:
        conn.execute('UPDATE messages SET content=?',(label+' synthetic history marker',))
        conn.execute('UPDATE blocks SET title=? WHERE block_id=?',(label+' synthetic dialogue','blk-dialogue'))
        conn.execute('CREATE TABLE attachments(sha256 TEXT,mime_type TEXT,bytes INTEGER,path TEXT)')
        conn.execute('INSERT INTO attachments VALUES(?,?,?,?)',(sha,'image/png',len(png),'fixture.png'))
        conn.execute('UPDATE messages SET attachments=? WHERE ordinal=0',(json.dumps([
            {'sha256':sha,'mime_type':'image/png','format_ok':True,'name':'fixture.png','bytes':len(png)}]),))
    return {'EVOLVMEM_HISTORY_CANDIDATE_DB':str(db),
        'EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE':str(directory/'unused-owner-verified-token-path'),
        'EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE':label},sha,png


class Fixture:
    def __init__(self,directory,port=0):
        from evolvmem import web_server,web_reads
        from evolvmem.web_auth import WebAuth,AuthSettings
        from evolvmem.knowledge_console import ConsoleService
        self.directory=Path(directory).resolve()
        if self.directory.exists():
            raise ValueError('fixture directory must be new')
        self.directory.mkdir(parents=True)
        self.reads=[]
        self.thread=None
        self.original_auth=web_server.WebAuth
        self.original_snapshot=web_reads.snapshot
        self.env_before={}
        self.owner,self.owner_experience_id=_source(self.directory/'owner','Owner')
        member,self.member_experience_id=_source(self.directory/'lan'/'users'/'kane','Member')
        member.close()
        owner_env,self.image_sha,self.image=_history(self.directory/'owner-history','Owner')
        member_env,_,_=_history(self.directory/'member-history','Member')
        policy=self.directory/'product-policy.json'
        policy.write_text(json.dumps({'console_enabled':True,'legacy_web_enabled':False,'ai_processing_enabled':False}))
        registry=self.directory/'metadata'/'users.db'
        changes={'EVOLVMEM_PRODUCT_POLICY_FILE':str(policy),'EVOLVMEM_CONSOLE_REGISTRY':str(registry),
            'EVOLVMEM_CONSOLE_OWNER_ID':'jiangli','EVOLVMEM_LAN_DATA_DIR':str(self.directory/'lan')}
        from evolvmem.knowledge_console import HISTORY_KEYS
        for key in HISTORY_KEYS:
            changes[key]=owner_env.get(key)
        for key,value in changes.items():
            self.env_before[key]=os.environ.get(key)
            if value is None:
                os.environ.pop(key,None)
            else:
                os.environ[key]=value
        self.console=ConsoleService(registry,'jiangli',self.owner.config.data_dir,self.directory/'lan',owner_env)
        self.console.bootstrap('test-owner-open-id','合成管理员')
        self.console.observe_login('test-member-open-id','合成成员')
        self.console.dispatch('POST','/users/kane',{'open_id':'test-member-open-id','display_name':'合成成员'},
                              {'open_id':'test-owner-open-id'})
        self.console.configure_library('kane',history_environ=member_env)
        self.console.observe_login('test-pending-open-id-1','待绑定合成用户一')
        self.console.observe_login('test-pending-open-id-2','待绑定合成用户二')
        settings=AuthSettings(app_id='synthetic-fixture-app',app_secret='synthetic-placeholder',
            owner_open_id='test-owner-open-id',redirect_uri='http://fixture.test/auth/feishu/callback',
            tenant_key='synthetic-fixture-tenant',owner_only=False)
        def auth_factory(_settings):
            self.auth=WebAuth(settings)
            for value,openid,name,csrf in (
                ('test-owner-cookie','test-owner-open-id','合成管理员','test-owner-csrf'),
                ('test-member-cookie','test-member-open-id','合成成员','test-member-csrf'),
                ('test-pending-cookie','test-pending-open-id-1','待绑定合成用户一','test-pending-csrf')):
                self.auth.sessions[value]={'open_id':openid,'name':name,'csrf':csrf,'expires':time.time()+8*3600}
            return self.auth
        @contextmanager
        def tracked_snapshot(config,**kwargs):
            self.reads.append(str(config.db_path))
            with self.original_snapshot(config,**kwargs) as reader:
                yield reader
        web_server.WebAuth=auth_factory
        web_reads.snapshot=tracked_snapshot
        self.server=web_server.make_http_server(self.owner,host='127.0.0.1',port=port)
        self.info={'pid':os.getpid(),'port':self.server.server_port,'url':f'http://127.0.0.1:{self.server.server_port}',
            'cookie_name':'evolvmem_session','owner_cookie':'test-owner-cookie','member_cookie':'test-member-cookie',
            'pending_cookie':'test-pending-cookie','owner_csrf':'test-owner-csrf','member_csrf':'test-member-csrf',
            'owner_id':'jiangli','member_id':'kane','owner_experience_id':self.owner_experience_id,
            'member_experience_id':self.member_experience_id,'image_sha':self.image_sha}
        (self.directory/'fixture.json').write_text(json.dumps(self.info,ensure_ascii=False,indent=2))

    def start(self):
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        return self

    def close(self):
        from evolvmem import web_server,web_reads
        if self.thread:
            self.server.shutdown()
            self.thread.join(timeout=3)
        self.server.server_close()
        self.owner.close()
        web_server.WebAuth=self.original_auth
        web_reads.snapshot=self.original_snapshot
        for key,value in self.env_before.items():
            if value is None:
                os.environ.pop(key,None)
            else:
                os.environ[key]=value


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--port',type=int,default=0)
    args=parser.parse_args()
    fixture=Fixture(args.directory,args.port).start()
    print(json.dumps({'fixture_file':str(fixture.directory/'fixture.json'),**fixture.info},ensure_ascii=False),flush=True)
    try:
        fixture.thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        fixture.close()


if __name__=='__main__':
    main()
