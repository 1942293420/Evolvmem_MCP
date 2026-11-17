"""Source evidence is bound to native local events, not caller assertions."""
import json
import os
import time

import pytest

from evolvmem.experience_sources import ExperienceSourceResolver


def _jsonl(path, *events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        ''.join(json.dumps(event, ensure_ascii=False) + '\n' for event in events),
        encoding='utf-8',
    )
    return path


def test_codex_explicit_line_accepts_tool_result_and_rejects_assistant(tmp_path):
    root = tmp_path / 'codex'
    transcript = _jsonl(
        root / '2026' / '09' / 'rollout-task-9.jsonl',
        {'type': 'response_item', 'payload': {
            'type': 'function_call_output', 'output': 'pytest 7 passed'}},
        {'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': 'pytest 7 passed'}]}},
    )
    resolver = ExperienceSourceResolver(codex_roots=(root,))

    result = resolver.resolve(
        source_kind='tool_result', source_ref=f'{transcript.resolve()}#1',
        task_id='task-9', quote='pytest 7 passed')
    assert result.source_ref == f'{transcript.resolve()}#1'
    assert result.snapshot == 'pytest 7 passed'
    assert len(result.digest) == 64
    with pytest.raises(ValueError, match='native tool event'):
        resolver.resolve(
            source_kind='tool_result', source_ref=f'{transcript.resolve()}#2',
            task_id='task-9', quote='pytest 7 passed')
    with pytest.raises(ValueError, match='task_id does not match'):
        resolver.resolve(
            source_kind='tool_result', source_ref=f'{transcript.resolve()}#1',
            task_id='forged-independent-task', quote='pytest 7 passed')


def test_codex_automatic_resolution_only_searches_matching_session(tmp_path):
    root = tmp_path / 'codex'
    _jsonl(root / 'rollout-other-task.jsonl', {
        'type': 'response_item',
        'payload': {'type': 'function_call_output', 'output': 'same quote'},
    })
    wanted = _jsonl(root / '2026' / 'rollout-task-42.jsonl',
                    {'type': 'response_item', 'payload': {
                        'type': 'function_call_output',
                        'output': 'same quote and verified'}})
    resolver = ExperienceSourceResolver(codex_roots=(root,))

    result = resolver.resolve(
        source_kind='tool_result', source_ref='', task_id='task-42',
        quote='same quote')
    assert result.source_ref == f'{wanted.resolve()}#1'


def test_kimi_native_user_and_tool_events_are_supported(tmp_path):
    root = tmp_path / 'kimi-sessions'
    wire = _jsonl(
        root / 'wd_demo_hash' / 'session_kimi-1' / 'agents' / 'main' / 'wire.jsonl',
        {'type': 'turn.prompt',
         'input': [{'type': 'text', 'text': '用户确认页面结果正确'}]},
        {'type': 'context.append_loop_event', 'event': {
            'type': 'tool.result', 'result': {'output': '构建检查通过'}}},
        {'type': 'context.append_loop_event', 'event': {
            'type': 'content.part', 'part': {
                'type': 'text', 'text': '用户确认页面结果正确'}}},
    )
    resolver = ExperienceSourceResolver(kimi_roots=(root,))

    user = resolver.resolve(
        source_kind='user_confirmation', source_ref='', task_id='kimi-1',
        quote='用户确认页面结果正确')
    tool = resolver.resolve(
        source_kind='tool_result', source_ref=f'{wire.resolve()}#2',
        task_id='kimi-1', quote='构建检查通过')
    assert user.source_ref == f'{wire.resolve()}#1'
    assert tool.source_ref == f'{wire.resolve()}#2'
    with pytest.raises(ValueError, match='native user event'):
        resolver.resolve(
            source_kind='user_confirmation', source_ref=f'{wire.resolve()}#3',
            task_id='kimi-1', quote='用户确认页面结果正确')


def test_dsh_native_user_and_tool_result_events_are_supported(tmp_path):
    root = tmp_path / 'dsh-sessions'
    transcript = _jsonl(
        root / 'wd-demo' / 'session-dsh-1' / 'session.jsonl',
        {'type': 'user', 'message': {
            'role': 'user', 'content': '用户验收结果正确'}},
        {'type': 'user', 'message': {'role': 'user', 'content': [{
            'type': 'tool_result', 'content': 'DSH 测试 12 passed'}]}},
    )
    resolver = ExperienceSourceResolver(dsh_roots=(root,))

    user = resolver.resolve(
        source_kind='user_confirmation', source_ref='', task_id='dsh-1',
        quote='用户验收结果正确')
    tool = resolver.resolve(
        source_kind='tool_result', source_ref=f'{transcript.resolve()}#2',
        task_id='dsh-1', quote='DSH 测试 12 passed')
    assert user.source_ref == f'{transcript.resolve()}#1'
    assert tool.source_ref == f'{transcript.resolve()}#2'


def test_fix_record_quote_must_be_in_verification_section(tmp_path):
    fix_root = tmp_path / 'fix-records' / 'records'
    record = fix_root / '2026-09-05-pagination.md'
    record.parent.mkdir(parents=True)
    record.write_text(
        '# 分页修复\n\n## 修复内容\n这里也写了 7 passed\n\n'
        '## 验证\npytest：7 passed；人工翻页正确\n\n## 遗留事项\n无\n',
        encoding='utf-8')
    resolver = ExperienceSourceResolver(fix_root=fix_root)

    result = resolver.resolve(
        source_kind='historical_record', source_ref=str(record.resolve()),
        task_id='historical-1', quote='pytest：7 passed')
    assert result.source_ref == str(record.resolve())
    with pytest.raises(ValueError, match='验证 section'):
        resolver.resolve(
            source_kind='historical_record', source_ref=str(record.resolve()),
            task_id='historical-1', quote='这里也写了 7 passed')


def test_arbitrary_file_and_missing_quote_are_rejected(tmp_path):
    root = tmp_path / 'codex'
    outside = _jsonl(tmp_path / 'arbitrary.jsonl', {
        'type': 'response_item',
        'payload': {'type': 'function_call_output', 'output': 'passed'},
    })
    resolver = ExperienceSourceResolver(codex_roots=(root,))

    with pytest.raises(ValueError, match='approved transcript roots'):
        resolver.resolve(source_kind='tool_result',
                         source_ref=f'{outside.resolve()}#1',
                         task_id='task-1', quote='passed')
    with pytest.raises(ValueError, match='quote required'):
        resolver.resolve(source_kind='tool_result',
                         source_ref=f'{outside.resolve()}#1',
                         task_id='task-1', quote='')


def test_codex_code_mode_output_is_native_evidence(tmp_path):
    root=tmp_path/'codex'
    transcript=_jsonl(root/'rollout-task-code.jsonl', {
        'type':'response_item','payload':{
            'type':'custom_tool_call_output','call_id':'call-1','output':'actual checker: 7 passed'}})
    source=ExperienceSourceResolver(codex_roots=(root,)).resolve(
        source_kind='tool_result',source_ref=f'{transcript}#1',task_id='task-code',quote='actual checker: 7 passed')
    assert source.snapshot == 'actual checker: 7 passed'


NATIVE_ID = '01a072da-f744-7781-800c-d7366b2cb509'
OTHER_ID = '01a072da-f744-7781-800c-d7366b2cb510'


def _native_codex(root, task_id=NATIVE_ID, *, messages=None):
    return _jsonl(
        root / f'rollout-2026-09-06T02-35-47-{task_id}.jsonl',
        {'type': 'session_meta', 'payload': {'id': task_id}},
        *(messages or [_codex_user('页面已经按要求验收通过')]),
    )


def _codex_user(message, *, mirror=False):
    if mirror:
        return {'type': 'event_msg', 'payload': {
            'type': 'user_message', 'message': message}}
    return {'type': 'response_item', 'payload': {
        'type': 'message', 'role': 'user',
        'content': [{'type': 'input_text', 'text': message}]}}


@pytest.mark.parametrize('task_id', ['current', '', None])
def test_current_task_resolves_unique_recent_native_message(tmp_path, task_id):
    root = tmp_path / 'codex'
    transcript = _native_codex(root)
    source = ExperienceSourceResolver(codex_roots=(root,), kimi_roots=(), dsh_roots=()).resolve(
        source_kind='user_confirmation', source_ref='', task_id=task_id,
        quote='页面已经按要求验收通过')
    assert source.task_id == NATIVE_ID
    assert source.source_ref == f'{transcript}#2'


def test_explicit_native_ref_binds_task_without_current_environment(tmp_path):
    root = tmp_path / 'codex'
    transcript = _native_codex(root)
    resolver = ExperienceSourceResolver(codex_roots=(root,))
    source = resolver.resolve(
        source_kind='user_confirmation', source_ref=f'{transcript}#2',
        task_id='current', quote='页面已经按要求验收通过')
    assert source.task_id == NATIVE_ID
    with pytest.raises(ValueError, match='task_id does not match'):
        resolver.resolve(
            source_kind='user_confirmation', source_ref=f'{transcript}#2',
            task_id=NATIVE_ID[:12], quote='页面已经按要求验收通过')


def test_current_quote_rejects_two_native_sessions(tmp_path):
    root = tmp_path / 'codex'
    _native_codex(root)
    _native_codex(root, OTHER_ID)
    with pytest.raises(ValueError, match='multiple source events'):
        ExperienceSourceResolver(codex_roots=(root,), kimi_roots=(), dsh_roots=()).resolve(
            source_kind='user_confirmation', source_ref='', task_id='current',
            quote='页面已经按要求验收通过')


@pytest.mark.parametrize('reverse', [False, True])
def test_codex_mirrored_user_events_share_one_canonical_source(tmp_path, reverse):
    root = tmp_path / 'codex'
    messages = [_codex_user('页面验收通过'), _codex_user('页面验收通过', mirror=True)]
    if reverse:
        messages.reverse()
    transcript = _native_codex(root, messages=messages)
    resolver = ExperienceSourceResolver(codex_roots=(root,), kimi_roots=(), dsh_roots=())
    resolved = [resolver.resolve(
        source_kind='user_confirmation', source_ref=ref, task_id='current',
        quote='页面验收通过') for ref in ('', f'{transcript}#2', f'{transcript}#3')]
    assert len({source.source_ref for source in resolved}) == 1
    assert resolved[0].source_ref == f'{transcript}#{3 if reverse else 2}'
    assert len({source.digest for source in resolved}) == 1


def test_codex_repeated_user_message_is_ambiguous_even_with_mirrors(tmp_path):
    root = tmp_path / 'codex'
    pair = [_codex_user('页面验收通过'), _codex_user('页面验收通过', mirror=True)]
    transcript = _native_codex(root, messages=pair + pair)
    resolver = ExperienceSourceResolver(codex_roots=(root,), kimi_roots=(), dsh_roots=())
    with pytest.raises(ValueError, match='multiple source events'):
        resolver.resolve(source_kind='user_confirmation', source_ref='',
                         task_id='current', quote='页面验收通过')
    assert resolver.resolve(
        source_kind='user_confirmation', source_ref=f'{transcript}#5',
        task_id='current', quote='页面验收通过').source_ref == f'{transcript}#4'


@pytest.mark.parametrize('age,maximum', [(7200, 12), (60, 1)])
def test_recent_discovery_is_bounded_and_does_not_guess_workspace_ids(tmp_path, age, maximum):
    root = tmp_path / 'codex'
    older = _native_codex(root, OTHER_ID)
    now = time.time()
    os.utime(older, (now - age, now - age))
    wanted = _native_codex(root)
    _jsonl(root / 'rollout-ws_fake.jsonl', _codex_user('页面已经按要求验收通过'))
    resolver = ExperienceSourceResolver(
        codex_roots=(root,), kimi_roots=(), dsh_roots=(),
        recent_window_seconds=3600, max_recent_transcripts=maximum)
    result = resolver.resolve(source_kind='user_confirmation', source_ref='',
                              task_id='current', quote='页面已经按要求验收通过')
    assert result.task_id == NATIVE_ID
    assert result.source_ref == f'{wanted}#2'


def test_workspace_identity_cannot_bind_as_native_task(tmp_path):
    root = tmp_path / 'codex'
    path = _jsonl(root / 'rollout-ws_fake.jsonl', _codex_user('验收完成'))
    with pytest.raises(ValueError, match='native task_id'):
        ExperienceSourceResolver(codex_roots=(root,)).resolve(
            source_kind='user_confirmation', source_ref=f'{path}#1',
            task_id='ws_fake', quote='验收完成')


def test_codex_home_is_the_default_native_session_root(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'custom-codex'))
    root = tmp_path / 'custom-codex' / 'sessions'
    path = _native_codex(root)
    source = ExperienceSourceResolver(kimi_roots=(), dsh_roots=()).resolve(
        source_kind='user_confirmation', source_ref='', task_id='current',
        quote='页面已经按要求验收通过')
    assert source.source_ref == f'{path}#2'


def test_codex_mirror_pairing_requires_equal_complete_messages(tmp_path):
    root = tmp_path / 'codex'
    _native_codex(root, messages=[
        _codex_user('第一次页面验收通过'),
        _codex_user('第二次页面验收通过', mirror=True),
    ])
    with pytest.raises(ValueError, match='multiple source events'):
        ExperienceSourceResolver(codex_roots=(root,), kimi_roots=(), dsh_roots=()).resolve(
            source_kind='user_confirmation', source_ref='', task_id='current',
            quote='页面验收通过')


def test_native_metadata_cannot_contradict_canonical_path(tmp_path):
    root = tmp_path / 'codex'
    transcript = _native_codex(root, messages=[_codex_user('页面验收通过')])
    lines = transcript.read_text().splitlines()
    lines[0] = json.dumps({'type': 'session_meta', 'payload': {'id': OTHER_ID}})
    transcript.write_text('\n'.join(lines) + '\n')
    with pytest.raises(ValueError, match='session metadata'):
        ExperienceSourceResolver(codex_roots=(root,)).resolve(
            source_kind='user_confirmation', source_ref=f'{transcript}#2',
            task_id='current', quote='页面验收通过')


@pytest.mark.parametrize('adapter,relative,task_id,event', [
    ('kimi', 'wd_demo/session_kimi-42/agents/main/wire.jsonl', 'kimi-42',
     {'type': 'turn.prompt', 'input': [{'type': 'text', 'text': '验收完成'}]}),
    ('dsh', 'wd_demo/session-dsh-42/session.jsonl', 'dsh-42',
     {'type': 'user', 'message': {'role': 'user', 'content': '验收完成'}}),
    ('kimi', f'wd_demo/{NATIVE_ID}/agents/main/wire.jsonl', NATIVE_ID,
     {'type': 'turn.prompt', 'input': [{'type': 'text', 'text': '验收完成'}]}),
    ('dsh', f'wd_demo/{NATIVE_ID}/session.jsonl', NATIVE_ID,
     {'type': 'user', 'message': {'role': 'user', 'content': '验收完成'}}),
])
def test_current_binding_supports_other_native_adapter_paths(tmp_path, adapter, relative, task_id, event):
    root = tmp_path / adapter
    path = _jsonl(root / relative, event)
    roots = dict(codex_roots=(), kimi_roots=(), dsh_roots=())
    roots[f'{adapter}_roots'] = (root,)
    resolver = ExperienceSourceResolver(**roots)
    for ref in ('', f'{path}#1'):
        source = resolver.resolve(source_kind='user_confirmation', source_ref=ref,
                                  task_id='current', quote='验收完成')
        assert source.task_id == task_id
        assert source.source_ref == f'{path}#1'


def test_current_does_not_infer_native_task_from_arbitrary_session_directory(tmp_path):
    root = tmp_path / 'kimi'
    path = _jsonl(root / 'wd_demo' / 'arbitrary' / 'agents' / 'main' / 'wire.jsonl',
                  {'type': 'turn.prompt', 'input': [{'type': 'text', 'text': '验收完成'}]})
    with pytest.raises(ValueError, match='canonical native task_id'):
        ExperienceSourceResolver(kimi_roots=(root,)).resolve(
            source_kind='user_confirmation', source_ref=f'{path}#1',
            task_id='current', quote='验收完成')
