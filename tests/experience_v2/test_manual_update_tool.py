"""Explicit manual edit uses current revision and stable request ID."""
import pytest
from evolvmem import manual_experience_mcp as m

def test_manual_update_registered_and_preserves_cas(monkeypatch):
    spec=m.MANUAL_EXPERIENCE_SPECS['manual_experience_update']['inputSchema']
    assert {'id','revision','request_id',*m.manual_experiences.FIELDS}<=set(spec['required'])
    seen=[]
    def write(source, actor, body, **kw):
        seen.append(body);kw['validate_new'](body)
        return 200,{'ok':True,'experience':{'revision':3}}
    monkeypatch.setattr(m.manual_experiences,'write',write)
    args=dict(id='manual-'+'a'*32,revision=2,request_id='manual-update-01',category='业务经验',
              problem='费用字段为空',solution='查原始费用记录补齐',reusable_experience='核实费用后才核算')
    assert m.call('manual_experience_update',args,{'user_id':'alice'})['ok']
    assert seen==[{**args,'action':'update'}]
    with pytest.raises(m.ManualMcpError):m.call('manual_experience_update',{k:v for k,v in args.items() if k!='revision'},{'user_id':'alice'})
