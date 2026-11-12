"""Transitional SQLite source readers shared by ConsoleService (SQLite & PG control modes)."""
from __future__ import annotations

import json
from pathlib import Path

JSON_MIME = 'application/json; charset=utf-8'
HISTORY_KEYS = frozenset({
    'EVOLVMEM_HISTORY_CANDIDATE_DB',
    'EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE',
    'EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE',
    'EVOLVMEM_HISTORY_MANAGEMENT_DB',
    'EVOLVMEM_HISTORY_COVERAGE_FILE',
    'EVOLVMEM_HISTORY_ACCEPTED_AUDIT_FILE',
})


def clean_history_env(values: dict) -> dict[str, str]:
    if not isinstance(values, dict):
        raise ValueError('history environment must be a server-controlled mapping')
    return {
        key: str(value)
        for key, value in values.items()
        if key in HISTORY_KEYS and value is not None and str(value)
    }


def validate_history_paths(env: dict[str, str], registry_path: Path | None = None) -> None:
    source = env.get('EVOLVMEM_HISTORY_CANDIDATE_DB')
    if source and (
        not Path(source).is_absolute()
        or (registry_path is not None and Path(source).resolve() == registry_path)
    ):
        raise ValueError('invalid source configuration')
    management = env.get('EVOLVMEM_HISTORY_MANAGEMENT_DB')
    if management and (
        not Path(management).is_absolute()
        or (registry_path is not None and Path(management).resolve() == registry_path)
        or (source and Path(management).resolve() == Path(source).resolve())
    ):
        raise ValueError('management must be separate from source databases')


def read_history_count(env: dict[str, str]) -> int:
    from evolvmem.history_candidate_read import open_reader

    with open_reader(dict(env)) as reader:
        return sum(
            reader.block_visible(row[0])
            for row in reader.conn.execute('SELECT block_id FROM blocks WHERE message_count>0')
        )


def read_or_manage_history(owner_id: str, source, suffix: str, method: str, body: dict, error_cls):
    raw_env = source['history_environ'] if source else '{}'
    env = json.loads(raw_env) if isinstance(raw_env, str) else dict(raw_env or {})
    candidate = env.get('EVOLVMEM_HISTORY_CANDIDATE_DB')
    connected = bool(
        candidate and Path(candidate).is_file() and env.get('EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE')
    )
    if not connected:
        empty = {
            'ok': True, 'connected': False, 'history_connected': False,
            'owner_id': owner_id, 'reason': 'history_not_connected',
        }
        if method == 'GET' and suffix in ('', '/', '/projects'):
            return 200, JSON_MIME, {**empty, 'projects': []}
        if method == 'GET' and suffix == '/unassigned':
            return 200, JSON_MIME, {**empty, 'dialogue': [], 'empty': [], 'dialogue_count': 0, 'empty_count': 0}
        raise error_cls('history_not_connected', 404)
    if method == 'GET' and suffix.startswith('/attachment/'):
        from evolvmem.history_candidate_view import handle

        return handle('GET', '/history' + suffix, body, owner_verified=True, auth_enabled=True, environ=env)
    from evolvmem.history_candidate_web import dispatch

    status, result = dispatch(
        method,
        '/api/history-candidate' + (suffix if suffix not in ('', '/') else '/projects'),
        body,
        {'X-EvolvMem-Action': 'history-manage'},
        environ=env,
        owner_verified=True,
    )
    if isinstance(result, dict):
        result = {**result, 'owner_id': owner_id, 'connected': True, 'history_connected': True}
    return status, JSON_MIME, result


def read_experience_source(source, params: dict, item_id, *, search=None, error_cls):
    if params.get('origin') == 'manual' or (isinstance(item_id, str) and item_id.startswith('manual-')):
        from evolvmem.manual_experiences import ManualExperienceError, read

        try:
            return read(source, params, item_id, search=search)
        except ManualExperienceError as exc:
            raise error_cls(exc.code, exc.status) from None
    path = Path(source['data_dir']) if source and source['data_dir'] else None
    if not path or not (path / 'memory.db').is_file():
        if item_id is not None:
            raise error_cls('experience_not_found', 404)
        return {
            'ok': True, 'connected': False, 'experience_connected': False,
            'reason': 'experience_not_connected', 'rows': [], 'total': 0, 'page': 1, 'page_size': 20,
        }
    from evolvmem.config import Config
    from evolvmem.web_reads import snapshot

    config = Config(data_dir=path, apply_environment=False)
    with snapshot(config) as reader:
        origin = params.get('origin', 'core')
        if origin not in ('core', 'history'):
            raise error_cls('invalid_origin')
        if isinstance(item_id, str) or (item_id is None and origin == 'history'):
            from evolvmem import history_experience_reads

            conn = reader.store._connection()
            result = (
                history_experience_reads.experience(conn, source['user_id'], item_id)
                if item_id is not None
                else history_experience_reads.experiences(conn, source['user_id'], params)
            )
        else:
            result = (
                reader.insights().experience(item_id)
                if item_id is not None
                else reader.insights().experiences(params)
            )
    return {**result, 'ok': True, 'connected': True, 'experience_connected': True}
