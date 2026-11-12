import hashlib
import re
import unittest
from pathlib import Path

OUT = Path(__file__).resolve().parents[2]
R = OUT
REL = Path("evolvmem/library_project_acl/sql")
BASELINE = R / REL / "005_control_project_acl.sql"
CANDIDATE = OUT / REL / "005a_control_acl_identity_share.sql"
BASELINE_FILE_SHA = "b42c004b56d08671cfe8217940a16a93fdb6685dc1b14a01a7cee78ca01b0d7b"
FUNCTION = "CREATE OR REPLACE FUNCTION em_control.get_acl_projection_plan(p_operation uuid)"
OLD_COMMENT = (
    " -- Lock all relevant identities before library/route, including inactive rows.\n"
    " -- UPDATE locks on parent identities also block FK KEY SHARE from new device/\n"
    " -- credential inserts; merely locking existing credential rows misses phantoms.\n"
    " -- No online identity mutation API is supported by this projector.\n"
)
NEW_LOCK = (
    " -- Table SHARE prevents identity DML and new FK children while allowing pins.\n"
    " LOCK TABLE em_control.principals, em_control.devices, em_control.credentials IN SHARE MODE;\n"
)
ROW_LOCKS = ("ORDER BY p.principal_id FOR ", "ORDER BY d.device_id FOR ")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def definition(text):
    matched = re.search(re.escape(FUNCTION) + r"\n(.*?)\$fn\$(.*?)\$fn\$;", text, re.S)
    if matched is None:
        raise AssertionError("exact SQL function declaration missing")
    return matched.groups()


class IdentityShareSql(unittest.TestCase):
    def setUp(self):
        self.baseline = BASELINE.read_bytes()
        self.sql = CANDIDATE.read_text(encoding="ascii")
        self.old_header, self.old_body = definition(self.baseline.decode("ascii").replace("\r\n", "\n"))
        self.new_header, self.new_body = definition(self.sql)

    def test_baseline_bytes_and_definition_lock_only_diff(self):
        self.assertEqual(sha(self.baseline), BASELINE_FILE_SHA)
        self.assertEqual(self.new_header, self.old_header)
        self.assertEqual(self.new_body.count(NEW_LOCK), 1)
        self.assertNotIn(OLD_COMMENT, self.new_body)
        reconstructed = self.new_body.replace(NEW_LOCK, OLD_COMMENT)
        for prefix in ROW_LOCKS:
            self.assertEqual(reconstructed.count(prefix + "SHARE;"), 1)
            reconstructed = reconstructed.replace(prefix + "SHARE;", prefix + "UPDATE;")
        self.assertEqual(reconstructed, self.old_body)

    def test_body_fingerprints_and_fail_closed_idempotence(self):
        for label, body in (("old_body_sha256", self.old_body), ("new_body_sha256", self.new_body)):
            self.assertIn("\\set " + label + " " + sha(body.encode("ascii")) + "\n", self.sql)
        self.assertIn("body_sha256 IN (:'old_body_sha256', :'new_body_sha256')", self.sql)
        self.assertIn("body_sha256=:'old_body_sha256' AS apply_patch", self.sql)
        self.assertIn("\\if :apply_patch\n" + FUNCTION, self.sql)
        self.assertIn("acl_identity_share_unrecognized_body", self.sql)
        self.assertIn("acl_identity_share_postcheck_failed", self.sql)
        self.assertIn("=:'new_body_sha256'", self.sql)

    def test_fixed_lock_order_and_existing_fences(self):
        ordered = (
            "_require_service('coordinate')", NEW_LOCK, "ORDER BY p.principal_id FOR SHARE;",
            "ORDER BY d.device_id FOR SHARE;", "ORDER BY c.credential_id FOR SHARE;",
            "o:=em_control._lock_acl_operation(p_operation);", "projection_plan_changed",
        )
        offsets = [self.new_body.index(item) for item in ordered]
        self.assertEqual(offsets, sorted(offsets))

    def test_metadata_and_privilege_guards_precede_only_replacement(self):
        prefix, suffix = self.sql.split(FUNCTION)
        for token in (
            "pg_advisory_xact_lock(hashtextextended('evolvmem.control.schema',0))",
            "current_database()=:'database_name'", "current_user=:'owner_role'",
            "to_regprocedure('em_control.get_acl_projection_plan(uuid)')",
            "p.prosecdef", "p.proretset", "p.proargnames", "p.proargmodes",
            "p.proallargtypes", "p.proconfig", "p.proowner", "aclexplode(p.proacl)",
            "has_function_privilege", "has_table_privilege", "has_any_column_privilege",
            "pg_has_role", "acl_identity_share_security_precondition_failed",
        ):
            self.assertIn(token, prefix)
        outer = prefix + suffix.split("$fn$;", 1)[1]
        self.assertNotRegex(outer, r"(?im)^\s*(GRANT|REVOKE|DROP|ALTER|CREATE|INSERT|UPDATE|DELETE|TRUNCATE)\b")
        self.assertIn("version=5 AND migration_name='005_control_project_acl'", outer)
        self.assertLess(outer.index("BEGIN;"), outer.index("pg_advisory_xact_lock"))
        self.assertLess(outer.index("COMMIT;"), outer.index(" AS migration_name"))


if __name__ == "__main__":
    unittest.main()
