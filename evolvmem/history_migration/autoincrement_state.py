"""Resolve AUTO allocation only from source sequence or a proven empty scan.

The zero result for an absent, empty AUTO table is an allocation floor, not a
fabricated sqlite_sequence record. Callers retain the original evidence and
must pass counts from a fully exhausted, source-bound plan. Nonempty absence
never obtains a highwater from max(id).
"""
from .history_core import mapping as M

_FIELDS={'present','value','storage_type','autoincrement_ddl_verified','allocation_history_verified'}


def ready(evidence,row_count,maximum):
    if not (type(evidence) is dict and set(evidence)==_FIELDS
            and evidence['autoincrement_ddl_verified'] is True
            and evidence['allocation_history_verified'] is False
            and type(row_count) is int and row_count>=0
            and type(maximum) is int and 0<=maximum<=M.INT64_MAX):return False
    if evidence['present'] is False:
        return row_count==0 and maximum==0 and evidence['value'] is None and evidence['storage_type'] is None
    return (evidence['present'] is True and evidence['storage_type']=='integer'
        and type(evidence['value']) is int and maximum<=evidence['value']<M.INT64_MAX)


def resolved_highwater(evidence,row_count,maximum):
    M.check(ready(evidence,row_count,maximum),'source_highwaters_unresolved')
    return evidence['value'] if evidence['present'] else 0


def legacy_ready(sequence,maxima):
    """Recognize old conservative plans without rewriting their raw evidence."""
    return all(e['present'] is True and e['storage_type']=='integer' and type(e['value']) is int
        and maxima[t]<=e['value']<M.INT64_MAX for t,e in sequence.items())
