"""Keep absent sequence evidence distinct; migrate exhausted IDs without reuse."""
from . import operations_mapping as M
from .history_core.importer import _rows


def allocation(evidence,maximum):
    M.check(type(evidence) is dict and set(evidence)=={'present','value','storage_type','autoincrement_ddl_verified','allocation_history_verified'}
        and evidence['autoincrement_ddl_verified'] is True and evidence['allocation_history_verified'] is False
        and type(maximum) is int and M.INT64_MIN<=maximum<=M.INT64_MAX,'operations_sequence_invalid')
    if evidence['present'] is False:
        M.check(evidence['value'] is None and evidence['storage_type'] is None,'operations_sequence_invalid')
        high=maximum
    else:
        M.check(evidence['present'] is True and evidence['storage_type']=='integer'
            and type(evidence['value']) is int and M.INT64_MIN<=evidence['value']<=M.INT64_MAX,'operations_sequence_invalid')
        high=max(maximum,evidence['value'])
    return (M.INT64_MAX,True) if high==M.INT64_MAX else (max(0,high)+1,False)


def finish(connection,table,evidence,maximum,*,verified):
    value,exhausted=allocation(evidence,maximum)
    M.check(table in M.AUTO,'operations_sequence_table')
    sequence='em_content.'+table+'_id_seq'
    M.check(_rows(connection,'SELECT pg_get_serial_sequence(%s,%s)',('em_content.'+table,'id'))==[(sequence,)],'operations_identity_sequence_changed')
    shape=_rows(connection,'SELECT seqtypid::regtype::text,seqstart,seqincrement,seqmax,seqmin,seqcache,seqcycle FROM pg_sequence WHERE seqrelid=%s::regclass',(sequence,))
    M.check(shape==[('bigint',1,1,M.INT64_MAX,1,1,False)],'operations_identity_sequence_shape_changed')
    last,called=_rows(connection,'SELECT last_value,is_called FROM '+sequence)[0]
    M.check(type(last) is int and 1<=last<=M.INT64_MAX and type(called) is bool,'operations_sequence_state_invalid')
    exhausted=exhausted or (last==M.INT64_MAX and called)
    if verified:
        M.check((last==M.INT64_MAX and called) if exhausted else last+int(called)>=value,'operations_identity_sequence_regressed')
    else:
        if exhausted:
            # RESTART creates a transactional sequence state. Consuming its last
            # value keeps NO CYCLE exhausted, including when the high row was deleted.
            connection.execute('ALTER SEQUENCE '+sequence+' RESTART WITH '+str(M.INT64_MAX))
            M.check(_rows(connection,'SELECT nextval(%s::regclass)',(sequence,))==[(M.INT64_MAX,)],'operations_sequence_exhaustion_failed')
        else:
            value=max(value,last+int(called))
            connection.execute('ALTER SEQUENCE '+sequence+' RESTART WITH '+str(value))
        after=_rows(connection,'SELECT last_value,is_called FROM '+sequence)
        M.check(after==[(M.INT64_MAX,True)] if exhausted else after==[(value,False)],'operations_sequence_restore_failed')
    return dict(source_present=evidence['present'],source_value=evidence['value'],exhausted=exhausted,
        next_value=None if exhausted else max(value,last+int(called)))
