"""B6 depends only on the verified core; budget its legacy metadata reads first."""
from .history_core.importer import _rows
from .history_core.mapping import check
from .batched_importer import WATCHED_TABLES

def check_parent_trigger_qualifications(c):
    found=_rows(c,"""SELECT c.relname,t.tgqual IS NULL,t.tgattr=''::int2vector,
        t.tgnargs=0,octet_length(t.tgargs)=0
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='em_content' AND t.tgname='history_batch_generation' ORDER BY c.relname""")
    check(found==[(t,True,True,True,True) for t in sorted(WATCHED_TABLES)]
        and all(type(v) is bool for r in found for v in r[1:]),'continuity_parent_generation_qualification')

def checked_progress(c,port,parent):
    run=_rows(c,"SELECT octet_length(manifest::text),state='core_verified' FROM em_content.history_import_runs WHERE run_id=%s::uuid FOR UPDATE",(parent.run_id,))
    check(len(run)==1 and type(run[0][0]) is int and 0<=run[0][0]<=16*1024*1024,'continuity_parent_manifest_budget')
    check(run[0][1] is True,'verified_core_parent_required')
    cursor=_rows(c,'''SELECT octet_length(loaded_counts::text),COALESCE(octet_length(count_last::text),0)
        FROM em_content.history_batch_progress WHERE run_id=%s::uuid FOR UPDATE''',(parent.run_id,))
    check(len(cursor)==1 and all(type(v) is int for v in cursor[0])
        and 0<=cursor[0][0]<=16384 and 0<=cursor[0][1]<=1024,'continuity_parent_progress_budget')
    result=port._progress(c,parent)
    check(result['stage']=='verified' and result['generation']==result['verify_generation'],'verified_core_parent_required')
    return result
