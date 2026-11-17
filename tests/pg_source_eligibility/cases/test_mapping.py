import importlib.util,json,sqlite3,unittest
from pathlib import Path
HERE=Path(__file__).resolve().parents[1]


class Mapping(unittest.TestCase):
    def mapping(self):
        spec=importlib.util.spec_from_file_location('organization_mapping',HERE/'organization_mapping.py')
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
    def test_all_seven_exact_saved_fields_and_bad_json_are_lossless(self):
        m=self.mapping();source=json.loads((HERE/'reviewed-schema.json').read_text(encoding='utf-8'))
        self.assertEqual(set(source),set(m.SPECS))
        for table,spec in source.items():
            self.assertEqual([c['name'] for c in spec['columns']],list(m.SPECS[table]))
            row={c['name']:('bad { raw TEXT' if c['type']=='TEXT' else 1) for c in spec['columns']}
            for c in spec['columns']:
                if not c['not_null'] and not (table in ('organization_tasks','learning_memories') and c['pk_position']):row[c['name']]=None
            row['source_rowid']=1
            self.assertEqual(row,m.map_row(table,row))
    def test_int64_min_and_nullable_text_pk_preserved(self):
        m=self.mapping();row=dict(source_rowid=-(2**63),source_key=None,identity='x',members='[]',updated_at='a')
        self.assertEqual(row,m.map_row('organization_conversations',row))
        self.assertEqual(2,len(m.chunk('organization_conversations',[row,dict(row,source_rowid=1)])))
    def test_wrong_storage_types_duplicate_rowid_and_missing_values_refuse(self):
        m=self.mapping();row=dict(source_rowid=1,source_key=None,identity='x',members='[]',updated_at='a')
        with self.assertRaisesRegex(ValueError,'duplicate_source_rowid'):m.chunk('organization_conversations',[row,row])
        with self.assertRaisesRegex(ValueError,'source_shape'):m.map_row('organization_conversations',dict(row,unknown='x'))
        with self.assertRaisesRegex(ValueError,'source_storage_type'):m.map_row('organization_conversations',dict(row,members=b'[]'))
    def test_ddl_never_promotes_or_rewrites_old_history(self):
        sql=(HERE/'sql/001_organization_qualification.sql').read_text()
        self.assertNotIn('DROP POLICY',sql);self.assertNotIn('pg_active',sql)
        self.assertNotIn('DELETE FROM',sql);self.assertNotIn('domain_ready',sql)
        self.assertIn('history_organization_quarantine',sql)
        self.assertIn('ENABLE ALWAYS TRIGGER',sql)
