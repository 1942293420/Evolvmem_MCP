"""Fixed historical read functions only; never runtime access to quarantine tables."""
READ_CONTEXT = '''SELECT schema_version,mode,policy_epoch,route_epoch,readiness_revision,mapping_revision,completion_hash FROM em_security.history_recall_context(%s,%s)'''
# The old two-column entrypoint is retained for installed readers. New code
# uses the first-page metadata call so a source list is never truncated by its
# 64-row SQL guard.
BATCH = 'SELECT id,payload FROM em_content.history_item_inputs(%s,%s,%s::bigint[])'
FIRST_PAGE = ('SELECT id,payload,source_after,source_more '
    'FROM em_content.history_item_inputs_first_page(%s,%s,%s::bigint[])')
SOURCE_PAGE = ('SELECT source_id,payload,has_more '
    'FROM em_content.history_item_sources_page(%s,%s,%s,%s)')
FAMILY = 'SELECT identity_key,project,scope,id,qualification FROM em_content.history_identity_family_qualified(%s,%s,%s::jsonb)'
LEXICAL = 'SELECT item_id,layer,score,content_type,project,status FROM em_content.history_lexical_candidates(%s,%s,%s,%s::text[],%s,%s,%s,%s,%s,%s::jsonb)'
KNOWLEDGE = 'SELECT payload FROM em_content.history_knowledge_inputs(%s,%s,%s::bigint[],%s)'
LEGACY = 'SELECT item_id,payload FROM em_content.history_legacy_projection(%s,%s,%s::bigint[])'
LAYER = 'SELECT payload FROM em_content.history_layer_read(%s,%s,%s,%s)'
MODE = "SELECT to_regclass('em_security.history_readiness') IS NOT NULL"


def historical_mode(session):
    """A DB-owned marker chooses the gate; presence does not grant readiness."""
    from evolvmem.library_foundation import FoundationError
    rows=session.execute(MODE)
    if len(rows)!=1 or len(rows[0])!=1 or type(rows[0][0]) is not bool:
        raise FoundationError('invalid_recall_mode')
    return rows[0][0]
