"""Static contracts for the additive SQL013 source-pagination migration."""
from pathlib import Path
import hashlib
import re

ROOT = Path(__file__).resolve().parents[2]
import os
SQL = Path(os.environ.get('HISTORY_SQL013', ROOT / 'evolvmem/pg_business_core/sql/013_history_sources_paged.sql'))
OLD_PROVISIONER_MD5 = 'a847bbc4e78827614f7f6ee797c46175'
INPUTS_MD5 = 'da05bf9463893fe65b019b13cb7a0636'
G1 = " EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_item_inputs_first_page(uuid,uuid,bigint[]) TO %I',p_role);"
G2 = " EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_item_sources_page(uuid,uuid,bigint,bigint) TO %I',p_role);"


def sql():
    return SQL.read_text(encoding='utf-8-sig')


def provisioner():
    text = sql()
    match = re.search(r'CREATE OR REPLACE FUNCTION em_security\.provision_history_reader\(p_role name\) RETURNS void\n(.*?)AS \$fn\$(.*?)\$fn\$;', text, re.S)
    assert match is not None
    return text, match.group(1), match.group(2)


def test_old_input_function_and_nested_proof_guard_are_not_redefined():
    text = sql()
    assert not re.search(r'CREATE (?:OR REPLACE )?FUNCTION em_content\.history_item_inputs\(', text)
    assert not re.search(r'CREATE (?:OR REPLACE )?FUNCTION em_content\.history_checked\(', text)
    assert 'jsonb_array_length(source->\'event_proofs\')>64' in text
    assert "md5(p.prosrc)='%s'" % INPUTS_MD5 in text
    assert 'history_item_inputs_preimage_changed' in text


def test_first_page_is_64_bounded_and_more_is_a_real_same_snapshot_sentinel():
    text = sql()
    first = text.split('CREATE FUNCTION em_content.history_item_inputs_first_page', 1)[1].split('CREATE FUNCTION em_content.history_item_sources_page', 1)[0]
    assert 'ORDER BY s.id LIMIT 64' in first
    assert 'source_after' in first and 'source_more' in first
    assert 's.id > COALESCE((SELECT max(s2.id)' in first
    assert 'source_count' not in first
    assert "'sources',COALESCE" in first


def test_page_is_64_returned_from_a_65_row_sentinel_and_strictly_advances():
    text = sql()
    page = text.split('CREATE FUNCTION em_content.history_item_sources_page', 1)[1].split('REVOKE ALL ON FUNCTION em_content.history_item_sources_page', 1)[0]
    assert 'ORDER BY s.id LIMIT 65' in page
    assert 'counted.n>64' in page and 'c.rn<=64' in page
    assert 's.id>p_after' in page
    assert 'p_after IS NULL OR p_after<1' in page
    assert 's.id=p_after' in page
    assert 'history_member(p_library,p_project,p_item)' in page
    assert 'require_history_scope(p_library,p_project)' in page


def test_new_functions_are_definer_fixed_path_and_public_revoked():
    text = sql()
    for signature in ('history_item_inputs_first_page(uuid,uuid,bigint[])',
                      'history_item_sources_page(uuid,uuid,bigint,bigint)',
                      'history_source_payload(uuid,bigint,bigint)'):
        assert 'FUNCTION em_content.' + signature in text
        assert "REVOKE ALL ON FUNCTION em_content." + signature + ' FROM PUBLIC;' in text
    assert text.count('SECURITY DEFINER') >= 3
    assert 'SET search_path=pg_catalog,em_security,em_content' in text


def test_provisioner_preserves_012_body_and_adds_only_two_new_grants():
    text, attributes, body = provisioner()
    assert attributes == 'LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,em_security '
    assert body.count(G1) == 1 and body.count(G2) == 1
    restored = body.replace(G1 + '\n', '').replace(G2 + '\n', '')
    assert hashlib.md5(restored.encode()).hexdigest() == OLD_PROVISIONER_MD5
    assert "md5(p.prosrc)='%s'" % OLD_PROVISIONER_MD5 in text
    assert "RAISE EXCEPTION 'history_reader_provisioning_preimage_changed'" in text
    assert 'history_reader_provisioning_preimage_changed' in text
    assert 'REVOKE ALL ON TABLE em_content.%I FROM %I' in body
    assert 'history_source_payload' not in body


def test_explicit_acl_copy_never_broadens_tables_or_public():
    text = sql()
    grants = text.split('DO $grants$', 1)[1].split('CREATE OR REPLACE FUNCTION em_security.provision_history_reader', 1)[0]
    assert "to_regprocedure('em_content.history_item_inputs(uuid,uuid,bigint[])')" in grants
    assert "a.grantee<>p.proowner" in grants
    assert "COALESCE(p.proacl,acldefault('f',p.proowner))" in grants
    assert "a.grantee=0 OR a.privilege_type<>'EXECUTE'" in grants
    assert 'history_item_inputs_first_page(uuid,uuid,bigint[])' in grants
    assert 'history_item_sources_page(uuid,uuid,bigint,bigint)' in grants
    assert not re.search(r'GRANT\s+[^;]*ON\s+TABLE', text, re.I)
    assert 'from public' in text.lower()


# --- Independent counterexamples for the three defects in the original SRC. ---
PAYLOAD_PREDICATES = [
    "p.library_id=s.library_id AND p.source_id=s.id AND p.item_id=i.id AND p.state='verified'",
    "p.item_revision=i.revision",
    "p.item_content_hash=l.content_hash",
    "p.evidence_revision=e.revision AND p.experience_version=e.experience_version",
    "p.evidence_note_hash=encode(sha256(convert_to(e.note,'UTF8')),'hex')",
    "p.task_id=e.task_id AND p.source_ref=s.source_ref AND p.extraction_version=s.extraction_version",
    "p.archive_id=s.archive_id AND p.archive_payload_sha256=a.payload_sha256 AND a.state='available'",
    "p.event_ref=s.source_ref",
    "((s.source_kind='tool_result' AND p.role='tool') OR(s.source_kind='user_confirmation' AND p.role='user'))",
]


def payload_fn():
    return sql().split('CREATE FUNCTION em_content.history_source_payload', 1)[1].split(
        'REVOKE ALL ON FUNCTION em_content.history_source_payload', 1)[0]


def acl_block():
    return sql().split('DO $grants$', 1)[1].split(
        'CREATE OR REPLACE FUNCTION em_security.provision_history_reader', 1)[0]


def test_counterexample_payload_joins_real_context_items_revision_and_keeps_005_predicates():
    fn = payload_fn()
    assert 'p.item_revision=s.item_revision' not in fn
    assert re.search(r'JOIN em_content\.context_items i ON i\.library_id=s\.library_id AND i\.id=s\.item_id', fn)
    assert re.search(r'JOIN em_content\.context_evidence e ON e\.library_id=p\.library_id AND e\.id=p\.evidence_id\s+AND e\.source_id=p\.source_id AND e\.item_id=p\.item_id', fn)
    for pred in PAYLOAD_PREDICATES:
        assert pred in fn, pred
    assert 'SELECT s.*' in fn
    for alias in ('AS archive', 'AS exclusion', 'AS event_proofs'):
        assert alias in fn
    assert 'ORDER BY p.evidence_id LIMIT 65' in fn


def test_counterexample_acl_clears_each_new_function_own_acl_and_helper_stays_owner_only():
    grants = acl_block()
    cleared = re.search(r'FOREACH f IN ARRAY ARRAY\[(.*?)\] LOOP', grants, re.S)
    assert cleared is not None
    for sig in ('history_source_payload(uuid,bigint,bigint)',
                'history_item_inputs_first_page(uuid,uuid,bigint[])',
                'history_item_sources_page(uuid,uuid,bigint,bigint)'):
        assert sig in cleared.group(1)
    assert grants.count('to_regprocedure(f)') == 1
    assert 'aclexplode(COALESCE(p.proacl,acldefault(\'f\',p.proowner)))' in grants
    assert 'a.grantee<>p.proowner' in grants
    assert 'a.grantee=0' in grants and 'REVOKE ALL ON FUNCTION %s FROM PUBLIC' in grants
    assert 'history_source_payload_acl_not_owner_only' in grants
    assert "to_regprocedure('em_content.history_source_payload(uuid,bigint,bigint)')" in grants
    granted = re.findall(r"EXECUTE format\('GRANT EXECUTE ON FUNCTION %s TO %I',f,recipient\.rolname\)", grants)
    assert len(granted) == 1
    entries = re.findall(r"FOREACH f IN ARRAY ARRAY\[(.*?)\] LOOP", grants, re.S)[1]
    assert 'first_page' in entries and 'history_item_sources_page' in entries
    assert 'history_source_payload' not in entries
    text, _, body = provisioner()
    assert 'history_source_payload' not in body


def test_counterexample_page_checks_cumulative_budget_and_nested_proofs_before_transport():
    page = sql().split('CREATE FUNCTION em_content.history_item_sources_page', 1)[1].split(
        'REVOKE ALL ON FUNCTION em_content.history_item_sources_page', 1)[0]
    assert 'RETURN QUERY' not in page
    for token in ('total_bytes bigint:=0', 'em_content.history_checked(rec.payload)',
                  "jsonb_array_length(rec.payload->'event_proofs')>64",
                  'octet_length(rec.payload::text)', '>8388608',
                  "RAISE EXCEPTION 'history_dependency_limit'", 'RETURN NEXT'):
        assert token in page, token
    assert (page.index('history_checked(rec.payload)') <
            page.index('total_bytes:=total_bytes+octet_length') <
            page.index('>8388608') < page.index('RETURN NEXT'))
    assert 'ORDER BY s.id LIMIT 65' in page and 'c.rn<=64' in page and 'counted.n>64' in page
    assert 'require_history_scope(p_library,p_project)' in page
    assert 'history_member(p_library,p_project,p_item)' in page
    assert 'p_after IS NULL OR p_after<1' in page and 's.id=p_after' in page


def test_item_authorization_precedes_any_cursor_existence_lookup():
    # Otherwise an allowed project reader can distinguish external cursor IDs
    # before the opaque item refusal and infer source existence.
    page = sql().split('CREATE FUNCTION em_content.history_item_sources_page(', 1)[1].split('END; $fn$;', 1)[0]
    assert page.index('IF NOT em_content.history_member') < page.index('NOT EXISTS(SELECT 1 FROM em_content.context_sources')
