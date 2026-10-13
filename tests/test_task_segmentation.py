"""Task identity survives processing windows and interleaved conversation."""
import json
import pytest
from evolvmem.topic_segmentation import message_spans


def engine():
    try:
        from evolvmem import task_segmentation
    except ImportError:
        pytest.fail('requirement-to-result task segmentation is not implemented')
    return task_segmentation


def test_task_continues_across_model_windows_and_intervening_task():
    api=engine()
    messages=[{'role':'user','content':'增加写入设置。'},
              {'role':'assistant','content':'设置已添加，更新报错需要继续修复。'},
              {'role':'user','content':'先解释货件筛选规则。'},
              {'role':'assistant','content':'货件按当前视图筛选。'},
              {'role':'user','content':'回到写入开关，更新后仍然报错。'},
              {'role':'assistant','content':'修正了配置读取，更新后开关可用。'}]
    spans=message_spans(messages);source='\n\n'.join(s['part'] for s in spans)
    responses=iter([
        {'segments':[{'task':'new','start_id':1,'end_id':2,'title':'增加写入设置并完成更新','need_quote':'增加写入设置。'}]},
        {'segments':[{'task':'new','start_id':3,'end_id':4,'title':'解释货件筛选规则','need_quote':'先解释货件筛选规则。'}]},
        {'segments':[{'task':'t1','start_id':5,'end_id':6,'title':'增加写入设置并完成更新','need_quote':'增加写入设置。'}]},
    ])
    units=api.segment(source,lambda prompt:json.dumps(next(responses),ensure_ascii=False),records=spans,max_records=2)
    assert len(units)==2
    assert '增加写入设置' in units[0]['text'] and '开关可用' in units[0]['text']
    assert '货件按当前视图筛选' not in units[0]['text']
    assert len(units[0]['source_ranges'])==2
    assert '货件按当前视图筛选' in units[1]['text']


def test_model_cannot_drop_or_duplicate_original_records():
    api=engine();spans=message_spans([{'role':'user','content':'需求甲。'},{'role':'assistant','content':'尚未解决。'}])
    source='\n\n'.join(s['part'] for s in spans)
    def incomplete(prompt):return json.dumps({'segments':[{'task':'new','start_id':1,'end_id':1,'title':'任务','need_quote':'需求甲。'}]})
    with pytest.raises(ValueError,match='task_coverage_gap'):api.segment(source,incomplete,records=spans)


def test_unknown_continuation_target_is_rejected():
    api=engine();spans=message_spans([{'role':'user','content':'继续处理。'}])
    def false_link(prompt):return json.dumps({'segments':[{'task':'t99','start_id':1,'end_id':1,'title':'任务','need_quote':''}]})
    with pytest.raises(ValueError,match='task_unknown_reference'):
        api.segment(spans[0]['part'],false_link,records=spans)


def test_appended_conversation_reuses_old_task_and_only_reads_new_messages():
    api=engine();old=message_spans([{'role':'user','content':'需求甲。'}]);prefix=old[0]['part']
    seed=api.segment(prefix,lambda _:json.dumps({'segments':[{'task':'new','start_id':1,'end_id':1,'title':'甲','need_quote':'需求甲。'}]}),records=old)
    seed[0]['task_record']={'version':'complete-task-v1','marker':'previous narrative'}
    records=message_spans([{'role':'user','content':'需求甲。'},{'role':'user','content':'新需求乙。'}])
    def provider(prompt):
        rows=json.loads(prompt.split('本批原文：')[1]);assert len(rows)==1 and rows[0]['id']==2
        return json.dumps({'segments':[{'task':'new','start_id':2,'end_id':2,'title':'乙','need_quote':'新需求乙。'}]})
    units=api.segment('\n\n'.join(r['part'] for r in records),provider,records=records,seed=seed,processed_end=len(prefix))
    assert len(units)==2 and units[0]['task_record']['marker']=='previous narrative'


def test_exact_model_quote_includes_display_role_prefix_without_fabrication():
    spans=message_spans([{'role':'user','content':'增加写入开关。'}])
    # This is exactly the text offered in the numbered model input.
    response={'segments':[{'task':'new','start_id':1,'end_id':1,'title':'写入开关','need_quote':'用户：增加写入开关。'}]}
    units=engine().segment(spans[0]['part'],lambda _:json.dumps(response),records=spans)
    assert units[0]['need_quote']=='增加写入开关。'


def test_bad_window_is_corrected_once_and_checkpoint_keeps_previous_windows():
    records=message_spans([{'role':'user','content':'需求甲。'},{'role':'assistant','content':'甲的处理结果。'},{'role':'user','content':'需求乙。'}])
    checkpoints=[];calls=[]
    replies=iter([
        {'segments':[{'task':'new','start_id':1,'end_id':1,'title':'甲','need_quote':'需求甲。'}]},
        {'segments':[{'task':'t1','start_id':99,'end_id':99,'title':'甲','need_quote':''}]},
        {'segments':[{'task':'t1','start_id':2,'end_id':2,'title':'甲','need_quote':''}]},
        {'segments':[{'task':'new','start_id':3,'end_id':3,'title':'乙','need_quote':'需求乙。'}]}])
    def provider(prompt):calls.append(prompt);return json.dumps(next(replies))
    result=engine().segment('\n\n'.join(r['part'] for r in records),provider,records=records,max_records=1,on_progress=lambda x:checkpoints.append(x))
    assert len(result)==2 and len(calls)==4
    assert 'task_coverage_gap' in calls[2]
    assert len(checkpoints)==3 and checkpoints[-1]['processed_end']==records[-1]['end']
