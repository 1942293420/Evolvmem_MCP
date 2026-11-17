"""SQLite effect-port adapter; not PG catalog/security/locking evidence."""
import json,re,sqlite3
from types import SimpleNamespace

class Cursor:
    def __init__(self,raw,sql):self.raw,self.sql=raw,sql
    def fetchall(self):
        result=self.raw.fetchall();sql=self.sql
        if 'SELECT input_hash,row_keys' in sql:return [(a,json.loads(b),c,d) for a,b,c,d in result]
        if 'SELECT manifest_hash,stage,next_load' in sql:return [(*r[:-1],None if r[-1] is None else json.loads(r[-1])) for r in result]
        if 'SELECT manifest_hash,manifest,loaded_counts' in sql:return [(a,json.loads(b),json.loads(c),d) for a,b,c,d in result]
        return result

class Connection:
    def __init__(self,env):
        self.env=env;self.raw=sqlite3.connect(env.target,isolation_level=None);self.raw.execute('PRAGMA foreign_keys=ON')
        self.raw.create_function('octet_length',1,lambda v:None if v is None else len(v if type(v) is bytes else v.encode('utf8')))
        self.closed=False;self.autocommit=True;self.info=SimpleNamespace(transaction_status=0)
    def execute(self,sql,args=()):
        self.env.sql.append(sql)
        if sql.startswith(('SET LOCAL ','LOCK TABLE ')):return Cursor(self.raw.execute('SELECT 1 WHERE 0'),sql)
        if self.env.fault=='before_receipt' and sql.startswith('INSERT INTO em_content.history_operations_chunks'):raise RuntimeError('fixed local fault')
        translated=re.sub(r'::(?:uuid|jsonb|text)','',sql.replace('em_content.',''))
        translated=translated.replace('%s','?').replace(' FOR UPDATE','')
        if translated.startswith('BEGIN ISOLATION'):translated='BEGIN'
        return Cursor(self.raw.execute(translated,args),sql)
    def commit(self):
        self.raw.commit()
        if self.env.fault=='after_commit':raise RuntimeError('local postcommit fault; not network loss')
    def rollback(self):self.raw.rollback()
    def close(self):self.closed=True;self.raw.close()
