"""Bounded owner-only quarantine invalidation; NOT online organization mutation.

The canonical invalidation predicate and unit/basis changes are retained.
Task summaries and dual-mode capability/module refresh are explicitly deferred.
No source, item, archive, role, permission or import receipt is rewritten.
"""
from evolvmem.library_foundation import FoundationError
from .eligibility_sql import RECONCILE, _rows
from .historical_validation import _QuarantineRead

UNIT_SQL=_rows('''SELECT ordinal,decision FROM em_content.organization_units
 WHERE library_id=%s AND task_id=%s AND digest=%s ORDER BY ordinal LIMIT 2 FOR UPDATE''')
UPDATE_UNIT='''UPDATE em_content.organization_units SET decision='review',project='',reason=%s,
 revision=revision+1,updated_at=%s WHERE library_id=%s AND task_id=%s AND digest=%s AND decision='auto' RETURNING ordinal'''
UPDATE_BASIS='''UPDATE em_content.organization_context_basis SET state='invalid',updated_at=%s
 WHERE library_id=%s AND unit_task_id=%s AND unit_digest=%s AND state='active' RETURNING unit_task_id'''


def reconcile_quarantined_basis(factory,binding,*,limit=10):
    """One transaction, one bounded cascade step; explicit next invocation only.

    Existing bounded owner factory owns BEGIN/commit/rollback/deadline/capacity.
    Returning occurs AFTER its context exit, so unknown commit raises to caller;
    no success receipt is fabricated. A new call rechecks rows and is idempotent.
    """
    if type(limit) is not int or not 1<=limit<=128:raise FoundationError('invalid_reconciliation_limit')
    binding.checked();invalidated=0;touched=[]
    with factory() as connection:
        scope=_QuarantineRead(connection,binding)
        try:
            # Locks precede both stale selection and writes, so no other writer
            # can flip manual/auto or change predecessor rows between them.
            scope.gate(for_write=True)
            rows=scope._execute(RECONCILE,(binding.library_id,limit))
            if len(rows)>limit:raise FoundationError('reconciliation_row_limit')
            scope._record('reconciliation_candidates',(binding.library_id,limit),rows)
            seen=set()
            for (row,) in rows:
                key=(row['unit_task_id'],row['unit_digest'])
                if key in seen:raise FoundationError('ambiguous_basis_unit')
                seen.add(key)
                unit=scope._execute(UNIT_SQL,(binding.library_id,*key))
                scope._record('reconciliation_unit',(binding.library_id,*key),unit)
                if len(unit)!=1:raise FoundationError('ambiguous_basis_unit')
                if unit[0][0]['decision']!='auto':raise FoundationError('basis_candidate_changed')
                stamp=scope._execute("SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')")[0][0]
                reason='前文依据已变化或失效（%s 第 %d-%d 行），本批退回核对' % (
                    str(row['prior_source_key'] or row['prior_task_id']),
                    int(row['prior_start_line'] or 0),int(row['prior_end_line'] or 0))
                updated=scope._execute(UPDATE_UNIT,(reason[:600],stamp,binding.library_id,*key))
                changed=scope._execute(UPDATE_BASIS,(stamp,binding.library_id,*key))
                if len(updated)!=1 or len(changed)!=1:raise FoundationError('basis_update_changed')
                invalidated+=1;touched.append(key[0])
            next_rows=scope._execute(RECONCILE,(binding.library_id,1))
            scope._record('reconciliation_remaining',(binding.library_id,),next_rows)
            remaining=bool(next_rows)
        finally:scope.active=False
    return {'invalidated':invalidated,'remaining':remaining,'touched_tasks':tuple(sorted(set(touched))),
            'task_refresh_complete':False,'product_ready':False}
