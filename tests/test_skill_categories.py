import hashlib
import json

import pytest

from evolvmem.skill_library import (
    CATEGORY_FILE, MAX_CATEGORIES, encode, envelope, read_catalog,
    receive_windows, snapshot_path, write_snapshot,
)


def setup_catalog(root):
    rows = []
    for char, name, source in [('a', 'Browser UI', 'official'),
                               ('b', 'Local UI', 'windows'), ('c', 'Revenue', 'windows')]:
        body = '# ' + name + '\nOriginal instructions'
        rows.append({'id': char*24, 'name': name, 'description': name, 'origin': 'Fixture',
                     'version': char*40, 'url': 'https://github.com/demo/skill' if source == 'official' else '',
                     'files': {'SKILL.md': body}})
    for kind, items in [('official', rows[:1]), ('windows', rows[1:])]:
        write_snapshot(snapshot_path(root, kind), envelope(kind, items))
    categories = {'schema': 1, 'entries': {}}
    for entry, ids in zip(rows, [('frontend', 'testing'), ('frontend',), ('business',)]):
        categories['entries'][entry['id']] = {
            'source_sha256': hashlib.sha256(entry['files']['SKILL.md'].encode()).hexdigest(),
            'category_ids': list(ids),
        }
    (root / CATEGORY_FILE).write_bytes(encode(categories))
    return rows, categories


def counts(result):
    return {r['id']: r['count'] for r in result['categories']}


def test_facets_apply_query_and_source_before_category(tmp_path):
    rows, _ = setup_catalog(tmp_path)
    originals = {s: snapshot_path(tmp_path, s).read_bytes() for s in ('official', 'windows')}
    all_rows = read_catalog(tmp_path, {})
    assert all_rows['matched_total'] == len(all_rows['rows']) == 3
    assert counts(all_rows)['frontend'] == 2
    assert counts(all_rows)['testing'] == 1
    filtered = read_catalog(tmp_path, {'category': 'frontend'})
    assert len(filtered['rows']) == 2 and counts(filtered)['business'] == 1
    combined = read_catalog(tmp_path, {'source': 'windows', 'q': 'UI', 'category': 'frontend'})
    assert combined['matched_total'] == 1 and [x['id'] for x in combined['rows']] == [rows[1]['id']]
    assert counts(combined)['testing'] == 0
    empty = read_catalog(tmp_path, {'source': 'windows', 'q': 'UI', 'category': 'business'})
    assert empty['rows'] == [] and empty['matched_total'] == 1 and counts(empty)['frontend'] == 1
    assert len(read_catalog(tmp_path, {'q': '前端与界面'})['rows']) == 2
    assert read_catalog(tmp_path, {'id': rows[0]['id']})['skill']['categories'] == [
        {'id': 'frontend', 'label': '前端与界面'}, {'id': 'testing', 'label': '测试与验收'}]
    assert read_catalog(tmp_path, {'id': rows[0]['id']})['text'] == rows[0]['files']['SKILL.md']
    assert all(snapshot_path(tmp_path, s).read_bytes() == raw for s, raw in originals.items())


@pytest.mark.parametrize('case', ['stale', 'unknown', 'duplicate', 'bad', 'oversize', 'symlink', 'absent'])
def test_invalid_or_stale_classification_keeps_skill_in_uncategorized(tmp_path, case):
    rows, data = setup_catalog(tmp_path)
    path = tmp_path / CATEGORY_FILE
    if case == 'stale':
        data['entries'][rows[0]['id']]['source_sha256'] = '0'*64
    elif case == 'unknown':
        data['entries'][rows[0]['id']]['category_ids'] = ['made-up']
    elif case == 'duplicate':
        data['entries'][rows[0]['id']]['category_ids'] = ['frontend', 'frontend']
    path.write_bytes(encode(data))
    if case == 'bad':
        path.write_bytes(b'not json')
    elif case == 'oversize':
        path.write_bytes(b' '*(MAX_CATEGORIES+1))
    elif case == 'symlink':
        saved = tmp_path / 'saved.json'
        path.rename(saved)
        path.symlink_to(saved)
    elif case == 'absent':
        path.unlink()
    detail = read_catalog(tmp_path, {'id': rows[0]['id']})
    assert detail['skill']['categories'] == [{'id': 'uncategorized', 'label': '待分类'}]
    assert rows[0]['id'] in [r['id'] for r in read_catalog(tmp_path, {'category': 'uncategorized'})['rows']]
    assert len(read_catalog(tmp_path, {})['rows']) == 3


@pytest.mark.parametrize('bad', ['invalid', [], {'id': 'frontend'}, None])
def test_reject_bad_category_query(tmp_path, bad):
    with pytest.raises(ValueError, match='category'):
        read_catalog(tmp_path, {'category': bad})


def test_raw_sync_retains_categories_file_and_marks_changed_source(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace
    rows, _ = setup_catalog(tmp_path)
    before = (tmp_path / CATEGORY_FILE).read_bytes()
    rows[1]['files']['SKILL.md'] += '\nChanged'
    monkeypatch.setattr('evolvmem.skill_library.sys.stdin',
                        SimpleNamespace(buffer=io.BytesIO(encode(envelope('windows', rows[1:])))))
    receive_windows(tmp_path)
    assert (tmp_path / CATEGORY_FILE).read_bytes() == before
    assert read_catalog(tmp_path, {'id': rows[1]['id']})['skill']['categories'][0]['id'] == 'uncategorized'


def test_console_passes_combined_filter_and_preserves_admin_gate(tmp_path):
    from evolvmem.knowledge_console import ConsoleService
    from evolvmem.skill_library import catalog_root
    from tests.test_knowledge_console import OWNER, MEMBER, call
    service = ConsoleService(tmp_path/'registry.db', 'jiangli', tmp_path/'owner', tmp_path/'lan', {})
    service.bootstrap(OWNER['open_id'], 'Owner')
    service.observe_login(MEMBER['open_id'], 'Member')
    assert service.dispatch('POST', '/users/kane', {'open_id': MEMBER['open_id']}, OWNER)[0] == 200
    setup_catalog(catalog_root(service.owner_data_dir))
    query = {'source': 'windows', 'q': 'UI', 'category': 'frontend'}
    status, data = call(service, '/skills', OWNER, query)
    assert status == 200 and len(data['rows']) == 1
    assert call(service, '/skills', MEMBER, query)[0] == 403
    assert call(service, '/skills', None, query)[0] == 401
    assert call(service, '/skills', OWNER, {'category': 'bad'})[0] == 400
