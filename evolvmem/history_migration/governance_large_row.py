"""Fixed measured audit-row profile; no source/manifest/value normalization."""
TABLE='capability_provenance_repairs'
SQLITE_BYTES=8*1024*1024
PAGE_BYTES=16*1024*1024


def check_ledger(c,kind):
    """Reject old/tampered metadata bounds; never ALTER existing schemas."""
    from .history_core.mapping import check
    check(kind in ('governance','catchup'),'history_ledger_kind')
    table='history_governance_chunks' if kind=='governance' else 'history_catchup_pages'
    actual=c.execute("""SELECT k.convalidated,NOT k.condeferrable,
      regexp_replace(pg_get_expr(k.conbin,k.conrelid),'[[:space:]()]','','g')
      FROM pg_constraint k JOIN pg_class t ON t.oid=k.conrelid
      JOIN pg_namespace n ON n.oid=t.relnamespace
      JOIN pg_attribute a ON a.attrelid=t.oid AND a.attname='input_bytes'
      WHERE n.nspname='em_content' AND t.relname=%s AND k.contype='c'
        AND a.attnum=ANY(k.conkey) ORDER BY k.conname""",(table,)).fetchall()
    check(actual==[(True,True,'input_bytes>=1ANDinput_bytes<=16777216')],
          kind+'_ledger_16m_schema_required')
