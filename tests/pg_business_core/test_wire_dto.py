"""Wire contract against actual original methods/models; no PG claim.

CoreSpy records typed domain calls. It is not used as a database fixture.
"""
from evolvmem.config import Config
from evolvmem.context_models import ContextLayer,ContextReadResult
from evolvmem.legacy_models import LegacyMutationResult,LegacyAddRequest,LegacyReplaceRequest
from evolvmem.pg_business_core.wire import LegacyWire
from evolvmem.pg_business_core.domain import UnsupportedBusiness
import unittest

LAYERS=(ContextLayer.L0,ContextLayer.L1,ContextLayer.L2)

class CoreSpy:
    config=Config()
    def __init__(self):self.calls=[];self.rows=[];self.read_error=None
    def get_by_key(self,key):return self.rows
    def legacy_add(self,request):
        self.calls.append(request);return LegacyMutationResult(7,70,None,None,LAYERS,True)
    def legacy_replace(self,request):
        self.calls.append(request);return LegacyMutationResult(8,80,7,70,LAYERS,True)
    def legacy_remove(self,request):
        self.calls.append(request);return LegacyMutationResult(request.legacy_id,None,None,None,(),False)
    def read(self,request):
        self.calls.append(request);return ContextReadResult(request.id,request.layer,'' if self.read_error else 'Synthetic exact body.',self.read_error)

class WireDTOTests(unittest.TestCase):
    def setUp(self):self.core=CoreSpy();self.wire=LegacyWire(self.core,semantic_merge_enabled=False)
    def test_memory_add_exact_original_field_mapping(self):
        result=self.wire.memory_add(dict(key='synthetic-key',value='Synthetic meaningful original body.',tags=['a']))
        self.assertEqual(dict(status='added',id=7,context_id=70,old_context_id=None,available_layers=['l0','l1','l2'],index_state='degraded'),result)
        request=self.core.calls[0];self.assertIsInstance(request,LegacyAddRequest)
        self.assertEqual(('fact',('a',),5.0,'normal'),(request.attribute,request.tags,request.importance,request.tier))
    def test_memory_replace_preserves_omitted_inheritance_fields(self):
        result=self.wire.memory_replace(dict(key='synthetic-key',value='Synthetic meaningful replacement body.'))
        self.assertEqual(dict(status='replaced',new_id=8,context_id=80,old_context_id=70,available_layers=['l0','l1','l2'],index_state='degraded'),result)
        request=self.core.calls[0];self.assertIsInstance(request,LegacyReplaceRequest)
        self.assertIsNone(request.attribute);self.assertIsNone(request.tags);self.assertIsNone(request.importance)
    def test_missing_remove_dto_does_not_pretend_it_created_context(self):
        result=self.wire.memory_remove({'id':9})
        self.assertEqual(dict(status='deleted',id=9,context_id=None,old_context_id=None,available_layers=[],index_state='degraded'),result)
    def test_invalid_inputs_preserve_old_messages_and_do_not_call_domain(self):
        self.assertEqual({'error':'key and value parameters cannot be empty'},self.wire.memory_add({'key':'k','value':''}))
        self.assertEqual({'error':'id parameter cannot be empty'},self.wire.memory_remove({'id':0}))
        self.assertEqual([],self.core.calls)
    def test_context_read_exact_tool_name_and_result(self):
        self.assertFalse(hasattr(self.wire,'memory_read'))
        self.assertEqual(dict(id=70,layer='l2',content='Synthetic exact body.'),self.wire.context_read({'id':70,'layer':'l2'}))
        self.core.read_error='not_readable'
        result=self.wire.context_read({'id':70,'layer':'l2'})
        self.assertEqual({'error','message'},set(result));self.assertEqual('not_readable',result['error'])
        self.assertEqual('invalid_arguments',self.wire.context_read({'id':70,'layer':'l0'})['error'])
    def test_required_merge_is_explicit_but_reference_remains_supported(self):
        wire=LegacyWire(self.core,semantic_merge_enabled=True)
        with self.assertRaises(UnsupportedBusiness) as caught:wire.memory_add(dict(key='k',value='Synthetic semantic merge candidate body.'))
        self.assertEqual('unsupported_semantic_merge',caught.exception.code)
        self.assertEqual([],self.core.calls)
        self.assertEqual('added',wire.memory_add(dict(key='k',value='Synthetic nonmerging reference body.',tier='reference'))['status'])

if __name__=='__main__':unittest.main()
