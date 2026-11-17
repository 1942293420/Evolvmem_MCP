from __future__ import annotations

import json
import uuid

import pytest

from evolvmem.history_pg.candidate_reader import PgHistoryCandidateReader
from evolvmem.history_pg.config import PgHistoryConfig
from evolvmem.history_pg.history_web_pg import dispatch_pg_history_api
from evolvmem.history_pg.schema import apply_history_schemas, ensure_databases_exist
from evolvmem.history_pg.workspace_paths import normalize_workspace_path, workspace_key, workspace_name
from evolvmem.history_pg.workspace_views import WorkspaceHistoryReader, _response_time
from tests.stage5.conftest import STAGE5_PG_CONTAINER, STAGE5_PG_PORT, STAGE5_PW_FILE


def test_workspace_paths_are_client_lexical_and_keep_real_generic_names():
    assert normalize_workspace_path(r'\\?\C:\Users\ASUS\new-chat' + '\\') == r'C:\Users\ASUS\new-chat'
    assert workspace_name(r'C:\Users\ASUS\new-chat') == 'new-chat'
    assert workspace_key(r'C:\Work\App') == workspace_key('c:/work/app/')
    assert workspace_key('/srv/App') != workspace_key('/srv/app')
    assert workspace_key('C:/one/App') != workspace_key('C:/two/App')
    assert normalize_workspace_path(r'\\server\share\App') == r'\\server\share\App'
    for invalid in ('relative/folder', 'C:relative', 'folder\x00bad', '', None, {}, 3):
        assert normalize_workspace_path(invalid) == ''


def test_archive_time_keeps_source_precision_and_only_known_utc_is_qualified():
    for original in ('', '2026-11-17', '20261117', 'invalid', '2026-02-30 10:00:00', '2026-11-17T04:00:00Z'):
        assert _response_time(original, 'archive_fallback') == original
    assert _response_time('2026-11-17 04:00:00', 'archive_fallback') == '2026-11-17T04:00:00+00:00'
    assert _response_time('2026-11-17 04:00:00', 'recorded') == '2026-11-17 04:00:00'


@pytest.fixture
def seeded(tmp_path):
    cfg = PgHistoryConfig(
        host='127.0.0.1', port=STAGE5_PG_PORT,
        admin_password=STAGE5_PW_FILE.read_text().strip(),
        user_history_dbs={'workspace-test': 'evolvmem_history_migration'},
        allow_loopback_disable_ssl=True, container_name=STAGE5_PG_CONTAINER, cas_root=tmp_path,
    )
    ensure_databases_exist(cfg)
    apply_history_schemas(cfg)
    prefix = 'wsv-' + uuid.uuid4().hex[:10]
    proj = prefix + '-business'
    bids = {name: prefix + '-' + name for name in ('a', 'case', 'other', 'unknown', 'mixed', 'bad', 'deleted', 'oldtrash')}
    paths = {'a': r'C:\One\App', 'case': 'c:/one/app', 'other': r'C:\Two\App', 'deleted': r'C:\Secret\Hidden', 'oldtrash': r'C:\Old\Deleted'}
    with cfg.connect(cfg.history_migration_db) as conn:
        conn.execute('INSERT INTO em_history.projects(project) VALUES (%s)', (proj,))
        for name, bid in bids.items():
            bp = proj + '-trash' if name == 'oldtrash' else proj
            conn.execute(
                "INSERT INTO em_history.blocks(block_id,adapter,title,message_count,turn_count,projects,observed_first_at,observed_last_at) VALUES (%s,'codex',%s,2,1,%s,'2026-11-16','2026-11-17')",
                (bid, 'needle ' + name, json.dumps([bp])),
            )
            conn.execute('INSERT INTO em_history.block_projects(block_id,project) VALUES (%s,%s)', (bid, bp))
            conn.execute("INSERT INTO em_history.messages(block_id,ordinal,role,content) VALUES (%s,1,'user',%s)", (bid, 'needle ' + name))
            path = paths.get(name, '')
            reason = 'mixed' if name == 'mixed' else 'verified' if path else 'unknown'
            val = {'version': 1, 'workspace_path': path, 'reason': reason, 'source_archive_ids': '[]', 'block_sha256': ''}
            if name == 'mixed':
                val['workspace_path'] = r'C:\One\App'
            text = '{bad-json' if name == 'bad' else json.dumps(val)
            conn.execute('INSERT INTO em_history.candidate_meta(key,value) VALUES (%s,%s)', ('workspace_projection:v1:block:' + bid, text))
            conn.execute('INSERT INTO em_history.candidate_meta(key,value) VALUES (%s,%s)', ('workspace_projection:v1:folder:' + bid, workspace_key(path) if path else ''))
        conn.execute("INSERT INTO em_history.management_trash(kind,project,key) VALUES ('block','',%s)", (bids['deleted'],))
        conn.execute("INSERT INTO em_history.management_trash(kind,project,key) VALUES ('project',%s,%s)", (proj + '-trash', proj + '-trash'))
        conn.commit()
    try:
        yield cfg, prefix, proj, bids
    finally:
        with cfg.connect(cfg.history_migration_db) as conn:
            conn.execute('DELETE FROM em_history.messages WHERE block_id = ANY(%s)', (list(bids.values()),))
            conn.execute('DELETE FROM em_history.blocks WHERE block_id = ANY(%s)', (list(bids.values()),))
            conn.execute('DELETE FROM em_history.projects WHERE project LIKE %s', (prefix + '%',))
            conn.execute('DELETE FROM em_history.candidate_meta WHERE key LIKE %s', ('workspace_projection:v1:%:' + prefix + '%',))
            conn.execute('DELETE FROM em_history.management_trash WHERE key LIKE %s OR project LIKE %s', (prefix + '%', prefix + '%'))
            conn.execute('DELETE FROM em_history.management_trash WHERE kind = %s AND key = ANY(%s)', ('workspace', [workspace_key(p) for p in paths.values()]))
            conn.execute('DELETE FROM em_history.management_operations WHERE request_id LIKE %s', (prefix + '%',))
            conn.execute('DELETE FROM em_runtime.cutover_write_journal WHERE request_id LIKE %s', (prefix + '%',))
            conn.commit()


def test_workspace_groups_unknowns_and_no_old_trash_resurrection(seeded):
    cfg, prefix, proj, bids = seeded
    reader = WorkspaceHistoryReader(cfg, user_id='workspace-test')
    groups = {p['project']: p for p in reader.projects()['projects']}
    first = groups[workspace_key(r'C:\One\App')]
    assert first['display_name'] == 'App'
    assert first['block_count'] == 2
    assert first['message_count'] == 4
    assert workspace_key(r'C:\Two\App') in groups
    assert workspace_key(r'C:\Secret\Hidden') not in groups
    assert workspace_key(r'C:\Old\Deleted') not in groups
    overview = reader.project_overview(first['project'])
    assert {b['block_id'] for b in overview['blocks']} == {bids['a'], bids['case']}
    unassigned = {b['block_id'] for b in reader.unassigned()['blocks'] if b['block_id'].startswith(prefix)}
    assert unassigned == {bids['unknown'], bids['mixed'], bids['bad']}
    block = reader.block_page(bids['a'])['block']
    assert block['projects'] == [first['project']] and block['workspace_path']
    assert reader.block_page(bids['oldtrash'])['ok'] is False
    legacy = PgHistoryCandidateReader(cfg, user_id='workspace-test')
    assert legacy.project_overview(proj)['ok'] is True


def test_workspace_search_filters_before_limit_and_projects_relabel(seeded):
    cfg, prefix, proj, bids = seeded
    reader = WorkspaceHistoryReader(cfg, user_id='workspace-test')
    target = workspace_key(r'C:\Two\App')
    hits = reader.hybrid_search('needle', project=target, limit=1)
    assert len(hits) == 1 and hits[0]['block_id'] == bids['other']
    assert hits[0]['projects'] == [target] and hits[0]['workspace_path'] == r'C:\Two\App'
    assert hits[0]['turn_count'] == 1 and hits[0]['message_count'] == 2
    assert hits[0]['observed_first_at'] == '2026-11-16'
    assert hits[0]['observed_last_at'] == '2026-11-17'
    assert hits[0]['time_basis'] == 'recorded'
    assert reader.hybrid_search('needle', project='workspace-' + '0' * 32, limit=1) == []
    unknown = reader.hybrid_search('needle', scope='unassigned', limit=100)
    ours = [h for h in unknown if h['block_id'].startswith(prefix)]
    assert {h['block_id'] for h in ours} == {bids['unknown'], bids['mixed'], bids['bad']}
    assert all(h['projects'] == [] for h in ours)
    assert all(h['turn_count'] == 1 and h['observed_last_at'] == '2026-11-17' for h in ours)
    vector = [1.] + [0.] * 767
    with cfg.connect(cfg.history_migration_db) as conn:
        literal = '[' + ','.join(str(n) for n in vector) + ']'
        conn.execute('UPDATE em_history.blocks SET embedding=%s::vector WHERE block_id=ANY(%s)', (literal, [bids['a'], bids['other']]))
        conn.execute('UPDATE em_history.messages SET embedding=%s::vector WHERE block_id=%s', (literal, bids['other']))
        conn.commit()
    vector_hits = reader.hybrid_search('', query_vec=vector, project=target, limit=1)
    assert len(vector_hits) == 1 and vector_hits[0]['block_id'] == bids['other']


def test_new_archive_invalidates_old_directory_until_index_refresh(seeded):
    cfg, prefix, proj, bids = seeded
    with cfg.connect(cfg.history_migration_db) as conn:
        conn.execute('UPDATE em_history.blocks SET block_sha256=%s WHERE block_id=%s', ('new-sha', bids['a']))
        conn.execute('DELETE FROM em_history.candidate_meta WHERE key=%s', ('workspace_projection:v1:block:' + bids['other'],))
        conn.commit()
    reader = WorkspaceHistoryReader(cfg, user_id='workspace-test')
    unassigned = {b['block_id'] for b in reader.unassigned()['blocks']}
    assert bids['a'] in unassigned and bids['other'] in unassigned
    assert reader.block_page(bids['a'])['block']['projects'] == []


def test_workspace_soft_delete_hides_all_reads_and_restores_without_business_rewrite(seeded):
    cfg, prefix, proj, bids = seeded
    reader = WorkspaceHistoryReader(cfg, user_id='workspace-test')
    legacy = PgHistoryCandidateReader(cfg, user_id='workspace-test')
    key = workspace_key(r'C:\One\App')
    out = reader.manage('delete', kind='workspace', project=key, key=key, request_id=prefix + '-del')
    assert out['ok']
    assert reader.project_overview(key)['ok'] is False
    assert reader.block_turn_page(bids['a'])['ok'] is False
    assert legacy.block_page(bids['a'])['ok'] is False
    assert not any(h['block_id'] == bids['a'] for h in legacy.hybrid_search('needle', limit=100))
    assert bids['a'] not in {b['block_id'] for b in legacy.project_overview(proj)['blocks']}
    assert bids['a'] not in {b['block_id'] for b in reader.unassigned()['blocks']}
    reader.manage('restore', kind='workspace', project=key, key=key, request_id=prefix + '-restore')
    assert reader.block_page(bids['a'])['ok']
    with cfg.connect(cfg.history_migration_db) as conn:
        assert conn.execute('SELECT project FROM em_history.block_projects WHERE block_id=%s', (bids['a'],)).fetchone()[0] == proj


def test_workspace_dispatch_keeps_authorization_and_routes_scope(seeded):
    cfg, prefix, proj, bids = seeded
    env = {'EVOLVMEM_CONSOLE_OWNER_ID': 'workspace-test', 'EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE': 'workspace-test'}
    kwargs = {'environ': env, 'pg_config': cfg}
    path = '/api/history-candidate/projects'
    status, _ = dispatch_pg_history_api('GET', path, {'grouping': 'workspace'}, {}, **kwargs)
    assert status == 403
    status, payload = dispatch_pg_history_api('GET', path, {'grouping': 'workspace'}, {}, owner_verified=True, **kwargs)
    assert status == 200 and payload['grouping'] == 'workspace'
    status, payload = dispatch_pg_history_api('GET', '/api/history-candidate/search', {'grouping': 'workspace', 'q': 'needle', 'scope': 'unassigned', 'limit': 100}, {}, owner_verified=True, **kwargs)
    assert status == 200 and bids['a'] not in {h['block_id'] for h in payload['items']}
    status, _ = dispatch_pg_history_api('POST', '/api/history-candidate/management/delete', {'kind': 'workspace', 'key': workspace_key(r'C:\One\App')}, {'x-evolvmem-action': 'history-manage'}, **kwargs)
    assert status == 403


def test_workspace_trash_disambiguates_same_names_and_restores_only_one(seeded):
    cfg, prefix, proj, bids = seeded
    reader = WorkspaceHistoryReader(cfg, user_id='workspace-test')
    first, second = workspace_key(r'C:\One\App'), workspace_key(r'C:\Two\App')
    for name, key in (('first', first), ('second', second)):
        reader.manage('delete', kind='workspace', project=key, key=key, title='App', request_id=prefix + '-' + name)
    trash = {item['key']: item for item in reader.manage('trash')['items'] if item['kind'] == 'workspace'}
    assert trash[first]['workspace_path'] == r'C:\One\App'
    assert trash[second]['workspace_path'] == r'C:\Two\App'
    assert trash[first]['block_count'] == 2 and trash[second]['block_count'] == 1
    status, payload = dispatch_pg_history_api(
        'GET', '/api/history-candidate/management/trash', {}, {}, owner_verified=True,
        environ={'EVOLVMEM_CONSOLE_OWNER_ID': 'workspace-test', 'EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE': 'workspace-test'}, pg_config=cfg,
    )
    assert status == 200
    assert next(item for item in payload['items'] if item['key'] == first)['workspace_path'] == r'C:\One\App'
    reader.manage('restore', kind='workspace', project=first, key=first, request_id=prefix + '-restore-first')
    assert reader.project_overview(first)['ok']
    assert reader.project_overview(second)['ok'] is False
    assert reader.block_page(bids['other'])['ok'] is False


def test_message_times_expose_archive_fallback_without_changing_stored_values(seeded):
    cfg, prefix, proj, bids = seeded
    reader = WorkspaceHistoryReader(cfg, user_id='workspace-test')
    with cfg.connect(cfg.history_migration_db) as conn:
        conn.execute("UPDATE em_history.blocks SET adapter='dsh',kind='live_session' WHERE block_id=%s", (bids['other'],))
        conn.execute("UPDATE em_history.messages SET recorded_at='2026-11-17 04:00:00' WHERE block_id=%s", (bids['other'],))
        conn.commit()
    records = reader.unassigned()['blocks'] + [b for p in reader.projects()['projects'] for b in reader.project_overview(p['project'])['blocks']]
    hit = next(b for b in records if b['block_id'] == bids['other'])
    assert hit['time_basis'] == 'archive_fallback'
    result = reader.block_turn_page(bids['other'])
    assert result['ok'] and result['time_basis'] == 'archive_fallback'
    assert result['messages'][0]['recorded_at'] == '2026-11-17T04:00:00+00:00'
    assert result['messages'][0]['time_basis'] == 'archive_fallback'
    assert reader.block_turn_page(bids['a'])['time_basis'] == 'recorded'
    status, payload = dispatch_pg_history_api('GET', '/api/history-candidate/block/' + bids['other'] + '/turns', {}, {},
        owner_verified=True, pg_config=cfg,
        environ={'EVOLVMEM_CONSOLE_OWNER_ID': 'workspace-test', 'EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE': 'workspace-test'})
    assert status == 200 and payload['time_basis'] == 'archive_fallback'
    with cfg.connect(cfg.history_migration_db) as conn:
        assert conn.execute('SELECT recorded_at FROM em_history.messages WHERE block_id=%s', (bids['other'],)).fetchone()[0] == '2026-11-17 04:00:00'


def test_message_pages_cover_long_conversation_and_report_end(seeded):
    cfg, prefix, proj, bids = seeded
    bid = bids['a']
    with cfg.connect(cfg.history_migration_db) as conn:
        conn.execute('DELETE FROM em_history.messages WHERE block_id=%s', (bid,))
        for ordinal in range(116):
            conn.execute("INSERT INTO em_history.messages(block_id,ordinal,role,content) VALUES (%s,%s,'user',%s)", (bid, ordinal, 'message ' + str(ordinal)))
        conn.commit()
    reader = WorkspaceHistoryReader(cfg, user_id='workspace-test')
    pages = [reader.block_turn_page(bid, offset=o, limit=40) for o in (0, 40, 80)]
    assert [p['has_more'] for p in pages] == [True, True, False]
    assert [p['next_offset'] for p in pages] == [40, 80, 116]
    assert [m['ordinal'] for p in pages for m in p['messages']] == list(range(116))
    empty = reader.block_turn_page(bid, offset=116, limit=40)
    assert empty['messages'] == [] and empty['has_more'] is False
