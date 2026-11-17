"""Fixed SQL only. All identifiers are author constants, all values parameters."""
def _rows(body):
    return 'SELECT to_jsonb(q) FROM (' + body + ') q'


BASIS_FROM = ''' FROM em_content.organization_context_basis b
 LEFT JOIN em_content.organization_units p ON p.library_id=b.library_id AND p.task_id=b.prior_task_id AND p.digest=b.prior_digest
 LEFT JOIN em_content.organization_tasks pt ON pt.library_id=b.library_id AND pt.id=b.prior_task_id
 LEFT JOIN em_content.session_archives sa ON sa.library_id=b.library_id AND sa.id=b.prior_archive_id
 LEFT JOIN em_content.knowledge_cleaning_reviews cr ON cr.library_id=b.library_id AND cr.source_key=b.prior_source_key
 JOIN em_content.organization_units u ON u.library_id=b.library_id AND u.task_id=b.unit_task_id AND u.digest=b.unit_digest
'''
BASIS_STALE = '''(p.task_id IS NULL OR pt.id IS NULL OR pt.status='superseded'
 OR sa.id IS NULL OR sa.state<>'available'
 OR p.project<>b.project OR p.decision NOT IN ('auto','manual') OR p.disposition NOT IN ('keep','history_only')
 OR p.revision<>b.prior_revision OR pt.source_revision<>b.prior_source_revision
 OR sa.payload_sha256<>b.prior_payload_sha256 OR COALESCE(cr.revision,0)<>b.prior_cleaning_revision)'''

SQL = {
 'backing':_rows('''SELECT d.item_id,d.unit_task_id,d.unit_digest,d.kind,d.project AS derivation_project,
 t.id AS task_exists,i.id AS item_exists,t.source_key,t.source_revision,t.status,
 u.ordinal,u.decision,u.disposition,u.project,u.projects,u.knowledge_scope,u.revision,
 i.project AS item_project,i.legacy_projects_text AS item_projects,i.scope AS item_scope
 FROM em_content.unit_derivations d
 LEFT JOIN em_content.organization_tasks t ON t.library_id=d.library_id AND t.id=d.unit_task_id
 LEFT JOIN em_content.context_items i ON i.library_id=d.library_id AND i.id=d.item_id
 LEFT JOIN em_content.organization_units u ON u.library_id=d.library_id AND u.task_id=d.unit_task_id AND u.digest=d.unit_digest
 WHERE d.library_id=%s AND d.item_id=%s ORDER BY d.unit_task_id,d.unit_digest,u.ordinal LIMIT 513'''),
 'conversation':_rows('''SELECT source_rowid,source_key,members FROM em_content.organization_conversations
 WHERE library_id=%s AND source_key=%s ORDER BY source_rowid LIMIT 2'''),
 'sources':_rows('''SELECT * FROM em_content.context_sources WHERE library_id=%s AND item_id=%s ORDER BY id LIMIT 513'''),
 'eligibility':_rows('''SELECT i.content_type,i.status,i.success_count,i.legacy_experience_payload_text AS experience_payload,
 l.category,l.payload AS learning_payload,l.revision AS learning_revision
 FROM em_content.context_items i LEFT JOIN em_content.learning_memories l ON l.library_id=i.library_id AND l.item_id=i.id
 WHERE i.library_id=%s AND i.id=%s LIMIT 2'''),
 'human':_rows('''SELECT * FROM em_content.context_project_resolutions
 WHERE library_id=%s AND item_id=%s AND decision_source='human' AND review_state='accepted' ORDER BY item_id LIMIT 2'''),
 'manual':_rows('''SELECT * FROM em_content.context_sources
 WHERE library_id=%s AND item_id=%s AND source_kind='manual' ORDER BY id LIMIT 513'''),
 'basis':_rows('''SELECT to_jsonb(b) AS basis,u.ordinal,u.decision,
 p.ordinal AS prior_ordinal,p.project AS prior_project,p.decision AS prior_decision,
 p.disposition AS prior_disposition,p.revision AS prior_revision,pt.status AS prior_status,
 pt.source_revision,sa.state AS archive_state,sa.payload_sha256,cr.revision AS cleaning_revision,
 (b.state='active' AND u.decision='auto' AND '''+BASIS_STALE+''') AS stale'''+BASIS_FROM+'''
 WHERE b.library_id=%s AND EXISTS(SELECT 1 FROM em_content.unit_derivations d
 WHERE d.library_id=b.library_id AND d.item_id=%s AND d.unit_task_id=b.unit_task_id AND d.unit_digest=b.unit_digest)
 ORDER BY b.unit_task_id,b.unit_digest,u.ordinal,p.ordinal LIMIT 513'''),
 'item':_rows('SELECT * FROM em_content.context_items WHERE library_id=%s AND id=%s LIMIT 2'),
 'layers':_rows('''SELECT * FROM em_content.context_layers WHERE library_id=%s AND item_id=%s ORDER BY layer LIMIT 4'''),
 'family':_rows('''SELECT id FROM em_content.context_items WHERE library_id=%s AND identity_key=%s
 AND project=%s AND scope=%s ORDER BY id LIMIT 513'''),
 'registry':_rows('''SELECT project,project_uuid::text,status FROM em_content.context_project_registry
 WHERE library_id=%s AND project=%s LIMIT 2'''),
}

# No FROM is supplied by a caller. These exact predicates are the original
# organization_context candidate test with same-library joins and fixed bounds.
RECONCILE = _rows('''SELECT b.unit_task_id,b.unit_digest,b.prior_task_id,b.prior_source_key,
 b.prior_start_line,b.prior_end_line'''+BASIS_FROM+'''
 WHERE b.library_id=%s AND b.state='active' AND u.decision='auto' AND '''+BASIS_STALE+'''
 ORDER BY b.unit_task_id,b.unit_digest LIMIT %s''')

