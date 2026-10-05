"""History classification: preview is read-only; acceptance is explicit and versioned."""
import json
import pytest
from tests.test_history_qa_memory import service, archive
from evolvmem.knowledge_api import dispatch


def call(service, route, body=None):
    return dispatch(service, 'GET' if body is None else 'POST', 'history/organization' + route, body)


def test_queue_includes_unassigned_dialogue_and_material_but_not_global(service):
    a = archive(service, project='')
    item = service.knowledge().create({'title':'待整理资料', 'body':'此资料需要确认所属项目。', 'scope':'project', 'action':'draft'})
    service.knowledge().create({'title':'全局习惯', 'body':'这是跨项目通用习惯。', 'scope':'global', 'action':'draft'})
    keys = {r['key'] for r in call(service, '')['items']}
    assert keys == {f'archive:{a.id}', f'item:{item["id"]}'}


def test_preview_uses_saved_skill_and_never_assigns(service, monkeypatch):
    from evolvmem import kimi_hooks
    from evolvmem.pipeline_skills import read, save
    a = archive(service, project='')
    rules = read(service, 'ownership')
    save(service, 'ownership', {'expected_revision':rules['revision'], 'instructions':'看到棋子优先检查 shop。'})
    row = call(service, '')['items'][0]
    prompts = []
    def model(prompt, *args, **kwargs):
        prompts.append(prompt)
        return json.dumps({'items':[{'key':row['key'], 'project':'shop', 'reason':'与已保存规则一致'}]})
    monkeypatch.setattr(kimi_hooks, '_load_llm_config', lambda: object())
    monkeypatch.setattr(kimi_hooks, '_call_llm_with_retry', model)
    result = call(service, '/preview', {'items':[row]})
    assert result['items'][0]['project'] == 'shop'
    assert result['persisted'] == 0 and result['model_calls'] == 1
    assert '看到棋子优先检查 shop' in prompts[0]
    assert 'RAW_TOOL_LOG' not in prompts[0] and 'HIDDEN_REASONING' not in prompts[0]
    assert service.store.get_session_archive(a.id)['project'] == ''
    # Manual correction can differ from the model suggestion.
    saved = call(service, '/save', {'items':[{**row, 'project':'evo'}]})
    assert saved['succeeded'] == 1
    assert service.store.get_session_archive(a.id)['project'] == 'evo'
    assert not call(service, '')['items']


def test_batch_saves_valid_rows_retains_conflicts_and_does_not_publish(service):
    a = archive(service, project='')
    kb = service.knowledge()
    item = kb.create({'title':'资料', 'body':'归属需要核对，内容还没有确认入库。', 'scope':'project', 'action':'draft'})
    rows = call(service, '')['items']
    kb.update(item['id'], {'expected_revision':item['revision'], 'body':'已经修改，旧的分类预览必须重新核对。'})
    saved = call(service, '/save', {'items':[{**r,'project':'evo'} for r in rows]})
    assert (saved['succeeded'], saved['failed']) == (1, 1)
    assert saved['items'][1 if rows[0]['key'].startswith('archive:') else 0]['error'] == 'revision_conflict'
    assert kb.detail(item['id'])['project'] == ''
    assert kb.detail(item['id'])['status'] == 'candidate'
    # An old request must never move a now-confirmed archive to a different project.
    stale = next(r for r in rows if r['key'] == f'archive:{a.id}')
    assert call(service, '/save', {'items':[{**stale,'project':'shop'}]})['failed'] == 1
    assert service.store.get_session_archive(a.id)['project'] == 'evo'


def test_invalid_model_project_is_left_for_manual_review(service, monkeypatch):
    from evolvmem import kimi_hooks
    a = archive(service, project='')
    row = call(service, '')['items'][0]
    monkeypatch.setattr(kimi_hooks, '_load_llm_config', lambda: object())
    monkeypatch.setattr(kimi_hooks, '_call_llm_with_retry', lambda *a, **k: json.dumps({'items':[{'key':row['key'],'project':'invented'}]}))
    assert call(service, '/preview', {'items':[row]})['items'][0]['project'] == ''
    assert call(service, '/save', {'items':[{**row,'project':'invented'}]})['failed'] == 1
    assert service.store.get_session_archive(a.id)['project'] == ''


def test_current_archive_assignment_does_not_resurface_old_snapshot(service):
    from evolvmem.session_archive import SessionArchiver
    archiver = SessionArchiver(service.config, service.store)
    payload = json.dumps({'messages':[{'role':'user','content':'这是一份需要分类的会话历史。'}]})
    old = archiver.archive_session('', 'codex', 'same-session:old', payload)
    head = archiver.archive_session('', 'codex', 'same-session:new', payload)
    rows = call(service, '')['items']
    assert [r['key'] for r in rows] == [f'archive:{head.id}']
    assert call(service, '/save', {'items':[{**rows[0],'project':'evo'}]})['succeeded'] == 1
    assert call(service, '')['items'] == []
    assert service.store.get_session_archive(old.id)['project'] == ''


def test_lan_archive_confirmation_queues_existing_extraction_and_backfill(service):
    from evolvmem.lan_capture import LanCapture
    from types import SimpleNamespace
    LanCapture(SimpleNamespace(context_service=service, config=service.config))
    a = archive(service, project='')
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute("INSERT INTO lan_session_uploads(device_id,session_id,sha256,project,declared_project,received_at,total_bytes,archive_id,extraction_status,backfill_status) VALUES('fixture-device','fixture-session','abc','','',0,1,?,'unassigned','candidate')", (a.id,))
        conn.execute("INSERT INTO lan_session_heads(device_id,session_id,sha256) VALUES('fixture-device','fixture-session','abc')")
    row = call(service, '')['items'][0]
    assert call(service, '/save', {'items':[{**row,'project':'evo'}]})['succeeded'] == 1
    upload = conn.execute('SELECT * FROM lan_session_uploads').fetchone()
    assert (upload['project'],upload['extraction_status'],upload['backfill_status']) == ('evo','pending','pending')
