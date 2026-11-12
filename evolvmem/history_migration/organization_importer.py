"""Owner quarantine catalog gate; obsolete giant-transaction entry is disabled."""
import hashlib
from . import organization_scan as S
from . import organization_plan as P

def require(value,message):
 if not value:raise ValueError(message)

def gate(c,library):
 parents=('memories','context_items','context_layers','legacy_memory_migrations','context_project_registry','context_project_resolutions','session_archives','session_archive_storage','session_archive_holds','conversation_history','archive_source_exclusions','context_sources','context_evidence')
 for t in parents+S.TABLES:c.execute('LOCK TABLE em_content.'+t+' IN SHARE ROW EXCLUSIVE MODE')
 c.execute('''SELECT c.run_id::text,s.run_id::text,cp.generation,sp.generation FROM em_content.history_import_runs c
 JOIN em_content.history_batch_progress cp ON cp.run_id=c.run_id
 JOIN em_content.history_source_runs s ON s.core_run_id=c.run_id AND s.library_id=c.library_id
 JOIN em_content.history_source_batch_progress sp ON sp.run_id=s.run_id
 WHERE c.library_id=%s::uuid AND c.state='core_verified' AND cp.stage='verified'
 AND cp.generation=cp.verify_generation AND sp.stage='verified' AND sp.generation=sp.verify_generation
 FOR UPDATE OF c,cp,s,sp''',(library,))
 proof=c.fetchall();require(len(proof)==1,'verified_six_plus_seven_parents_required')
 c.execute('''SELECT kind,device_id,project_id,authority_state,acl_state FROM em_security.library_state
 WHERE singleton AND library_id=%s::uuid FOR UPDATE''',(library,))
 require(c.fetchall()==[('legacy',None,None,'pg_staging','updating')],'owner_quarantine_state_required')
 c.execute('''SELECT r.rolcanlogin,r.rolsuper,r.rolbypassrls,r.rolcreatedb,r.rolcreaterole
 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid WHERE r.rolname=current_user AND d.datname=current_database()''')
 require(c.fetchall()==[(False,False,False,False,False)],'isolated_database_owner_required')
 for t in parents+S.TABLES:
  c.execute('''SELECT c.relowner=current_user::regrole,c.relrowsecurity,c.relforcerowsecurity,
 EXISTS(SELECT 1 FROM pg_policy p WHERE p.polrelid=c.oid AND NOT p.polpermissive AND p.polcmd='*'
 AND p.polroles=ARRAY[0::oid] AND pg_get_expr(p.polqual,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)'
 AND pg_get_expr(p.polwithcheck,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)')
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='em_content' AND c.relname=%s''',(t,))
  require(c.fetchall()==[(True,True,True,True)],'table_owner_quarantine_required')
 return proof

def import_manifest(*args,**kwargs):
 """Removed unsafe one-transaction prototype; use organization_complete.Importer."""
 raise ValueError('organization_bounded_manifest_api_required')
