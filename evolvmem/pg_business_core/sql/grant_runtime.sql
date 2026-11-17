\set ON_ERROR_STOP on
-- Trusted provisioner only, after verifying the runtime binding and pilot scope.
-- RLS still decides read/write/admin; no DELETE, DDL, TRIGGER or REFERENCES grant.
BEGIN;
GRANT SELECT, INSERT, UPDATE ON em_content.memories, em_content.context_items,
 em_content.context_layers, em_content.legacy_memory_migrations,
 em_content.context_sources, em_content.context_project_registry,
 em_content.context_project_aliases, em_content.context_project_workspace_bindings,
 em_content.context_project_resolutions, em_content.lan_write_requests,
 em_content.vector_outbox TO :"runtime_role";
GRANT USAGE ON SEQUENCE em_content.memories_id_seq, em_content.context_items_id_seq,
 em_content.context_sources_id_seq, em_content.vector_outbox_id_seq TO :"runtime_role";
COMMIT;
