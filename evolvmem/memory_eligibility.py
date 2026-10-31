"""History remains searchable; method claims require independently bound proof."""

def eligible(store, item_id, *, read=None, rows=None):
    from evolvmem.unit_derivations import currently_backed
    if not currently_backed(store, item_id, read=read):
        return False
    row = rows.get(item_id) if rows is not None else store._connection().execute(
        "SELECT i.content_type,i.status,i.success_count,i.experience_payload,l.category,l.payload AS learning_payload "
        "FROM context_items i LEFT JOIN learning_memories l ON l.item_id=i.id WHERE i.id=?", (item_id,)).fetchone()
    return _eligible_row(store, item_id, row)


def _eligible_row(store, item_id, row):
    if row is None:
        return False
    from evolvmem.knowledge_provenance import unconfirmed_rule
    if unconfirmed_rule(store, item_id, row['category'], row['learning_payload']):
        return False
    if row['content_type'] == 'project_summary':
        from evolvmem.rollup_provenance import current
        return current(store, item_id)
    if row['content_type'] != 'experience' and row['category'] != 'experience':
        return True
    # The experience service alone validates proof and derives these counters.
    return row['status'] == 'active' and row['success_count'] > 0 and bool(row['experience_payload'])


def eligible_ids(store, item_ids, *, read=None, preserve_text_decoding=False):
    """Same eligibility verdicts with shared inputs for this one read only."""
    from evolvmem.unit_derivations import BackingRead
    ids = list(set(item_ids))
    if preserve_text_decoding:
        # This health path retains the original TEXT decode boundaries. Only
        # backing-qualified items had their eligibility payloads read before;
        # sources themselves retain their original lazy SELECT * short circuit.
        from evolvmem.unit_derivations import currently_backed
        read = read if read is not None else BackingRead(store, ids, preserve_text_decoding=True)
        backed = [i for i in ids if currently_backed(store, i, read=read)]
        allowed = set()
        for offset in range(0, len(backed), 500):
            batch = backed[offset:offset+500]
            placeholders = ','.join('?' * len(batch))
            for row in store._connection().execute(
                    'SELECT i.id,i.content_type,i.status,i.success_count,i.experience_payload,'
                    'l.category,l.payload AS learning_payload FROM context_items i '
                    'LEFT JOIN learning_memories l ON l.item_id=i.id '
                    f'WHERE i.id IN ({placeholders})', batch):
                if _eligible_row(store, row['id'], row):
                    allowed.add(row['id'])
        return allowed
    rows = {}
    for offset in range(0, len(ids), 500):
        batch = ids[offset:offset+500]
        placeholders = ','.join('?' * len(batch))
        rows.update((r['id'], r) for r in store._connection().execute(
            'SELECT i.id,i.content_type,i.status,i.success_count,'
            "(i.experience_payload IS NOT NULL AND i.experience_payload!='') AS experience_payload,"
            'l.category,CASE WHEN l.category IN (\'habit\',\'project_convention\',\'task_requirement\') '
            'THEN l.payload END AS learning_payload FROM context_items i '
            'LEFT JOIN learning_memories l ON l.item_id=i.id '
            f'WHERE i.id IN ({placeholders})', batch))
    read = read if read is not None else BackingRead(store, ids)
    return {i for i in ids if eligible(store, i, read=read, rows=rows)}
