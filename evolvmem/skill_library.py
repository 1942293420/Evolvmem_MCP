"""Read-only skill catalog and trusted, one-way snapshot importers."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import Request, urlopen

SOURCES = ('official', 'windows')
SKILLS = (
    ('anthropics/skills', 'skills/frontend-design', 'Anthropic Frontend Design', None),
    ('vercel-labs/agent-skills', 'skills/web-design-guidelines', 'Vercel Web Design Guidelines', None),
    ('microsoft/playwright', 'packages/playwright-core/src/tools/skills/playwright-cli', 'Microsoft Playwright CLI', 'LICENSE'),
    ('vercel-labs/agent-skills', 'skills/react-best-practices', 'Vercel React Best Practices', None),
    ('shadcn-ui/ui', 'skills/shadcn', 'shadcn/ui Skill', 'LICENSE.md'),
)
MAX_SNAPSHOT = 48 * 1024 * 1024
MAX_FILE = 1024 * 1024
MAX_PRESENTATION = 2 * 1024 * 1024
PRESENTATION_FILE = 'presentation.zh-CN.json'
PRESENTATION_FIELDS = {'title_zh': 80, 'summary_zh': 600, 'content_zh': 12000}
CATEGORY_FILE = 'categories.zh-CN.json'
MAX_CATEGORIES = 512 * 1024
CATEGORIES = {
    'frontend': '前端与界面', 'development': '开发与代码', 'testing': '测试与验收',
    'business': '业务与数据', 'feishu': '飞书协作', 'ziniao': '紫鸟管理',
    'visual': '绘图与图像', 'system': '系统与工具', 'skills': '技能管理',
    'uncategorized': '待分类',
}

MAX_ARCHIVE = 192 * 1024 * 1024
WINDOWS_FOLDERS = {'references', 'scripts', 'assets', 'templates', 'examples', 'rules'}
TEXT_EXTENSIONS = {'.md', '.txt', '.rst', '.py', '.ps1', '.sh', '.js', '.mjs', '.cjs',
                   '.ts', '.tsx', '.json', '.toml', '.yaml', '.yml', '.css', '.html', '.sql',
                   '.csv', '.xml', '.svg', '.ini', '.conf', '.rb', '.go', '.bat', '.cmd'}
SECRET_NAME = re.compile(r'(?:^|[._-])(?:secrets?|credentials?|tokens?|auth-state|private-key)(?:[._-]|$)', re.I)
SECRET_VALUE = re.compile(
    r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]+?-----END [^-]*PRIVATE KEY-----'
    r'|(?:sk-|gh[pousr]_|github_pat_|AKIA|xox[baprs]-)[A-Za-z0-9_-]{18,}'
    r'|(?i:\b(?:authorization|bearer)\s*[:=]?\s*(?:bearer\s+)?)[A-Za-z0-9._~-]{32,}'
    r'|(?i:\b(?:app[_-]?secret|client[_-]?secret|access[_-]?token|refresh[_-]?token|'
    r'api[_-]?key|password)\b\s*[:=]\s*["\x27]?)[A-Za-z0-9._~+/=-]{20,}'
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def catalog_root(data_dir=None):
    return Path(data_dir or Path.home() / '.claude' / 'evolvmem') / 'skill-library'


def snapshot_path(root, source):
    return root / 'windows' / 'windows.json' if source == 'windows' else root / 'official.json'


def safe_path(value):
    if not isinstance(value, str) or len(value) > 300 or '\\' in value:
        return False
    path = PurePosixPath(value)
    return (bool(path.parts) and value == path.as_posix() and not path.is_absolute()
            and not any(part in ('.', '..') or any(ord(c) < 32 for c in part) for part in path.parts))


def validate(data, source):
    if not isinstance(data, dict) or data.get('schema') != 1 or data.get('source') != source:
        raise ValueError('invalid skill snapshot')
    entries = data.get('entries')
    if not isinstance(entries, list) or len(entries) > 512:
        raise ValueError('invalid skill count')
    ids = set()
    for entry in entries:
        if (not isinstance(entry, dict) or not all(isinstance(entry.get(key), str)
                for key in ('id', 'name', 'description', 'origin', 'version', 'url'))):
            raise ValueError('invalid skill entry')
        key, files = entry['id'], entry.get('files')
        if (not re.fullmatch(r'[a-f0-9]{24}', key) or key in ids or not entry['name']
                or len(entry['name']) > 160 or len(entry['description']) > 600
                or len(entry['origin']) > 160 or len(entry['version']) > 160
                or len(entry['url']) > 400 or (entry['url'] and
                    (source != 'official' or not entry['url'].startswith('https://github.com/')))
                or not isinstance(files, dict) or 'SKILL.md' not in files or len(files) > 256):
            raise ValueError('invalid skill identity or files')
        if not all(safe_path(path) and isinstance(value, str)
                   and len(value.encode('utf-8')) <= MAX_FILE for path, value in files.items()):
            raise ValueError('invalid skill file')
        assets = entry.get('assets', {})
        if (not isinstance(assets, dict) or (source == 'windows' and assets)
                or len(assets) + len(files) > 256 or set(assets) & set(files)):
            raise ValueError('invalid skill assets')
        for path, value in assets.items():
            if (not safe_path(path) or not isinstance(value, str)
                    or len(value) > 4 * ((MAX_FILE + 2) // 3)):
                raise ValueError('invalid skill asset')
            if len(base64.b64decode(value, validate=True)) > MAX_FILE:
                raise ValueError('skill asset too large')
        ids.add(key)
    if not isinstance(data.get('updated_at'), str) or len(data['updated_at']) > 40:
        raise ValueError('invalid sync time')
    return data


def read_snapshot(path, source):
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_SNAPSHOT + 1)
    except FileNotFoundError:
        return {'schema': 1, 'source': source, 'updated_at': '', 'entries': []}
    if len(raw) > MAX_SNAPSHOT:
        raise ValueError('skill snapshot too large')
    return validate(json.loads(raw), source)


def atomic_snapshot(path, raw, mode=0o600):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.skill-snapshot-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def envelope(source, entries):
    return {'schema': 1, 'source': source,
            'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'entries': entries}


def write_snapshot(path, data, mode=0o600):
    validate(data, data['source'])
    raw = encode(data)
    if len(raw) > MAX_SNAPSHOT:
        raise ValueError('skill snapshot too large')
    atomic_snapshot(path, raw, mode)
    return raw


def read_presentations(root):
    """Read optional display-only translations; damaged metadata never hides skills."""
    path = root / PRESENTATION_FILE
    if path.is_symlink():
        return {}
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_PRESENTATION + 1)
        if len(raw) > MAX_PRESENTATION:
            return {}
        data = json.loads(raw)
        if (not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 1
                or data.get('locale') != 'zh-CN' or not isinstance(data.get('entries'), dict)
                or len(data['entries']) > 1024):
            return {}
        result = {}
        for key, value in data['entries'].items():
            if (not re.fullmatch(r'[a-f0-9]{24}', key) or not isinstance(value, dict)
                    or not isinstance(value.get('source_sha256'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', value['source_sha256'])):
                continue
            if not all(isinstance(value.get(field), str)
                       and 0 < len(value[field].strip()) <= limit and '\x00' not in value[field]
                       for field, limit in PRESENTATION_FIELDS.items()):
                continue
            result[key] = {field: value[field] for field in (*PRESENTATION_FIELDS, 'source_sha256')}
        return result
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {}


def skill_presentation(entry, presentations):
    """Translations are valid only for their exact original SKILL.md."""
    value = presentations.get(entry['id'])
    if value and value['source_sha256'] == digest(entry['files']['SKILL.md'].encode('utf-8')):
        return dict(value)
    return None


def read_categories(root):
    """Optional, source-bound purpose labels; independent of native Skill content."""
    path = root / CATEGORY_FILE
    if path.is_symlink():
        return {}
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_CATEGORIES + 1)
        if len(raw) > MAX_CATEGORIES:
            return {}
        data = json.loads(raw)
        if (not isinstance(data, dict) or type(data.get('schema')) is not int
                or data['schema'] != 1 or not isinstance(data.get('entries'), dict)
                or len(data['entries']) > 1024):
            return {}
        result = {}
        for key, item in data['entries'].items():
            if not re.fullmatch(r'[a-f0-9]{24}', key) or not isinstance(item, dict):
                continue
            sha = item.get('source_sha256')
            ids = item.get('category_ids')
            if (not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha)
                    or not isinstance(ids, list) or not 1 <= len(ids) <= 3
                    or not all(isinstance(i, str) and i in CATEGORIES and i != 'uncategorized' for i in ids)
                    or len(set(ids)) != len(ids)):
                continue
            result[key] = {'source_sha256': sha, 'category_ids': list(ids)}
        return result
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {}


def skill_categories(entry, classifications):
    item = classifications.get(entry['id'])
    ids = item['category_ids'] if item and item['source_sha256'] == digest(
        entry['files']['SKILL.md'].encode('utf-8')) else ['uncategorized']
    return [{'id': ident, 'label': CATEGORIES[ident]} for ident in ids]


def read_catalog(root, params):
    if set(params) - {'source', 'q', 'id', 'file', 'category'}:
        raise ValueError('invalid catalog query')
    source, q = params.get('source', ''), params.get('q', '')
    category = params.get('category', '')
    if not isinstance(category, str) or category not in ('', *CATEGORIES):
        raise ValueError('invalid skill category')
    if source not in ('', *SOURCES) or not isinstance(q, str) or len(q) > 160:
        raise ValueError('invalid catalog filter')
    sources = (source,) if source else SOURCES
    rows, sync = [], {}
    presentations = read_presentations(root)
    classifications = read_categories(root)
    counts = dict.fromkeys(CATEGORIES, 0)
    matched_total = 0
    key = params.get('id')
    if 'file' in params and not key:
        raise ValueError('file requires a skill')
    needle = q.strip().casefold()
    for kind in sources:
        snapshot = read_snapshot(snapshot_path(root, kind), kind)
        sync[kind] = snapshot['updated_at']
        for entry in snapshot['entries']:
            files = entry['files']
            assets = entry.get('assets', {})
            item = {k: entry[k] for k in ('id', 'name', 'description', 'origin', 'version', 'url')}
            item['source'] = kind
            item['categories'] = skill_categories(entry, classifications)
            presentation = skill_presentation(entry, presentations)
            if presentation:
                item['presentation'] = presentation
            if key == item['id']:
                file = params.get('file', 'SKILL.md')
                detail = {'ok': True, 'skill': item, 'files': sorted((*files, *assets)), 'file': file}
                if file in files:
                    return {**detail, 'text': files[file]}
                if file in assets:
                    return {**detail, 'base64': assets[file]}
                raise LookupError('unknown skill file')
            search_text = [item['name'], item['description'], item['origin']]
            search_text.extend(c['label'] for c in item['categories'])
            if presentation:
                search_text.extend(presentation[field] for field in PRESENTATION_FIELDS)
            if not key and needle in ' '.join(search_text).casefold():
                matched_total += 1
                ids = [c['id'] for c in item['categories']]
                for ident in ids:
                    counts[ident] += 1
                if not category or category in ids:
                    rows.append({**item, 'file_count': len(files) + len(assets)})
    if key:
        raise LookupError('unknown skill')
    return {'ok': True, 'rows': rows, 'sync': sync, 'matched_total': matched_total,
            'categories': [{'id': ident, 'label': label, 'count': counts[ident]}
                           for ident, label in CATEGORIES.items()]}


def _http(url, limit):
    request = Request(url, headers={'User-Agent': 'EvolvMem-Skill-Catalog', 'Accept': '*/*'})
    with urlopen(request, timeout=20) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('upstream response too large')
    return data


def _git_api(repo, endpoint):
    return json.loads(_http(f'https://api.github.com/repos/{repo}/{endpoint}', 4 * MAX_FILE))


def _git_revision(repo):
    revision = _git_api(repo, 'commits/main')['sha']
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('invalid upstream revision')
    return revision


def _git_archive(repo, revision, directories, extra_files):
    request = Request(f'https://codeload.github.com/{repo}/tar.gz/{revision}',
                      headers={'User-Agent': 'EvolvMem-Skill-Catalog'})
    files, assets = {}, {}
    with tempfile.TemporaryFile() as downloaded:
        with urlopen(request, timeout=25) as response:
            for chunk in iter(lambda: response.read(1024 * 1024), b''):
                if downloaded.tell() + len(chunk) > MAX_ARCHIVE:
                    raise ValueError(f'upstream archive too large: {repo}')
                downloaded.write(chunk)
        downloaded.seek(0)
        unpacked = 0
        with tarfile.open(fileobj=downloaded, mode='r|gz') as archive:
            for item in archive:
                unpacked += item.size
                if unpacked > 4 * MAX_ARCHIVE:
                    raise ValueError(f'upstream expanded archive too large: {repo}')
                path = item.name.partition('/')[2]
                if not item.isfile() or not (path in extra_files or
                        any(path.startswith(directory + '/') for directory in directories)):
                    continue
                if not safe_path(path) or item.size > MAX_FILE:
                    raise ValueError(f'invalid upstream skill file: {repo}')
                file = archive.extractfile(item)
                if file is None:
                    raise ValueError(f'unreadable upstream skill file: {repo}')
                data = file.read(MAX_FILE + 1)
                try:
                    files[path] = data.decode('utf-8-sig')
                except UnicodeError:
                    assets[path] = base64.b64encode(data).decode('ascii')
    return files, assets


def _frontmatter(text, field):
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return ''
    for i, line in enumerate(lines[1:40], start=1):
        if line.strip() == '---':
            break
        if line.startswith(field + ':'):
            value = line.split(':', 1)[1].strip().strip('"\x27')
            if value in ('|', '>', '|-', '>-') and i + 1 < len(lines):
                value = lines[i + 1].strip()
            return value
    return ''


def refresh_official(root):
    old = read_snapshot(snapshot_path(root, 'official'), 'official')
    previous = {item['id']: item for item in old['entries']}
    guidelines_repo = 'vercel-labs/web-interface-guidelines'
    with ThreadPoolExecutor(max_workers=5) as workers:
        repos = sorted({repo for repo, *_ in SKILLS} | {guidelines_repo})
        revisions = dict(zip(repos, workers.map(_git_revision, repos)))
        pending, entries = {}, {}
        for repo, directory, name, license_path in SKILLS:
            revision = revisions[repo]
            key = digest(f'{repo}/{directory}'.encode())[:24]
            guidelines = directory.endswith('/web-design-guidelines')
            version = f'{revision}:{revisions[guidelines_repo]}' if guidelines else revision
            if key in previous and previous[key]['version'] == version:
                entries[key] = previous[key]
                continue
            pending[key] = (repo, directory, name, license_path, guidelines, version)
        downloads = {}
        for repo in {item[0] for item in pending.values()}:
            requested = [item for item in pending.values() if item[0] == repo]
            downloads[repo] = workers.submit(
                _git_archive, repo, revisions[repo], [item[1] for item in requested],
                {'LICENSE', 'LICENSE.md', 'LICENSE.txt'} if any(item[3] for item in requested) else set())
        if any(item[4] for item in pending.values()):
            downloads[guidelines_repo] = workers.submit(
                _git_archive, guidelines_repo, revisions[guidelines_repo], [], {'command.md', 'LICENSE', 'LICENSE.md'})
        for key, (repo, directory, name, license_path, guidelines, version) in pending.items():
            revision = revisions[repo]
            package, binary = downloads[repo].result()
            files = {path[len(directory) + 1:]: text for path, text in package.items()
                     if path.startswith(directory + '/')}
            assets = {path[len(directory) + 1:]: value for path, value in binary.items()
                      if path.startswith(directory + '/')}
            if license_path:
                license_text = next((package[path] for path in
                    (license_path, 'LICENSE', 'LICENSE.md', 'LICENSE.txt') if path in package), None)
                if not license_text:
                    raise ValueError(f'upstream license missing: {repo}')
                files['UPSTREAM-LICENSE.txt'] = license_text
            if guidelines:
                guide, _ = downloads[guidelines_repo].result()
                files['upstream-guidelines.md'] = guide['command.md']
                for path in ('LICENSE', 'LICENSE.md'):
                    if path in guide:
                        files['upstream-guidelines-LICENSE.txt'] = guide[path]
                        break
            entries[key] = {'id': key, 'name': name, 'description': _frontmatter(files['SKILL.md'], 'description')[:600],
                            'origin': repo, 'version': version, 'url': f'https://github.com/{repo}/tree/{revision}/{directory}',
                            'files': files, 'assets': assets}
    ordered = [entries[digest(f'{repo}/{directory}'.encode())[:24]] for repo, directory, *_ in SKILLS]
    write_snapshot(snapshot_path(root, 'official'), envelope('official', ordered))
    return len(ordered)


def _regular(path):
    info = path.lstat()
    return not (path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400)


def _windows_files(directory):
    files = {}
    skipped = 0
    for current, folders, names in os.walk(directory, followlinks=False):
        parent = Path(current)
        if parent == directory:
            folders[:] = [name for name in folders if name in WINDOWS_FOLDERS and _regular(parent / name)]
        else:
            folders[:] = [name for name in folders if not name.startswith('.') and name not in
                          {'__pycache__', 'node_modules', 'dist', 'build', 'venv'} and _regular(parent / name)]
        for name in sorted(names):
            path = parent / name
            relative = path.relative_to(directory).as_posix()
            primary = relative.casefold() == 'skill.md'
            allowed = primary or (not name.startswith('.') and not SECRET_NAME.search(name)
                        and (path.suffix.lower() in TEXT_EXTENSIONS or name.upper() in ('LICENSE', 'NOTICE')))
            if not allowed or not _regular(path) or not stat.S_ISREG(path.lstat().st_mode):
                skipped += 1
                continue
            try:
                with path.open('rb') as stream:
                    data = stream.read(MAX_FILE + 1)
                if len(data) > MAX_FILE:
                    raise ValueError('file too large')
                text = data.decode('utf-8-sig')
            except (UnicodeError, ValueError):
                if primary:
                    raise ValueError(f'unreadable SKILL.md in {directory.name}') from None
                skipped += 1
                continue
            text, redactions = SECRET_VALUE.subn('[REDACTED]', text)
            skipped += bool(redactions)
            files['SKILL.md' if primary else relative] = text
    return files, skipped


def collect_windows(home):
    codex = home / '.codex' / 'skills'
    hermes = home / 'AppData' / 'Local' / 'hermes' / 'skills'
    roots = [('Codex', codex), ('Codex 内置', codex / '.system'),
             ('Agents', home / '.agents' / 'skills'), ('Hermes', hermes)]
    if hermes.is_dir():
        roots.extend((f'Hermes / {group.name}', group) for group in sorted(hermes.iterdir())
                     if not group.name.startswith('.') and group.is_dir() and _regular(group)
                     and not (group / 'SKILL.md').exists())
    entries, skipped = [], 0
    seen = set()
    for origin, root in roots:
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir()):
            if directory.name.startswith('.') or not directory.is_dir() or not _regular(directory):
                continue
            if not (directory / 'SKILL.md').is_file():
                continue
            files, ignored = _windows_files(directory)
            skipped += ignored
            if 'SKILL.md' not in files:
                continue
            version = digest(encode(files))
            identity = (directory.name.casefold(), version)
            if identity in seen:
                continue
            seen.add(identity)
            entries.append({'id': digest(f'{origin}/{directory.name}'.encode())[:24],
                            'name': (_frontmatter(files['SKILL.md'], 'name') or directory.name)[:160],
                            'description': _frontmatter(files['SKILL.md'], 'description')[:600],
                            'origin': origin, 'version': version, 'url': '', 'files': files})
    if not entries:
        raise ValueError('no Windows skills found; keep prior snapshot')
    result = envelope('windows', entries)
    result['screened_files'] = skipped
    return result


def receive_windows(root, mirror=None):
    raw = sys.stdin.buffer.read(MAX_SNAPSHOT + 1)
    if len(raw) > MAX_SNAPSHOT:
        raise ValueError('skill snapshot too large')
    data = validate(json.loads(raw), 'windows')
    if not data['entries']:
        raise ValueError('empty Windows skill snapshot')
    from fcntl import LOCK_EX, flock

    target = snapshot_path(root, 'windows')
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with target.with_suffix('.lock').open('a+b') as lock:
        flock(lock, LOCK_EX)
        data['updated_at'] = envelope('windows', [])['updated_at']
        encoded = write_snapshot(target, data)
        if mirror is not None:
            atomic_snapshot(mirror, encoded)
            if digest(mirror.read_bytes()) != digest(encoded):
                raise OSError('platform mirror verification failed')
    return {'source': 'windows', 'count': len(data['entries']), 'sha256': digest(encoded),
            'screened_files': data.get('screened_files', 0), 'platform_mirrored': mirror is not None}


def push_windows(home, host, receiver):
    data = collect_windows(home)
    validate(data, 'windows')
    raw = encode(data)
    if len(raw) > MAX_SNAPSHOT:
        raise ValueError('skill snapshot too large')
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'IdentityAgent=none', '-o', 'ConnectTimeout=10', host, receiver]
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    output = subprocess.check_output(command, input=raw, creationflags=flags, timeout=180)
    receipt = json.loads(output)
    if receipt.get('source') != 'windows' or receipt.get('count') != len(data['entries']):
        raise ValueError('Windows skill receipt mismatch')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('official', 'windows-receive', 'windows-push', 'windows-check'))
    parser.add_argument('--root', type=Path, default=catalog_root())
    parser.add_argument('--mirror', type=Path)
    parser.add_argument('--host', default='codex-linux')
    parser.add_argument('--receiver')
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    if args.action == 'official':
        result = {'source': 'official', 'count': refresh_official(args.root)}
    elif args.action == 'windows-receive':
        result = receive_windows(args.root, args.mirror)
    elif args.action == 'windows-push':
        if not args.receiver:
            parser.error('--receiver is required')
        result = push_windows(Path.home(), args.host, args.receiver)
    else:
        data = collect_windows(Path.home())
        validate(data, 'windows')
        result = {'count': len(data['entries']), 'screened_files': data['screened_files'],
                  'files': sum(len(item['files']) for item in data['entries'])}
    if args.receipt:
        atomic_snapshot(args.receipt, encode({**result, 'checked_at': envelope('windows', [])['updated_at']}))
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
