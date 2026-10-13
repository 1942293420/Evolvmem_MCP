import json
import pytest


def engine():
    try:
        from evolvmem import task_narrative
    except ImportError:
        pytest.fail('task narrative stage is missing')
    return task_narrative


MESSAGES = [{'role':'user','content':'增加写入开关。'},
            {'role':'assistant','content':'更新失败，原因是配置未迁移。'},
            {'role':'assistant','content':'补充迁移后已可用，等待实际验收。'}]


def answer():
    return {'requirement':{'text':'支持控制写入','evidence':[{'message':1,'quote':'增加写入开关。'}]},
            'analysis':{'text':'旧配置缺少迁移','evidence':[{'message':2,'quote':'原因是配置未迁移'}]},
            'steps':[{'text':'修复迁移','evidence':[{'message':3,'quote':'补充迁移后已可用'}]}],
            'result':{'text':'助手报告可用，仍待验收','evidence':[{'message':3,'quote':'等待实际验收'}]},
            'outstanding':['等待实际验收'], 'state':'reported_complete'}


def test_narrative_keeps_need_cause_repairs_and_unverified_outcome():
    record=engine().organize(MESSAGES,lambda _:json.dumps(answer()))
    assert record['requirement'][0]['text']=='支持控制写入'
    assert record['steps'][0]['evidence'][0]['role']=='assistant'
    assert record['result']['text']=='助手报告可用，仍待验收'
    assert record['verification']=='unverified'
    assert '等待实际验收' in record['outstanding']
    assert '支持控制写入' in engine().render(record)


def test_fabricated_quote_or_role_cannot_become_evidence():
    bad=answer();bad['result']['evidence'][0]['quote']='用户验收通过'
    with pytest.raises(ValueError,match='task_narrative_evidence'):
        engine().organize(MESSAGES,lambda _:json.dumps(bad))


def test_long_task_reads_all_windows_and_keeps_last_failure():
    outputs=[answer(),{'requirement':None,'analysis':None,'steps':[],
                      'result':{'text':'重新测试仍失败','evidence':[{'message':4,'quote':'还是失败'}]},
                      'outstanding':['继续修复'], 'state':'open'}]
    messages=MESSAGES+[{'role':'user','content':'重新测试还是失败'}]
    record=engine().organize(messages,lambda _:json.dumps(outputs.pop(0)),max_messages=3)
    assert not outputs
    assert record['requirement'][0]['text']=='支持控制写入'
    assert record['state']=='open' and record['result']['text']=='重新测试仍失败'
    assert any(s['text']=='助手报告可用，仍待验收' for s in record['steps'])


def test_no_requirement_visible_is_not_invented():
    result={'requirement':None,'analysis':None,'steps':[], 'result':None,'outstanding':[], 'state':'unknown'}
    record=engine().organize([{'role':'assistant','content':'处理完了'}],lambda _:json.dumps(result))
    assert not record['requirement'] and record['state']=='unknown'


def test_later_progress_without_new_result_keeps_unfinished_work():
    first=answer();first['state']='open'
    outputs=[first,{'requirement':None,'analysis':None,'steps':[], 'result':None,'outstanding':[], 'state':'unknown'}]
    record=engine().organize(MESSAGES+[{'role':'assistant','content':'还在继续检查日志'}],
        lambda _:json.dumps(outputs.pop(0)),max_messages=3)
    assert record['state']=='open'
    assert record['outstanding']==['等待实际验收']
