"""Author-fixed, project-filtered native qualification reads; no caller SQL."""

READ_CONTEXT = '''SELECT schema_version,mode,policy_epoch,route_epoch,readiness_revision
 FROM em_security.recall_context(%s,%s)'''

# Each one-to-many branch retains a sentinel row: 65 rows is a refusal, never
# silent truncation. A whole envelope is bounded before crossing the driver.
BATCH = '''WITH candidates AS (
 SELECT i.id,jsonb_build_object(
  'item',to_jsonb(i),
  'item_l1_hash',(SELECT l.content_hash FROM em_content.context_layers l WHERE l.library_id=i.library_id
    AND l.project_id IS NOT DISTINCT FROM i.project_id AND l.item_id=i.id AND l.layer='l1'),
  'learning',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (
    SELECT l.* FROM em_content.native_learning_memories l
    WHERE l.library_id=i.library_id AND l.project_id IS NOT DISTINCT FROM i.project_id AND l.item_id=i.id
    ORDER BY l.item_id LIMIT 2) x),'[]'::jsonb),
  'decision',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (
    SELECT h.* FROM em_content.native_human_decisions h
    WHERE h.library_id=i.library_id AND h.project_id IS NOT DISTINCT FROM i.project_id AND h.item_id=i.id
    ORDER BY h.item_id LIMIT 2) x),'[]'::jsonb),
  'resolution',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (
    SELECT r.* FROM em_content.context_project_resolutions r
    WHERE r.library_id=i.library_id AND r.project_id IS NOT DISTINCT FROM i.project_id AND r.item_id=i.id
    ORDER BY r.item_id LIMIT 2) x),'[]'::jsonb),
  'registry',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (
    SELECT p.* FROM em_content.context_project_registry p
    WHERE p.library_id=i.library_id AND p.project_id IS NOT DISTINCT FROM i.project_id AND p.project=i.project
    ORDER BY p.project LIMIT 2) x),'[]'::jsonb),
  'sources',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM (
    SELECT s.*,to_jsonb(n) AS qualification FROM em_content.context_sources s
    LEFT JOIN em_content.native_source_states n ON n.library_id=s.library_id AND n.source_id=s.id
      AND n.project_id IS NOT DISTINCT FROM s.project_id
    WHERE s.library_id=i.library_id AND s.project_id IS NOT DISTINCT FROM i.project_id AND s.item_id=i.id
    ORDER BY s.id LIMIT 65) x),'[]'::jsonb),
  'derivations',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.unit_task_id,x.unit_digest) FROM (
    SELECT d.*,to_jsonb(b) AS basis_source,to_jsonb(bs) AS basis_descriptor FROM em_content.native_unit_derivations d
    LEFT JOIN em_content.native_source_states b ON b.library_id=d.library_id AND b.source_id=d.basis_source_id
      AND b.project_id IS NOT DISTINCT FROM d.project_id
    LEFT JOIN em_content.context_sources bs ON bs.library_id=d.library_id AND bs.id=d.basis_source_id
      AND bs.project_id IS NOT DISTINCT FROM d.project_id
    WHERE d.library_id=i.library_id AND d.project_id IS NOT DISTINCT FROM i.project_id AND d.item_id=i.id
    ORDER BY d.unit_task_id,d.unit_digest LIMIT 65) x),'[]'::jsonb),
  'evidence',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.evidence_id) FROM (
    SELECT e.* FROM em_content.native_source_evidence e
    WHERE e.library_id=i.library_id AND e.project_id IS NOT DISTINCT FROM i.project_id AND e.item_id=i.id
    ORDER BY e.evidence_id LIMIT 65) x),'[]'::jsonb),
  'rollup',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (
    SELECT r.* FROM em_content.native_project_rollups r
    WHERE r.library_id=i.library_id AND r.project_id IS NOT DISTINCT FROM i.project_id AND r.item_id=i.id
    ORDER BY r.item_id LIMIT 2) x),'[]'::jsonb)
 ) AS payload FROM em_content.context_items i
 WHERE i.library_id=%s AND i.project_id IS NOT DISTINCT FROM %s::uuid AND i.id=ANY(%s::bigint[])
 ORDER BY i.id LIMIT 241
), bounded AS (
 SELECT id,payload,octet_length(payload::text) AS bytes FROM candidates
) SELECT id,CASE WHEN bytes<=65536 AND sum(bytes) OVER()<=8388608 THEN payload ELSE NULL END
 FROM bounded ORDER BY id'''

SUMMARY_CONTENT = '''WITH rows AS (
 SELECT i.id,i.project,i.content_type,l.content,l.content_hash
 FROM em_content.context_items i JOIN em_content.context_layers l
 ON l.library_id=i.library_id AND l.item_id=i.id AND l.project_id IS NOT DISTINCT FROM i.project_id AND l.layer='l1'
 WHERE i.library_id=%s AND i.project_id IS NOT DISTINCT FROM %s::uuid AND i.id=ANY(%s::bigint[])
 ORDER BY i.id LIMIT 241
) SELECT id,project,content_type,
 CASE WHEN octet_length(content)<=65536 AND sum(octet_length(content)) OVER()<=8388608 THEN content ELSE NULL END,
 content_hash FROM rows ORDER BY id'''
