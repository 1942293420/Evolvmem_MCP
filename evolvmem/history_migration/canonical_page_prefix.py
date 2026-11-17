"""Exact whole-row canonical prefix for existing fixed scanner mappings.

No IO, SQL, normalization or caller-provided byte budget. Final source_rowid
participates before export. The caller must reopen an unexported page with the
returned row limit so the original frozen reader owns all hashes and cursors.
"""


def canonical_prefix_rows(mapping, table, rows, rowids):
    mapping.check(len(rows) == len(rowids), 'source_range_changed')
    used = 2  # JSON array brackets
    selected = 0
    for row, (rowid,) in zip(rows, rowids):
        final = mapping.map_row(table, dict(row, source_rowid=rowid))
        cost = len(mapping.canonical(final)) + int(selected > 0)
        if used + cost > mapping.MAX_BYTES:
            break
        used += cost
        selected += 1
    return selected
