"""Cross-batch acceptance probes and the non-mutating historical preview."""
import sqlite3
import pytest

from tests.test_knowledge_identity import config, service, material, save, rebuild, ingest, view
from tests.test_cross_batch_context import OTHER_SESSION
from scripts.preview_project_knowledge import preview, readonly_store, render_html


@pytest.mark.xfail(strict=True, reason='A4: the old decision is protected, but a mixed task still asks to review the whole card')
def test_a4_only_the_new_requirement_is_presented_for_review(service):
    from tests.test_cross_batch_context import craft, SESSION
    from evolvmem import conversation_tasks, auto_organization as org
    first, _ = material(service, text='Demo 的运行系统是 Linux。')
    original = save(service, first['id'])
    extra = craft(service, session=SESSION, start=5, end=8,
                  text='新增要求：Other 项目也要支持 Windows 部署。')
    task = conversation_tasks.enqueue(service, [first['source_key'], f'archive:{extra.id}'])['items'][0]
    rebuild(service, task['id'])
    conversation_tasks.protect_prior_decisions(service, org._task_row(service, task['id']))
    pending = [u for u in view(service, task['id']) if u['decision'] == 'review']
    assert service.knowledge().detail(original['item_id'])['project'] == 'demo'
    assert pending and any('新增要求' in u['text'] for u in pending)
    assert all('Demo 的运行系统是 Linux。' not in u['text'] for u in pending)


def test_preview_preserves_original_ids_display_sources_and_decisions(service):
    task, _ = material(service)
    saved = save(service, task['id'])
    before = service.knowledge().detail(saved['item_id'])
    report = preview(service.config.db_path, 'demo')
    assert report['sample_item_ids'] == [saved['item_id']]
    assert report['original_ids_display_sources_decisions_unchanged']
    assert report['writes'] == report['model_calls'] == 0
    assert report['legacy_materials'] == 1 and report['extracted_facts'] == 0
    assert report['items'][0]['human_decisions'][0]['decision']['action'] == 'include'
    assert report['items'][0]['sources']
    assert report['items'][0]['original_bytes_checked'] is False
    assert service.knowledge().detail(saved['item_id']) == before


def test_preview_connection_cannot_write_or_create_a_missing_database(service, tmp_path):
    with readonly_store(service.config.db_path) as store:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            store._connection().execute("UPDATE knowledge_objects SET retired=1")
    missing = tmp_path / 'nonexistent.db'
    with pytest.raises(sqlite3.OperationalError):
        preview(missing, 'demo')
    assert not missing.exists()


def test_preview_stays_within_selected_project_and_limit(service):
    first, _ = material(service)
    saved = save(service, first['id'])
    other, _ = material(service, session=OTHER_SESSION, text='Other 的服务器只用于测试。')
    foreign = save(service, other['id'], project='other')
    report = preview(service.config.db_path, 'demo', item_ids=[foreign['item_id']])
    assert report['items'] == []
    assert preview(service.config.db_path, 'demo', limit=1)['sample_item_ids'] == [saved['item_id']]
    with pytest.raises(ValueError, match='limit'):
        preview(service.config.db_path, 'demo', limit=51)


def test_preview_distinguishes_unknown_legacy_facts_from_no_conflict(service):
    task, _ = material(service, text='Demo 在生产环境使用 Linux。')
    save(service, task['id'])
    report = preview(service.config.db_path, 'demo')
    assert report['known_disagreements'] == 0
    assert report['extracted_facts'] == 0
    assert '尚不能判断' in ''.join(report['limitations'])


def test_preview_includes_fact_versions_without_including_foreign_ownership(service):
    from tests.test_knowledge_facts import add
    task, _ = add(service, 'Windows')
    save(service, task['id'])
    other, _ = add(service, 'Linux', session=OTHER_SESSION)
    save(service, other['id'], project='other')
    report = preview(service.config.db_path, 'demo')
    assert report['extracted_facts'] == 1 and report['known_disagreements'] == 1
    item = report['items'][0]
    assert [v['value'] for v in item['versions']] == ['Windows']
    assert item['alternatives'][0]['archive_state'] == 'not_in_project'


def test_three_retries_reword_split_merge_do_not_add_decisions_or_sources(service):
    task, _ = material(service)
    original = save(service, task['id'])
    before = service.store.list_item_sources(original['item_id'])
    count = lambda: service.store._connection().execute('SELECT COUNT(*) FROM knowledge_decisions').fetchone()[0]
    decisions = count()
    for i in range(3):
        rebuild(service, task['id'], split=True, title=f'重新表述 {i}')
        ingest(service, task['id'])
        rebuilt = rebuild(service, task['id'], title=f'合并展示 {i}')
        ingest(service, task['id'])
        assert all(u['decision'] == 'manual' and u['knowledge_id'] == original['knowledge_id'] for u in rebuilt)
    assert count() == decisions
    assert service.store.list_item_sources(original['item_id']) == before


def test_preview_html_escapes_source_content(service):
    task, _ = material(service, text='Demo 示例原文包含 <script>alert(1)</script>。')
    save(service, task['id'])
    page = render_html(preview(service.config.db_path, 'demo'))
    assert '<script>' not in page
    assert '&lt;script&gt;' in page
