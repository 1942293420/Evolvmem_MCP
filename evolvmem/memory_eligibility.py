"""History remains searchable; method claims require independently bound proof."""

def eligible(store, item_id):
    from evolvmem.unit_derivations import currently_backed
    if not currently_backed(store, item_id):
        return False
    row = store._connection().execute(
        "SELECT i.content_type,i.status,i.success_count,i.experience_payload,l.category "
        "FROM context_items i LEFT JOIN learning_memories l ON l.item_id=i.id WHERE i.id=?", (item_id,)).fetchone()
    if row is None:
        return False
    if row['content_type'] != 'experience' and row['category'] != 'experience':
        return True
    # The experience service alone validates proof and derives these counters.
    return row['status'] == 'active' and row['success_count'] > 0 and bool(row['experience_payload'])
