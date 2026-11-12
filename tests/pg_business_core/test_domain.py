"""Pure rules only. SQL/transaction claims require the separate real PG suite."""
import unittest
from pathlib import Path

from evolvmem.config import Config
from evolvmem.context_models import ContextLayer
from evolvmem.legacy_models import LegacyAddRequest, LegacyReplaceRequest, LegacyMutationResult
from evolvmem.project_models import ProjectRegistrySnapshot
from evolvmem.pg_business_core.domain import projection_payload, resolved_draft, mutation_response, UnsupportedBusiness


class DomainContract(unittest.TestCase):
    def setUp(self):
        self.config = Config(data_dir=Path('/unused-synthetic-pg-core'))
        self.snapshot = ProjectRegistrySnapshot(('evolvmem', 'procurement'),
            (('evo','evolvmem'),), (), ('workspace',), 1)
        self.old = dict(id=11,key='project:evo:fact:config',value='prior exact body',
            attribute='fact',tags='kept,verified',source_session='old-source',
            importance=8.0,tier='pinned',expires_at='2099-01-01 00:00:00',status='active')

    def test_replace_inherits_omitted_metadata_but_source_session_is_request_owned(self):
        result = projection_payload(LegacyReplaceRequest(key=self.old['key'],new_value='replacement exact body'), self.old)
        self.assertEqual((result['attribute'],result['tags'],result['importance'],result['tier'],result['expires_at']),
                         ('fact','kept,verified',8.0,'pinned','2099-01-01 00:00:00'))
        self.assertEqual(result['source_session'], '')
        self.assertEqual(result['value'], 'replacement exact body')

    def test_replace_explicit_empty_tags_does_not_inherit(self):
        result = projection_payload(LegacyReplaceRequest(key=self.old['key'],new_value='replacement exact body',tags=()), self.old)
        self.assertEqual(result['tags'], '')

    def test_replace_without_predecessor_uses_original_defaults(self):
        result = projection_payload(LegacyReplaceRequest(key='new',new_value='new exact content'))
        self.assertEqual((result['importance'],result['tier'],result['attribute'],result['tags']), (5.0,'normal','',''))

    def test_add_date_expiry_is_normalized_without_inventing_event_time(self):
        result = projection_payload(LegacyAddRequest(key='new',value='new exact content',expires_at='2099-01-01'))
        self.assertEqual(result['expires_at'], '2099-01-01 00:00:00')

    def test_existing_resolver_alias_and_existing_migrator_generate_layers(self):
        request = LegacyAddRequest(key=self.old['key'],value='A concrete deployment record with exact source content.',attribute='fact')
        row = dict(projection_payload(request),id=12,status='active')
        draft, decision = resolved_draft(self.config,row,self.snapshot,request)
        self.assertEqual(draft.project, 'evolvmem')
        self.assertEqual(decision.state.value, 'resolved')
        self.assertEqual(draft.layers.l2, request.value)
        self.assertIsNone(draft.occurred_at)
        self.assertIsNone(draft.effective_from)

    def test_unverified_workspace_input_is_explicitly_unsupported(self):
        request = LegacyAddRequest(key='a',value='concrete content',workspace_path='/untrusted/path')
        with self.assertRaises(UnsupportedBusiness) as raised:
            resolved_draft(self.config,dict(self.old),self.snapshot,request)
        self.assertEqual(raised.exception.code, 'unsupported_workspace_resolution')

    def test_mutation_dto_keeps_ids_and_layers_and_reports_pending_index(self):
        result = LegacyMutationResult(21,31,20,30,tuple(ContextLayer),True)
        value = mutation_response({'status':'replaced','new_id':21},result)
        self.assertEqual(value,dict(status='replaced',new_id=21,context_id=31,old_context_id=30,
                                   available_layers=['l0','l1','l2'],index_state='degraded'))


if __name__ == '__main__':
    unittest.main()
