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


def test_wrong_message_number_is_resolved_only_by_unique_verbatim_quote():
    data=answer();data['result']['evidence'][0]['message']=999
    record=engine().organize(MESSAGES,lambda _:json.dumps(data))
    assert record['result']['evidence'][0]['message']==3
    assert record['result']['evidence'][0]['role']=='assistant'


def test_correction_identifies_exact_bad_quote_and_field():
    bad=answer();bad['steps'][0]['evidence'][0]['quote']='迁移已全部完成'
    calls=[]
    def provider(prompt):
        calls.append(prompt)
        if len(calls)==1:return json.dumps(bad)
        assert 'steps[0].evidence[0]' in prompt
        assert '迁移已全部完成' in prompt
        return json.dumps(answer())
    assert engine().organize(MESSAGES,provider)['steps']
    assert len(calls)==2


def test_json_correction_includes_parser_location():
    calls=[]
    def provider(prompt):
        calls.append(prompt)
        if len(calls)==1:return '{"requirement": [}'
        assert 'JSON' in prompt and '第 1 行' in prompt
        return json.dumps(answer())
    assert engine().organize(MESSAGES,provider)['requirement']


def test_repeated_quote_in_one_message_still_has_unambiguous_message_source():
    messages=[{'role':'user','content':'增加写入开关。再次强调：增加写入开关。'}]
    data={'requirement':{'text':'增加开关','evidence':[{'message':99,'quote':'增加写入开关。'}]},'analysis':None,'steps':[],'result':None,'outstanding':[],'state':'open'}
    assert engine().organize(messages,lambda _:json.dumps(data))['requirement'][0]['evidence'][0]['message']==1


def test_evidence_source_identifier_copies_original_without_model_transcription():
    data=answer();data['steps'][0]['evidence']=[{'source':'e3'}]
    record=engine().organize(MESSAGES,lambda _:json.dumps(data))
    assert record['steps'][0]['evidence']==[{'message':3,'quote':MESSAGES[2]['content'],'role':'assistant'}]
    data['steps'][0]['evidence']=[{'source':'e999'}]
    with pytest.raises(ValueError,match='task_narrative_evidence'):
        engine().organize(MESSAGES,lambda _:json.dumps(data))


def test_explicit_null_steps_mean_no_actions_in_this_window():
    data={'requirement':None,'analysis':None,'steps':None,'result':None,'outstanding':None,'state':'unknown'}
    record=engine().organize([{'role':'assistant','content':'等待后续回复。'}],lambda _:json.dumps(data))
    assert record['steps']==[] and record['outstanding']==[]
    assert not record['requirement'] and record['state']=='unknown'


def test_repeated_requirements_merge_evidence_but_keep_changed_requirements():
    messages=[{'role':'user','content':t} for t in ('增加开关。','还是需要这个开关。','改为默认关闭。')]
    outputs=[{'requirement':{'text':t,'evidence':[{'source':f'e{i+1}'}]},
              'analysis':None,'steps':[],'result':None,'outstanding':[],'state':'open'}
             for i,t in enumerate(('增加写入开关','增加写入开关','开关默认关闭'))]
    record=engine().organize(messages,lambda _:json.dumps(outputs.pop(0)),max_messages=1)
    assert [x['text'] for x in record['requirement']]==['增加写入开关','开关默认关闭']
    assert len(record['requirement'][0]['evidence'])==2


def test_existing_records_normalize_on_read_without_mutating_saved_evidence():
    row={'text':'增加开关','evidence':[{'message':1,'role':'user','quote':'增加开关。'}]}
    original={'version':engine().VERSION,'requirement':[row,row]}
    assert len(engine().read(original)['requirement'])==1
    assert len(original['requirement'])==2


def test_source_quote_can_select_exact_short_evidence_not_whole_log_fragment():
    data=answer();data['steps'][0]['evidence']=[{'source':'e3','quote':'补充迁移后已可用'}]
    record=engine().organize(MESSAGES,lambda _:json.dumps(data))
    assert record['steps'][0]['evidence'][0]['quote']=='补充迁移后已可用'
    data['steps'][0]['evidence'][0]['quote']='用户已经验收通过'
    with pytest.raises(ValueError,match='task_narrative_evidence'):
        engine().organize(MESSAGES,lambda _:json.dumps(data))


def test_assistant_progress_cannot_be_presented_as_new_user_requirement():
    data={'requirement':{'text':'增加开关','evidence':[{'source':'e1'}]},'analysis':None,
          'steps':[],'result':None,'outstanding':[],'state':'open'}
    with pytest.raises(ValueError,match='task_narrative_evidence'):
        engine().organize([{'role':'assistant','content':'继续跑回归测试。'}],lambda _:json.dumps(data))


def test_compact_overview_keeps_key_repairs_without_per_evidence_controls():
    record={'requirement':[{'text':'刷新执行器状态','evidence':[]}],
            'analysis':[{'text':'页面没有刷新清单','evidence':[]}],
            'steps':[{'text':text,'evidence':[]} for text in [
                '检查接口返回的状态。','修复缓存导致的状态过期。','补上切回页面立即刷新。',
                '补上切回页面立即刷新。','修复下拉框焦点阻止刷新的问题。',
                '助手报告已上线；仍待用户验收。']],
            'result':{'text':'助手报告已上线；仍待用户验收。','evidence':[]}}
    before=json.dumps(record)
    result=engine().overview(record)
    assert '缓存' in result['steps'] and '焦点' in result['steps']
    assert result['steps'].count('切回页面')==1
    assert '仍待用户验收' in result['result']
    assert len(result['steps'])<160
    assert json.dumps(record)==before
