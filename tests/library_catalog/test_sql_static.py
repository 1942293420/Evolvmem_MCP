"""Static DDL lint, explicitly not PostgreSQL execution or lock verification."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2] / 'evolvmem/library_catalog'


class SqlStaticTests(unittest.TestCase):
    def test_pin_case_expression_is_parenthesized_for_plpgsql_if(self):
        sql = self.read('002_catalog_api.sql')
        pin = re.search(r'CREATE FUNCTION em_control\.pin_library\b(.*?)\$fn\$;', sql, re.S)[1]
        self.assertRegex(pin, r'OR NOT \(CASE p_action[\s\S]*ELSE false END\)')
        self.assertNotRegex(pin, r'OR NOT CASE p_action')

    def read(self, name):
        path = ROOT / 'sql' / name
        self.assertTrue(path.exists(), 'missing versioned control API SQL')
        return path.read_text(encoding='utf-8')

    def test_pin_has_all_dependency_row_locks_and_no_guc_identity(self):
        sql = self.read('002_catalog_api.sql')
        pin = re.search(r'CREATE FUNCTION em_control\.pin_library\b(.*?)\$fn\$;', sql, re.S)[1]
        for table in ('principals', 'devices', 'credentials', 'libraries', 'library_routes', 'library_acl'):
            self.assertRegex(pin, rf'FROM em_control\.{table}\b[^;]*FOR SHARE')
        self.assertNotRegex(sql, r"current_setting\s*\(")
        self.assertRegex(sql, r'login_role\s*=\s*session_user')
        self.assertRegex(pin, r'p_policy_epoch IS NULL')
        self.assertRegex(pin, r'p_route_epoch IS NULL')

    def test_only_narrow_service_functions_are_granted(self):
        sql = self.read('provision_catalog_service.sql')
        self.assertNotRegex(sql, r'(?i)GRANT\s+(SELECT|INSERT|UPDATE|DELETE|ALL)\s+ON\s+(ALL\s+)?TABLES?')
        self.assertRegex(sql, r'GRANT EXECUTE ON FUNCTION em_control\.pin_library')
        self.assertRegex(sql, r'pg_auth_members')
        self.assertRegex(sql, r'has_table_privilege')

    def test_acl_changes_require_intent_projection_receipt_and_conflicting_route_lock(self):
        sql = self.read('003_acl_api.sql')
        for function in ('begin_acl_change', 'confirm_acl_projection', 'finish_acl_change', 'fail_acl_change'):
            body = re.search(r'CREATE FUNCTION em_control\.' + function + r'\b(.*?)\$fn\$;', sql, re.S)
            self.assertIsNotNone(body, function)
            self.assertRegex(body[1], r'FOR UPDATE')
        self.assertRegex(sql, r"acl_state\s*=\s*'updating'")
        self.assertRegex(sql, r'acl_projection_receipts')
        self.assertRegex(sql, r"acl_state\s*=\s*'ready'")
        self.assertRegex(sql, r'FORCE ROW LEVEL SECURITY')
        self.assertNotRegex(sql, r'ON CONFLICT\s*\(operation_id\)')
        self.assertNotRegex(sql, r'ON CONFLICT\s*\(library_id,principal_id\)')
        lock = re.search(r'CREATE FUNCTION em_control\._lock_acl_operation\b(.*?)\$fn\$;', sql, re.S)[1]
        self.assertRegex(lock, r'r\.route_epoch=o\.expected_route_epoch')
        self.assertRegex(lock, r'l\.connection_ref=o\.expected_connection_ref')
        self.assertRegex(lock, r"r\.authority_state='pg_active'")
        self.assertRegex(sql, r'p_route_epoch IS DISTINCT FROM o.expected_route_epoch')
        begin = re.search(r'CREATE FUNCTION em_control\.begin_acl_change\b(.*?)\$fn\$;', sql, re.S)[1]
        self.assertRegex(begin, r"prior\.operation_state<>'complete'[\s\S]*_lock_acl_operation\(prior.operation_id\)")
        self.assertRegex(lock, r'r\.policy_epoch=o\.expected_policy_epoch')


if __name__ == '__main__': unittest.main()
