import io
from types import SimpleNamespace

import pytest

from evolvmem.skill_library import (
    collect_windows, encode, read_catalog, receive_windows, snapshot_path,
)
from tests.test_knowledge_console import OWNER, MEMBER, call, console


def _skill(folder, name, body):
    folder.mkdir(parents=True)
    (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Synthetic test\n---\n{body}', encoding='utf-8')


def test_windows_import_publishes_only_windows_and_keeps_markdown_inert(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    main = home / '.codex' / 'skills' / 'alpha'
    _skill(main, 'alpha', '<script>example</script>\napi_key=synthetic-secret-000000000000')
    (main / 'references').mkdir()
    (main / 'references' / 'steps.md').write_text('Reference file', encoding='utf-8')
    (main / 'credentials.json').write_text('not a credential fixture', encoding='utf-8')
    (home / '.agents' / 'skills').mkdir(parents=True)
    (home / '.agents' / 'skills' / 'alias').symlink_to(main, target_is_directory=True)
    _skill(home / 'AppData' / 'Local' / 'hermes' / 'skills' / 'devops' / 'beta', 'beta', 'Test')
    data = collect_windows(home)
    assert [x['name'] for x in data['entries']] == ['alpha', 'beta']
    assert 'credentials.json' not in data['entries'][0]['files']
    assert 'synthetic-secret' not in data['entries'][0]['files']['SKILL.md']
    assert 'references/steps.md' in data['entries'][0]['files']

    monkeypatch.setattr('evolvmem.skill_library.sys.stdin', SimpleNamespace(buffer=io.BytesIO(encode(data))))
    root, mirror = tmp_path / 'catalog', tmp_path / 'platform' / 'windows.json'
    receipt = receive_windows(root, mirror)
    assert receipt['count'] == 2 and receipt['platform_mirrored']
    assert mirror.read_bytes() == snapshot_path(root, 'windows').read_bytes()
    rows = read_catalog(root, {'q': 'ALPHA'})['rows']
    assert len(rows) == 1 and 'files' not in rows[0]
    detail = read_catalog(root, {'id': rows[0]['id']})
    assert detail['file'] == 'SKILL.md' and '<script>example</script>' in detail['text']
    assert read_catalog(root, {'source': 'official'})['rows'] == []
    with pytest.raises(LookupError):
        read_catalog(root, {'id': rows[0]['id'], 'file': '../anything'})


def test_invalid_snapshot_cannot_replace_catalog(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    _skill(home / '.codex' / 'skills' / 'alpha', 'alpha', 'Original')
    data = collect_windows(home)
    monkeypatch.setattr('evolvmem.skill_library.sys.stdin', SimpleNamespace(buffer=io.BytesIO(encode(data))))
    receive_windows(tmp_path)
    path = snapshot_path(tmp_path, 'windows')
    previous = path.read_bytes()
    data['entries'][0]['files']['../escape'] = 'No'
    monkeypatch.setattr('evolvmem.skill_library.sys.stdin', SimpleNamespace(buffer=io.BytesIO(encode(data))))
    with pytest.raises(ValueError, match='invalid skill file'):
        receive_windows(tmp_path)
    assert path.read_bytes() == previous


def test_console_catalog_requires_admin_and_reads_no_missing_data(console):
    status, payload = call(console, '/skills', OWNER)
    assert status == 200 and payload['rows'] == []
    assert call(console, '/skills', MEMBER)[0] == 403
    assert call(console, '/skills', OWNER, {'id': 'missing'})[0] == 404
    assert call(console, '/skills', OWNER, method='POST')[0] == 404


def test_official_binary_asset_is_returned_as_data(tmp_path):
    from evolvmem.skill_library import envelope, write_snapshot

    skill = {'id': 'a'*24, 'name': 'Official', 'description': '', 'origin': 'owner/repo',
             'version': 'c'*40, 'url': 'https://github.com/owner/repo',
             'files': {'SKILL.md': 'Instructions'}, 'assets': {'assets/example.png': 'iVBORw=='}}
    write_snapshot(snapshot_path(tmp_path, 'official'), envelope('official', [skill]))
    result = read_catalog(tmp_path, {'id': skill['id'], 'file': 'assets/example.png'})
    assert result['base64'] == 'iVBORw==' and 'text' not in result
    assert result['files'] == ['SKILL.md', 'assets/example.png']


def _translated_catalog(tmp_path):
    import hashlib
    from evolvmem.skill_library import envelope, write_snapshot
    entry = {'id': 'b'*24, 'name': 'frontend-helper', 'description': 'UI guidance',
             'origin': 'Fixture', 'version': 'c'*40, 'url': '',
             'files': {'SKILL.md': '# Original\nKeep original bytes <script>inert</script>.'}}
    write_snapshot(snapshot_path(tmp_path, 'windows'), envelope('windows', [entry]))
    translation = {'title_zh': '设计网页布局', 'summary_zh': '设计清晰的页面。',
                   'content_zh': '按设备宽度调整宫格列数。',
                   'source_sha256': hashlib.sha256(entry['files']['SKILL.md'].encode()).hexdigest()}
    metadata = {'schema': 1, 'locale': 'zh-CN', 'entries': {entry['id']: translation}}
    (tmp_path / 'presentation.zh-CN.json').write_bytes(encode(metadata))
    return entry, metadata


def test_chinese_presentation_searches_without_rewriting_originals(tmp_path):
    entry, metadata = _translated_catalog(tmp_path)
    original = snapshot_path(tmp_path, 'windows').read_bytes()
    for query in ('布局', '清晰', '宫格', 'FRONTEND'):
        rows = read_catalog(tmp_path, {'q': query})['rows']
        assert len(rows) == 1
        assert rows[0]['name'] == entry['name']
        assert rows[0]['presentation']['title_zh'] == '设计网页布局'
    detail = read_catalog(tmp_path, {'id': entry['id']})
    assert detail['text'] == entry['files']['SKILL.md']
    assert detail['files'] == ['SKILL.md']
    assert snapshot_path(tmp_path, 'windows').read_bytes() == original


@pytest.mark.parametrize('kind', ['stale', 'invalid', 'oversized', 'bad-fields', 'missing', 'symlink'])
def test_outdated_or_invalid_translation_falls_back_to_source(tmp_path, kind):
    entry, data = _translated_catalog(tmp_path)
    path = tmp_path / 'presentation.zh-CN.json'
    if kind == 'stale':
        data['entries'][entry['id']]['source_sha256'] = '0'*64
        path.write_bytes(encode(data))
    elif kind == 'invalid':
        path.write_text('invalid json')
    elif kind == 'oversized':
        path.write_bytes(b' ' * (2*1024*1024+1))
    elif kind == 'bad-fields':
        data['entries'][entry['id']]['title_zh'] = []
        path.write_bytes(encode(data))
    elif kind == 'symlink':
        saved = tmp_path / 'saved.json'
        path.rename(saved)
        path.symlink_to(saved)
    else:
        path.unlink()
    assert read_catalog(tmp_path, {'q': '宫格'})['rows'] == []
    rows = read_catalog(tmp_path, {'q': 'frontend'})['rows']
    assert len(rows) == 1 and 'presentation' not in rows[0]
    assert read_catalog(tmp_path, {'id': entry['id']})['text'] == entry['files']['SKILL.md']


def test_raw_refresh_retains_display_metadata_but_rejects_stale_translation(tmp_path, monkeypatch):
    from evolvmem.skill_library import envelope
    entry, metadata = _translated_catalog(tmp_path)
    display_path = tmp_path / 'presentation.zh-CN.json'
    previous = display_path.read_bytes()
    entry['files']['SKILL.md'] += '\nNew source content'
    data = envelope('windows', [entry])
    monkeypatch.setattr('evolvmem.skill_library.sys.stdin', SimpleNamespace(buffer=io.BytesIO(encode(data))))
    receive_windows(tmp_path)
    assert display_path.read_bytes() == previous
    assert 'presentation' not in read_catalog(tmp_path, {})['rows'][0]
    assert read_catalog(tmp_path, {'id': entry['id']})['text'].endswith('New source content')
