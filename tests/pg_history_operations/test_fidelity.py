"""Review regressions only; synthetic SQLite and catalog responses, never PG."""
import unittest
from unittest.mock import patch
from test_stream import Fixture, source, N, M
from evolvmem.history_migration import operations_importer as I


class StorageFidelity(Fixture):
    def test_invalid_utf8_text_is_not_exported_as_blob(self):
        ref = source(self.temp.name, mutate=lambda c: c.execute(
            "UPDATE organization_review_vectors SET vector=CAST(X'00FF' AS TEXT) WHERE rowid=1"))
        with patch.object(N.Snapshot, 'chunk_arguments', autospec=True, side_effect=N.Snapshot.chunk_arguments) as export:
            with self.assertRaisesRegex(M.ImportError, '^(operations_source_storage_type_changed|source_probe_storage_changed)$'):
                with N.Phase(ref) as phase:
                    phase.page('organization_review_vectors', None, 0)
            export.assert_not_called()

    def test_fixed_storage_classes_and_nullability_are_checked(self):
        from types import SimpleNamespace
        for table in M.TABLES:
            columns = M.SPECS[table]
            good = {name: {'INTEGER':'integer','REAL':'real','TEXT':'text','BLOB':'blob'}[kind]
                    for name, kind, nullable in columns}
            N.validate_storage(SimpleNamespace(table=table, rows=({},), storage_types=(good,)))
            for name, kind, nullable in columns:
                wrong = dict(good, **{name: 'blob' if kind != 'BLOB' else 'text'})
                with self.subTest(table=table, column=name, mismatch=True):
                    with self.assertRaisesRegex(M.ImportError, 'operations_source_storage_type_changed'):
                        N.validate_storage(SimpleNamespace(table=table, rows=({},), storage_types=(wrong,)))
                null = dict(good, **{name: 'null'})
                with self.subTest(table=table, column=name, nullable=nullable):
                    page = SimpleNamespace(table=table, rows=({},), storage_types=(null,))
                    if nullable:
                        N.validate_storage(page)
                    else:
                        with self.assertRaisesRegex(M.ImportError, 'operations_source_storage_type_changed'):
                            N.validate_storage(page)


class SecurityTriggerFidelity(unittest.TestCase):
    def test_trigger_definition_and_function_are_pinned(self):
        port = object.__new__(I.OperationsImporter)
        good = [(table,) + (True,) * len(I.SECURITY_SCOPE_CHECKS) for table in sorted(M.TARGET_TABLES)]
        observed = []
        def rows(connection, sql, params=()):
            observed.append((sql, params))
            return good
        with patch.object(I, 'rows', side_effect=rows):
            port._security_scope_gate(object())
        sql, params = observed[0]
        self.assertIn('t.tgtype=19', sql) # before update row, never insert replay
        self.assertIn('f.prosrc=%s', sql)
        self.assertIn("'em_content.keep_security_scope()'", sql)
        self.assertEqual(params, (I.SECURITY_SCOPE_BODY, list(M.TARGET_TABLES)))
        for index, predicate in enumerate(I.SECURITY_SCOPE_CHECKS):
            bad = list(good)
            values = list(bad[0]); values[index + 1] = False; bad[0] = tuple(values)
            with self.subTest(predicate=predicate), patch.object(I, 'rows', return_value=bad):
                with self.assertRaisesRegex(M.ImportError, 'operations_security_scope_trigger_required'):
                    port._security_scope_gate(object())
        for bad in (good[:-1], good + good[:1], [(good[0][0],) + (1,) * len(I.SECURITY_SCOPE_CHECKS)] + good[1:]):
            with patch.object(I, 'rows', return_value=bad):
                with self.assertRaisesRegex(M.ImportError, 'operations_security_scope_trigger_required'):
                    port._security_scope_gate(object())


if __name__ == '__main__': unittest.main()
