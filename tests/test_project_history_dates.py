"""Historical date regressions: use source dates, never migration timestamps."""
from tests.test_project_archive_store import save_manual_event
from types import SimpleNamespace
import pytest
from evolvmem.context_models import ContextContentType, ContextItemDraft, ContextLayers, ContextStatus
from evolvmem.context_store import ContextStore, _now_iso
from evolvmem.knowledge_restructure import migrate_batch
from evolvmem.project_archive_store import ProjectArchiveStore, ProjectEventDraft


def legacy(store, tag, project='ai_purchase', occurred_at=None):
    item = store.create_item(ContextItemDraft(
        identity_key=f'project:jiangli:progress:log:{tag}',
        content_type=ContextContentType.SESSION_SUMMARY,
        layers=ContextLayers(l0=tag, l1=tag, l2=tag, generator='test'),
        project=project, occurred_at=occurred_at, status=ContextStatus.ACTIVE,
    ))
    with store.transaction():
        store._connection().execute(
            "INSERT OR IGNORE INTO context_project_registry(project,created_at,updated_at) VALUES(?,'now','now')", (project,))
        store._connection().execute(
            "INSERT INTO context_project_resolutions(item_id,resolution_state,decision_source,review_state,resolved_project,created_at,updated_at) "
            "VALUES(?,'resolved','human','accepted',?,'now','now')", (item.id, project))
    return item.id


def register(store):
    with store.transaction():
        store._connection().execute(
            'INSERT INTO context_project_registry(project,display_name,created_at,updated_at) VALUES(?,?,?,?)',
            ('ai_purchase', 'AI采购', _now_iso(), _now_iso()))


@pytest.mark.parametrize(('tag', 'expected'), [
    ('2026-07-20-2328', '2026-07-20'),
    ('2026-08-05-1144', '2026-08-05'),
    ('2026-08-31-1730', '2026-08-31'),
])
def test_migration_keeps_literal_source_day_without_inventing_timezone(test_config, monkeypatch, tag, expected):
    with ContextStore(test_config) as store:
        register(store)
        item_id = legacy(store, tag)
        monkeypatch.setattr('evolvmem.knowledge_restructure._call_provider',
                            lambda *a: pytest.fail('historical dates must not call AI'))
        migrate_batch(SimpleNamespace(store=store, config=test_config))
        archives = ProjectArchiveStore(store)
        event = archives.events('ai_purchase')[0]
        assert event['occurred_at'] == expected
        assert event['occurred_at_precision'] == 'date'
        assert event['recorded_at'] != expected
        assert store.get_item(item_id, include_layers=False).occurred_at is None
        archive = archives.get('ai_purchase')
        assert archive['started_at'] == expected
        assert archive['display_name'] == 'AI采购'
        assert '# AI采购项目摘要' in archive['summary']


def test_unknown_occurrence_does_not_claim_import_date_is_project_start(test_config):
    with ContextStore(test_config) as store:
        archives = ProjectArchiveStore(store)
        save_manual_event(archives, ProjectEventDraft(project='unknown', event_type='milestone', title='旧摘要', content='日期不明'))
        archive = archives.rebuild('unknown')
        assert archive['started_at'] is None
        assert archive['development_days'] is None
        assert '发生时间未确认' in archive['summary']
        assert '最早有日期记录：未确认' in archive['summary']
        event = archives.events('unknown')[0]
        assert event['occurred_at_precision'] == 'unknown'


def test_get_corrects_stale_derived_dates_without_writing(test_config):
    with ContextStore(test_config) as store:
        register(store)
        archives = ProjectArchiveStore(store)
        save_manual_event(archives, ProjectEventDraft(project='ai_purchase', event_type='milestone', title='旧摘要', content='日期不明'))
        archives.rebuild('ai_purchase')
        with store.transaction():
            store._connection().execute("UPDATE project_archives SET started_at='2026-10-28',display_name='ai_purchase',summary='开始时间：2026-10-28'")
        before = store._connection().total_changes
        archive = archives.get('ai_purchase')
        assert archive['started_at'] is None
        assert archive['display_name'] == 'AI采购'
        assert '开始时间：2026-10-28' not in archive['summary']
        assert store._connection().total_changes == before


def test_known_manual_date_is_kept_and_ambiguous_keys_remain_unknown(test_config):
    with ContextStore(test_config) as store:
        legacy(store, '2026-07-20-2328', occurred_at='2026-07-19T09:00:00+08:00')
        for tag in ('2026-02-30-1030', '2026-07-20-2560', '2026-07-20-2328-extra', 'undated'):
            legacy(store, tag)
        migrate_batch(SimpleNamespace(store=store, config=test_config))
        dates = {e['title']: e['occurred_at'] for e in ProjectArchiveStore(store).events('ai_purchase')}
        assert dates['2026-07-20-2328'] == '2026-07-19 01:00:00'
        assert all(dates[tag] is None for tag in ('2026-02-30-1030', '2026-07-20-2560', '2026-07-20-2328-extra', 'undated'))
