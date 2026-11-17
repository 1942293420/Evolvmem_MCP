"""Closed authorization contract for readers provisioned after SQL012."""
from pathlib import Path
import hashlib
import re
import pytest

NAME='em_security.provision_history_reader'
NEW='em_content.history_identity_family_qualified(uuid,uuid,jsonb)'
OLD_BODY_MD5='78ff1f476bdadc07b49e10f2e672cd46'
GRANT=" EXECUTE format('GRANT EXECUTE ON FUNCTION "+NEW+" TO %I',p_role);"


def source():
    root=Path(__file__).resolve().parents[2]
    standard=root/'evolvmem/pg_business_core/sql/012_history_identity_family_qualified.sql'
    candidate=root/'012_history_identity_family_qualified.sql'
    return (standard if standard.exists() else candidate).read_text(encoding='utf-8-sig')


def replacement():
    text=source()
    match=re.search(r'CREATE OR REPLACE FUNCTION em_security\.provision_history_reader\(p_role name\) RETURNS void\n(.*?)AS \$fn\$(.*?)\$fn\$;',text,re.S)
    assert match is not None,'SQL012 must wire future readers through the existing guarded provisioner'
    return text,match.group(1),match.group(2)


def test_fresh_reader_receives_explicit_execute_on_the_python_family_entrypoint():
    _,_,body=replacement()
    # A future role was absent when the one-time old-ACL copy ran. Its grants
    # therefore come exclusively from this provisioner, not the migration copy.
    functions=re.findall(r"EXECUTE format\('GRANT EXECUTE ON FUNCTION ([^']+) TO %I',p_role\);",body)
    assert NEW in functions
    assert body.count(GRANT)==1


def test_original_provisioner_body_is_preserved_byte_for_byte_except_the_one_grant():
    _,_,body=replacement()
    restored=body.replace(GRANT+'\n','')
    assert hashlib.md5(restored.encode()).hexdigest()==OLD_BODY_MD5


def test_provisioner_stays_invoker_with_the_original_fixed_search_path_and_acl():
    text,attributes,_=replacement()
    assert attributes=='LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,em_security '
    assert not re.search(r'(?:GRANT|REVOKE|ALTER).*ON FUNCTION em_security\.provision_history_reader',text,re.I)
    assert 'ALTER FUNCTION em_security.provision_history_reader' not in text


def test_exact_original_provisioner_preimage_is_required_before_any_create():
    text,_,_=replacement()
    guard=text.split('CREATE FUNCTION em_content.history_identity_family_qualified',1)[0]
    assert "to_regprocedure('em_security.provision_history_reader(name)')" in guard
    assert "md5(p.prosrc)='"+OLD_BODY_MD5+"'" in guard
    assert 'NOT p.prosecdef' in guard and "p.provolatile='v'" in guard
    assert "p.proconfig=ARRAY['search_path=pg_catalog, em_security']::text[]" in guard
    assert "RAISE EXCEPTION 'history_reader_provisioning_preimage_changed'" in guard


def test_role_and_binding_safety_checks_precede_new_execute_grant():
    _,_,body=replacement()
    gate=body.index('IF NOT em_security._runtime_role_safe(p_role) OR NOT EXISTS(')
    refusal=body.index("RAISE EXCEPTION 'unsafe_history_reader'")
    revoke=body.index("REVOKE ALL ON TABLE em_content.%I FROM %I")
    assert gate<refusal<revoke<body.index(GRANT)
