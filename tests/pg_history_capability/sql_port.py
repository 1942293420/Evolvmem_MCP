"""SQLite effect port: never evidence for PG DDL, RLS, catalogs or locks."""
import json,re,sqlite3
from types import SimpleNamespace

class Cursor:
    def __init__(self,raw,sql):self.raw,self.sql=raw,sql
    def fetchall(self):
        result=self.raw.fetchall();sql=self.sql
        if 'SELECT input_hash,row_keys' in sql:return [(a,json.loads(b),c,d) for a,b,c,d in result]
        if 'SELECT binding_hash,manifest,state,loaded_counts' in sql:return [(a,json.loads(b),c,json.loads(d),e) for a,b,c,d,e in result]
        if 'SELECT r.binding_hash,r.manifest' in sql:return [(a,json.loads(b),c,d,e,bool(f)) for a,b,c,d,e,f in result]
        if 'SELECT manifest_hash,stage,next_load' in sql:return [(*r[:-1],None if r[-1] is None else json.loads(r[-1])) for r in result]
        if 'SELECT manifest_hash,manifest,loaded_counts' in sql:return [(a,json.loads(b),json.loads(c),d) for a,b,c,d in result]
        return result

class Connection:
    def __init__(self,env):
        self.env=env;self.raw=sqlite3.connect(env.target,isolation_level=None);self.raw.execute('PRAGMA foreign_keys=ON')
        self.raw.create_function('octet_length',1,lambda value:None if value is None else len(value.encode('utf8')))
        self.closed=False;self.autocommit=True;self.info=SimpleNamespace(transaction_status=0)
    def execute(self,sql,args=()):
        self.env.sql.append(sql)
        if sql.startswith(('SET LOCAL ','LOCK TABLE ')):return Cursor(self.raw.execute('SELECT 1 WHERE 0'),sql)
        if self.env.fault=='before_receipt' and sql.startswith('INSERT INTO em_content.history_capability_chunks'):raise RuntimeError('fixed test fault')
        if self.env.fault=='before_verified' and sql.startswith("UPDATE em_content.history_capability_progress SET stage='verified'"):raise RuntimeError('fixed test fault')
        translated=re.sub(r'::(?:uuid|jsonb|text)','',sql.replace('em_content.',''))
        translated=translated.replace('%s','?').replace(' FOR UPDATE','').replace(' FOR SHARE OF r,p','')
        if translated.startswith('BEGIN ISOLATION'):translated='BEGIN'
        return Cursor(self.raw.execute(translated,args),sql)
    def commit(self):
        self.raw.commit()
        if self.env.fault=='after_commit':raise RuntimeError('local postcommit fault, not network loss')
    def rollback(self):self.raw.rollback()
    def close(self):self.closed=True;self.raw.close()
