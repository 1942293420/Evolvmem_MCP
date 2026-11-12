"""Static SQL contract lint only: this does NOT execute PostgreSQL or prove RLS."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2] / "evolvmem" / "library_foundation"


def without_comments(sql):
    return re.sub(r"--[^\n]*|/\*.*?\*/", "", sql, flags=re.S)


def security_findings(sql):
    """Build table/policy sets to detect uncovered tables and unsafe helpers."""
    sql = without_comments(sql)
    tables = set(re.findall(r"CREATE\s+TABLE\s+([a-z_]+\.[a-z_]+)\s*\(", sql, re.I))
    enabled = set(re.findall(r"ALTER\s+TABLE\s+([a-z_]+\.[a-z_]+)\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY", sql, re.I))
    forced = set(re.findall(r"ALTER\s+TABLE\s+([a-z_]+\.[a-z_]+)\s+FORCE\s+ROW\s+LEVEL\s+SECURITY", sql, re.I))
    protected = set(re.findall(r"CREATE\s+POLICY\s+[a-z_]+\s+ON\s+([a-z_]+\.[a-z_]+)", sql, re.I))
    findings = []
    for table in sorted(tables):
        for label, covered in (("enable", enabled), ("force", forced), ("policy", protected)):
            if table not in covered:
                findings.append((table, label))
    if re.search(r"\b(current_setting|set_config)\s*\(", sql, re.I):
        findings.append(("identity", "GUC-dependent authorization"))
    if re.search(r"\b(CREATE\s+(DATABASE|ROLE|USER)|PASSWORD)\b", sql, re.I):
        findings.append(("provisioning", "unrequested identity/database creation"))
    for fn in re.finditer(r"CREATE\s+FUNCTION\s+([\w.]+)\b(.*?)\$function\$\s*;", sql, re.I | re.S):
        if re.search(r"\bSECURITY\s+DEFINER\b", fn[2], re.I) and not re.search(
            r"SET\s+search_path\s*=\s*pg_catalog\s*,\s*em_security", fn[2], re.I
        ):
            findings.append((fn[1], "unfixed definer search_path"))
    return findings


class DdlStaticContractTests(unittest.TestCase):
    def read(self, name):
        path = ROOT / "sql" / name
        self.assertTrue(path.is_file(), f"Missing candidate migration: {name}")
        return path.read_text(encoding="utf-8")

    def test_linter_detects_missing_forced_rls_and_guc_authorization(self):
        sql = """CREATE TABLE em_content.items (id uuid); ALTER TABLE em_content.items ENABLE ROW LEVEL SECURITY;
        CREATE POLICY p ON em_content.items USING (current_setting('app.principal') = id::text);"""
        self.assertIn(("em_content.items", "force"), security_findings(sql))
        self.assertIn(("identity", "GUC-dependent authorization"), security_findings(sql))

    def test_control_migration_covers_every_infrastructure_table(self):
        sql = self.read("001_control.sql")
        self.assertEqual([], security_findings(sql))
        tables = set(re.findall(r"CREATE TABLE em_control\.([a-z_]+)", without_comments(sql)))
        self.assertTrue({"schema_migrations", "principals", "devices", "credentials", "libraries",
                         "library_acl", "acl_operations", "library_routes", "migration_runs"} <= tables)
        self.assertRegex(sql, r"REFERENCES em_control\.devices\s*\(device_id, principal_id\)")
        self.assertRegex(sql, r"credential_digest\s+bytea")
        self.assertRegex(sql, r"REVOKE ALL ON SCHEMA em_control FROM PUBLIC")

    def test_content_migration_has_session_identity_and_closed_defaults(self):
        sql = self.read("001_content.sql")
        self.assertEqual([], security_findings(sql))
        self.assertRegex(sql, r"CREATE FUNCTION em_security\.session_context\(\)")
        self.assertRegex(sql, r"login_role\s*=\s*session_user")
        self.assertNotRegex(without_comments(sql), r"CREATE TABLE em_content\.")
        self.assertRegex(sql, r"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA em_security FROM PUBLIC")
        self.assertRegex(sql, r"REVOKE ALL ON SCHEMA em_content FROM PUBLIC")
        self.assertRegex(sql, r"projection_epoch\s*=\s*s\.policy_epoch")

    def test_device_library_cannot_name_a_different_owner(self):
        sql = self.read("001_control.sql")
        body = re.search(r"CREATE TABLE em_control\.libraries\s*\((.*?)\);", sql, re.S)[1]
        self.assertRegex(body, r"FOREIGN KEY \(device_id, owner_principal_id\)\s+REFERENCES em_control\.devices\(device_id, principal_id\)")

    def test_declared_connection_reference_constraint_excludes_paths_and_dsns(self):
        sql = self.read("001_control.sql")
        # Static examination of this deliberately portable character-class regex;
        # this is not a PostgreSQL CHECK execution test.
        declared = re.search(r"connection_ref ~ '([^']+)'", sql)[1]
        for rejected in ("postgresql://host/database", "/tmp/database", "vault/credential", "../secret"):
            self.assertIsNone(re.fullmatch(declared, rejected), rejected)
        self.assertIsNotNone(re.fullmatch(declared, "synthetic.device-a"))

    def test_runtime_grants_never_grant_auth_table_writes_or_owner_membership(self):
        sql = self.read("provision_runtime.sql")
        self.assertEqual([], security_findings(sql))
        self.assertNotRegex(without_comments(sql), r"(?i)GRANT\s+(INSERT|UPDATE|DELETE|ALL)\b")
        self.assertRegex(sql, r"rolsuper\s+OR\s+r\.rolbypassrls")
        self.assertRegex(sql, r"pg_has_role")
        self.assertRegex(sql, r"GRANT EXECUTE ON FUNCTION em_security\.session_context\(\)")

    def test_credential_projection_is_session_bound_and_requires_current_acl(self):
        sql = self.read("001_content.sql")
        match = re.search(r"CREATE FUNCTION em_security\.session_credential\(\)(.*?)\$function\$\s*;", sql, re.S)
        self.assertIsNotNone(match, "Missing narrow credential projection function")
        body = match[1]
        self.assertRegex(body, r"RETURNS TABLE\(credential_id uuid, authenticated_device_id uuid, credential_epoch bigint\)")
        self.assertRegex(body, r"SECURITY DEFINER")
        self.assertRegex(body, r"login_role\s*=\s*session_user")
        self.assertRegex(body, r"b\.enabled")
        self.assertRegex(body, r"em_security\.session_context\(\)")
        self.assertRegex(body, r"SET search_path = pg_catalog, em_security")
        self.assertEqual([], security_findings(sql))
        grants = self.read("provision_runtime.sql")
        self.assertRegex(grants, r"GRANT EXECUTE ON FUNCTION em_security\.session_credential\(\)")

    def test_future_business_template_enforces_both_read_and_write(self):
        sql = self.read("content_table_policy.sql.in")
        self.assertRegex(sql, r"ENABLE ROW LEVEL SECURITY")
        self.assertRegex(sql, r"FORCE ROW LEVEL SECURITY")
        self.assertRegex(sql, re.compile(r"FOR SELECT.*?USING", re.S))
        self.assertRegex(sql, re.compile(r"FOR INSERT.*?WITH CHECK", re.S))
        self.assertRegex(sql, re.compile(r"FOR UPDATE.*?USING", re.S))
        self.assertRegex(sql, r"FOR UPDATE[\s\S]*?WITH CHECK")
        self.assertRegex(sql, re.compile(r"FOR DELETE.*?USING", re.S))

    def test_local_credential_expiration_is_checked_without_a_background_worker(self):
        sql = self.read('001_content.sql')
        table = re.search(r'CREATE TABLE em_security\.principal_bindings\s*\((.*?)\);', sql, re.S)[1]
        self.assertRegex(table, r'expires_at\s+timestamptz')
        body = re.search(r'CREATE FUNCTION em_security\.session_context\(\)(.*?)\$function\$\s*;', sql, re.S)[1]
        self.assertRegex(body, r'b\.expires_at IS NULL OR b\.expires_at > (?:pg_catalog\.)?statement_timestamp\(\)')


if __name__ == "__main__":
    unittest.main()
