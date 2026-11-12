"""Private short-lived maintenance sessions; no route activation or bulk import."""
from dataclasses import asdict
from pathlib import Path
import json, os, re, shutil, time, threading
from .history_core.mapping import check, ImportError, canonical
from .history_core.importer import transaction
from .operator_config import trusted, private_read
from .operator_target import target_for

IDENTITY='''SELECT current_database(),current_user,session_user,r.rolcanlogin,r.rolsuper,r.rolbypassrls,r.rolcreatedb,r.rolcreaterole,d.datdba=r.oid FROM pg_roles r JOIN pg_database d ON d.datname=current_database() WHERE r.rolname=current_user'''
CONTROL='''SELECT l.library_id::text,l.kind,l.owner_principal_id::text,l.device_id::text,l.project_id::text,l.backend,l.connection_ref,l.active,r.route_epoch,r.policy_epoch,r.authority_state,r.write_backend,r.acl_state FROM em_control.libraries l JOIN em_control.library_routes r USING(library_id) WHERE l.library_id=%s::uuid FOR SHARE OF l,r'''
STATE='''SELECT library_id::text,kind,device_id::text,project_id::text,authority_state,acl_state,route_epoch,policy_epoch FROM em_security.library_state WHERE singleton FOR SHARE'''
SCRIPTS=(('001_history_core.sql','history_schema_migrations'),('001_history_batch.sql','history_batch_schema_migrations'),('001_history_sources.sql','history_source_schema_migrations'),('002_history_sources_batch.sql','history_source_batch_schema_migrations'),('001_organization_qualification.sql','history_organization_schema_migrations'),('002_organization_bounded.sql','history_organization_batch_schema_migrations'),('001_history_capture.sql','history_capture_schema_migrations'),('001_history_governance.sql','history_governance_schema_migrations'))
DOMAIN_SCRIPTS={'core':(0,1),'sources':(2,3),'organization':(4,5),'capture':(6,),'governance':(7,)}

def sql_statements(text):
    """Split our bundled PostgreSQL SQL, preserving quoted procedural bodies."""
    start=0;i=0;quote=None
    while i<len(text):
        if quote:
            if text.startswith(quote,i):
                if quote in ("'",'"') and text.startswith(quote*2,i):i+=2;continue
                i+=len(quote);quote=None;continue
            i+=1;continue
        if text.startswith('--',i):
            end=text.find('\n',i);i=len(text) if end<0 else end+1;continue
        if text[i] in ("'",'"'):quote=text[i];i+=1;continue
        if text[i]=='$':
            match=re.match(r'\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$',text[i:])
            if match:quote=match[0];i+=len(quote);continue
        if text[i]==';':
            yield text[start:i+1];start=i+1
        i+=1
    check(quote is None,'operator_sql_unterminated')
    if text[start:].strip():yield text[start:]

class LeaseConnection:
    def __init__(self,runtime,raw,control,require_binding):
        self.runtime=runtime;self.raw=raw;self.control=control;self.require_binding=require_binding
        self.deadline=min(runtime.deadline,time.monotonic()+5);self._closed=False
        self.owner=(os.getpid(),threading.get_ident())
    @property
    def closed(self):return self._closed or self.raw.closed
    @property
    def autocommit(self):return self.raw.autocommit
    @property
    def info(self):return self.raw.info
    def tick(self):
        check(self.owner==(os.getpid(),threading.get_ident()),'operator_lease_owner_changed')
        check(not self.closed and not self.control.closed,'operator_lease_closed')
        check(time.monotonic()<self.deadline,'operator_lease_expired')
        self.runtime.guard()
        self.control.execute('SELECT 1').fetchall()
        check(time.monotonic()<self.deadline,'operator_lease_expired')
    def authorize(self,target):
        check(asdict(target)==asdict(self.runtime.target),'operator_target_mismatch')
        self.tick();self.runtime._content(self.raw,self.require_binding)
    def execute(self,sql,args=None):
        self.tick()
        milliseconds=max(1,min(3000,int((self.deadline-time.monotonic())*1000)))
        self.raw.execute("SELECT set_config('statement_timeout',%s,false)",(str(milliseconds),))
        result=self.raw.execute(sql) if args is None else self.raw.execute(sql,args)
        if isinstance(sql,str) and sql.startswith('BEGIN'):
            self.raw.execute("SET LOCAL statement_timeout='3000ms'")
            self.raw.execute("SET LOCAL lock_timeout='300ms'")
            self.raw.execute("SET LOCAL idle_in_transaction_session_timeout='5000ms'")
            self.runtime._content(self.raw,self.require_binding)
        return result
    def commit(self):
        self.tick()
        milliseconds=max(1,min(3000,int((self.deadline-time.monotonic())*1000)))
        self.raw.execute("SELECT set_config('statement_timeout',%s,false)",(str(milliseconds),))
        self.raw.commit()
    def rollback(self):return self.raw.rollback()
    def close(self):
        if self._closed:return
        self._closed=True
        try:self.raw.close()
        finally:
            try:self.control.rollback()
            finally:self.control.close()

class OperatorRuntime:
    def __init__(self,config,connector=None):
        trusted(config);self.config=config;self.target=target_for(config)
        self.deadline=time.monotonic()+config.command_seconds
        self.connector=connector
    def guard(self):
        check(time.monotonic()<self.deadline,'operator_command_deadline')
        d=self.config.document
        check(shutil.disk_usage(d['capacity_path']).free-d['peak_reserve_bytes']>=30*1024**3,'operator_disk_floor')
        check(os.name=='posix','operator_posix_required')
        available=None
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):available=int(line.split()[1])*1024
        check(available is not None and available>=2*1024**3,'operator_memory_floor')
    def _connect(self,control=False):
        d=self.config.document;t=d['target'];e=d['control' if control else 'content']
        identity=e if control else t
        connect=self.connector
        if connect is None:
            import psycopg
            connect=psycopg.connect
        private_read(e['ca_file'])
        password=private_read(e['password_file'],4096).decode().rstrip('\r\n')
        check(password and '\x00' not in password,'operator_secret_invalid')
        raw=connect(host=e['host'],port=e['port'],dbname=identity['database'],user=identity['login_role'],password=password,
                    sslmode='verify-full',sslrootcert=e['ca_file'],connect_timeout=3,autocommit=True,
                    options='-c statement_timeout=3000 -c lock_timeout=300 -c idle_in_transaction_session_timeout=5000',
                    keepalives=1,keepalives_idle=2,keepalives_interval=1,keepalives_count=2,tcp_user_timeout=3000)
        try:
            check(raw.closed is False and raw.autocommit is True and raw.info.transaction_status==0,'fresh_owner_connection_required')
            raw.execute('SET ROLE "'+identity['owner_role']+'"')
            check(raw.execute(IDENTITY).fetchall()==[(identity['database'],identity['owner_role'],identity['login_role'],False,False,False,False,False,True)],'owner_connection_identity_mismatch')
            # SET ROLE can hide dangerous session_user privileges; inspect the
            # authenticated login separately and allow only direct SET to this owner.
            flags=raw.execute('SELECT rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolinherit,rolreplication FROM pg_roles WHERE rolname=session_user').fetchall()
            check(flags==[(True,False,False,False,False,False,False)],'operator_maintenance_login_unsafe')
            memberships=raw.execute('SELECT pg_get_userbyid(roleid),admin_option,inherit_option,set_option FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=session_user) ORDER BY roleid').fetchall()
            check(memberships==[(identity['owner_role'],False,False,True)],'operator_maintenance_membership_unsafe')
            return raw
        except BaseException:raw.close();raise
    def _open(self,require_binding=True):
        self.guard();control=None;raw=None
        try:
            control=self._connect(True)
            control.execute('BEGIN ISOLATION LEVEL READ COMMITTED')
            t=self.config.document['target']
            expected=(t['library_id'],'legacy',t['owner_principal_id'],None,None,'postgres',t['connection_ref'],False,t['route_epoch'],t['policy_epoch'],'pg_staging','none','updating')
            check(control.execute(CONTROL,(t['library_id'],)).fetchall()==[expected],'operator_control_binding_mismatch')
            raw=self._connect();self.guard()
            return LeaseConnection(self,raw,control,require_binding)
        except BaseException:
            if raw is not None:raw.close()
            if control is not None:
                try:control.rollback()
                finally:control.close()
            raise
    def factory(self):return self._open()
    def _content(self,raw,require_binding):
        t=self.target
        check(raw.execute(STATE).fetchall()==[(t.library_id,'legacy',None,None,'pg_staging','updating',t.route_epoch,t.policy_epoch)],'operator_content_binding_mismatch')
        if require_binding:
            rows=raw.execute('SELECT binding_hash,manifest FROM em_security.history_operator_binding WHERE singleton FOR SHARE').fetchall()
            check(rows==[(self.config.binding_hash,self._manifest())],'operator_persisted_binding_mismatch')
            raw.execute("SELECT set_config('history.operator_binding',%s,true)",(self.config.binding_hash,))
            raw.execute('SELECT em_security.assert_history_operator_binding()')
    def _manifest(self):
        d=self.config.document
        return {'version':1,'binding_hash':self.config.binding_hash,'migration_id':self.config.migration_id,'target':d['target'],'source_seal_sha256':d['source_seal_sha256']}
    def _schema_ready(self,c,index):
        names=('001_history_six_fixture','001_history_bounded_chain','001_history_sources_seven','002_history_sources_bounded','001_organization_qualification_fixture','002_organization_bounded','001_history_capture_three','001_history_governance_twenty_six')
        table=SCRIPTS[index][1]
        if c.execute('SELECT to_regclass(%s) IS NOT NULL',('em_content.'+table,)).fetchall()!=[(True,)]:return False
        check(c.execute('SELECT version,migration_name,library_id::text FROM em_content.'+table).fetchall()==[(1,names[index],self.target.library_id)],'operator_schema_marker_mismatch')
        if index==7:
            from .governance_large_row import check_ledger
            check_ledger(c,'governance')
        return True
    def preflight(self):
        with transaction(lambda:self._open(False)) as c:
            exists=c.execute("SELECT to_regclass('em_security.history_operator_binding') IS NOT NULL").fetchall()==[(True,)]
            if exists:self._content(c.raw,True)
        domains={}
        for domain,indexes in DOMAIN_SCRIPTS.items():
            if domain=='capture' and self.config.source.namespace=='public':
                domains[domain]=False;continue
            with transaction(lambda:self._open(False)) as c:
                domains[domain]=exists and all(self._schema_ready(c,i) for i in indexes)
        return {'ready':domains['core'],'prepared':domains['core'],'binding_registered':exists,'domains':domains,
            'capture_applicability':'not_applicable' if self.config.source.namespace=='public' else 'applicable',
            'route_active':False,'complete':False}
    def _capture_parent_variables(self,parent_plan):
        from .capture_mapping import schema_hash
        schema_hash(self.config.source.namespace)
        from .source_plan import Plan
        check(type(parent_plan) is Plan,'trusted_capture_parent_plan_required')
        parent_plan.require_importable()
        check(parent_plan.header['source']['seal_receipt_sha256']==self.config.source.receipt_sha256,
              'capture_parent_seal_mismatch')
        return {'parent_run_id':parent_plan.run_id,'parent_manifest_sha256':parent_plan.sha256}
    def _governance_parent_variables(self,parent_plan,organization_plan):
        from .source_plan import Plan
        from .organization_complete import Plan as OrganizationPlan
        from .governance_mapping import schema_hash
        schema_hash(self.config.source.namespace)
        check(type(parent_plan) is Plan and type(organization_plan) is OrganizationPlan,'trusted_governance_parent_plans_required')
        parent_plan.require_importable()
        check(parent_plan.header['source']['seal_receipt_sha256']==self.config.source.receipt_sha256
            and organization_plan.parent.sha256==parent_plan.sha256,'governance_parent_seal_mismatch')
        return {'parent_run_id':parent_plan.run_id,'parent_manifest_sha256':parent_plan.sha256,
                'organization_run_id':organization_plan.run_id,'organization_manifest_sha256':organization_plan.sha256}
    def prepare(self,domain='core',*,parent_plan=None,organization_plan=None):
        check(domain in DOMAIN_SCRIPTS,'operator_domain_invalid')
        parent_variables=self._governance_parent_variables(parent_plan,organization_plan) if domain=='governance' else self._capture_parent_variables(parent_plan) if domain=='capture' else {}
        # Explicit registration is durable. Interrupted schema steps resume separately.
        with transaction(lambda:self._open(False)) as c:
            exists=c.execute("SELECT to_regclass('em_security.history_operator_binding') IS NOT NULL").fetchall()==[(True,)]
            if exists:self._content(c.raw,True)
            else:
                for statement in sql_statements((Path(__file__).parent/'sql'/'000_operator_binding.sql').read_text()):c.execute(statement)
                c.execute('INSERT INTO em_security.history_operator_binding(singleton,binding_hash,manifest) VALUES(true,%s,%s::jsonb)',(self.config.binding_hash,canonical(self._manifest()).decode()))
        applied=[]
        for index in DOMAIN_SCRIPTS[domain]:
            name,table=SCRIPTS[index]
            with transaction(self.factory) as c:
                if self._schema_ready(c,index):continue
                sql=(Path(__file__).parent/'sql'/name).read_text()
                sql='\n'.join(line for line in sql.splitlines() if not line.startswith('\\') and line not in ('BEGIN;','COMMIT;'))
                values={**asdict(self.target),'database_name':self.target.database,**parent_variables}
                sql=re.sub(r":'([a-z_][a-z0-9_]*)'",lambda m:"'"+str(values[m[1]]).replace("'","''")+"'",sql)
                for statement in sql_statements(sql):c.execute(statement)
                if index==7:
                    from .governance_large_row import check_ledger
                    check_ledger(c,'governance')
                applied.append(name)
        return {'prepared':True,'domain':domain,'applied':applied,'route_active':False,'complete':False}
