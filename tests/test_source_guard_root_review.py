"""Final source-retirement acceptance: completed tasks and distant versions."""
from tests.test_source_eligibility_repair import (
    config, service, payload_for, windows_external, auto_new, discover, tasks,
    _insert_failed_task, source_revision,
)
from tests.unit_model_fixture import model_for, run_worker
from evolvmem.session_archive import SessionArchiver
from evolvmem.auto_organization import reconcile_sources, unit_views


def test_completed_automatic_history_is_retired_when_full_snapshot_advances(service, config, monkeypatch):
    archiver = SessionArchiver(config, service.store)
    auto_new(service)
    old = archiver.archive_session('', 'codex', windows_external(['device', 'done-session'], 'a'*64),
                                  payload_for('demo 项目必须保留导入来源。'))
    discover(service)
    run_worker(service, monkeypatch, model_for(service, projects={'demo'}))
    before = tasks(service)[f'archive:{old.id}']
    assert before['status'] == 'completed'
    units = unit_views(service, before['id'])
    assert units and all(u['decision'] != 'manual' for u in units)
    newer = archiver.archive_session('', 'codex', windows_external(['device', 'done-session'], 'b'*64),
                                    payload_for('demo 项目必须保留导入来源和时间。'))
    discover(service)
    assert tasks(service)[f'archive:{old.id}']['status'] == 'superseded'


def test_legacy_newer_version_is_not_hidden_by_twenty_unrelated_archives(service, config):
    archiver = SessionArchiver(config, service.store)
    auto_new(service)
    old = archiver.archive_session('', 'codex', windows_external(['device', 'distant'], 'a'*64),
                                  payload_for('旧来源正文。'))
    _insert_failed_task(service, f'archive:{old.id}', source_revision(service, f'archive:{old.id}'))
    for i in range(25):
        archiver.archive_session('', 'codex', windows_external(['device', f'unrelated-{i}'], 'c'*64),
                                 payload_for('其他会话。'))
    newer = archiver.archive_session('', 'codex', windows_external(['device', 'distant'], 'b'*64),
                                    payload_for('新来源正文。'))
    for _ in range(3):
        reconcile_sources(service, limit=2)
    assert tasks(service)[f'archive:{old.id}']['status'] == 'superseded'


def test_independent_source_keeps_shared_history_active_after_task_retires(service, config, monkeypatch):
    from evolvmem.auto_organization import _retire_task
    from evolvmem.context_store import _now_iso
    archiver = SessionArchiver(config, service.store)
    auto_new(service)
    old = archiver.archive_session('', 'codex', windows_external(['device', 'shared-source'], 'a'*64),
                                  payload_for('demo 项目必须保留导入来源。'))
    discover(service)
    run_worker(service, monkeypatch, model_for(service, projects={'demo'}))
    task = tasks(service)[f'archive:{old.id}']
    unit = unit_views(service, task['id'])[0]
    other = archiver.archive_session('demo', 'kimi', 'independent-chat', payload_for('独立的业务依据。'))
    with service.store.transaction():
        service.store._connection().execute(
            "INSERT INTO context_sources(item_id,archive_id,source_kind,source_ref,extraction_version,created_at) "
            "VALUES(?,?,'session','independent-chat','synthetic',?)", (unit['item_id'], other.id, _now_iso()))
    assert _retire_task(service, task['id'], status='superseded', reason='source_superseded')
    assert service.knowledge().detail(unit['item_id'])['status'] == 'active'


def test_retired_shared_outputs_do_not_spend_the_next_reconcile_budget(service, config, monkeypatch):
    from evolvmem.context_store import _now_iso
    archiver = SessionArchiver(config, service.store)
    auto_new(service)
    family = ['device', 'settled-shared']
    old = archiver.archive_session('', 'codex', windows_external(family, 'a'*64),
                                  payload_for('demo 项目必须保留导入来源。'))
    discover(service)
    run_worker(service, monkeypatch, model_for(service, projects={'demo'}))
    task = tasks(service)[f'archive:{old.id}']
    iid = unit_views(service, task['id'])[0]['item_id']
    other = archiver.archive_session('demo', 'kimi', 'other-provenance', payload_for('独立依据。'))
    with service.store.transaction():
        service.store._connection().execute(
            "INSERT INTO context_sources(item_id,archive_id,source_kind,source_ref,extraction_version,created_at) "
            "VALUES(?,?,'session','other-provenance','synthetic',?)", (iid, other.id, _now_iso()))
    archiver.archive_session('', 'codex', windows_external(family, 'b'*64), payload_for('demo 新版本。'))
    assert reconcile_sources(service)['superseded'] == 1
    assert service.knowledge().detail(iid)['status'] == 'active'
    assert reconcile_sources(service)['examined'] == 0, 'shared valid knowledge must not keep retired work scanning forever'
