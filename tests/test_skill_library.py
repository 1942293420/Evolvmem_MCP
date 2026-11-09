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
