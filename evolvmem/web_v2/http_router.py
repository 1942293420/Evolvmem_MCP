"""HTTP route dispatcher for /v2 static assets and /api/console-v2/* endpoints."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, unquote

from evolvmem.web_v2.api_experiences import (
    get_experience_trace,
    handle_model_rollback,
    list_experiences,
    write_experience,
)
from evolvmem.web_v2.api_interceptions_and_inbox import (
    get_evidence_context,
    get_review_inbox,
    list_interceptions,
    post_review_decide,
    rescue_interception,
    trace_interception,
)
from evolvmem.web_v2.api_pipeline_history_and_control import (
    get_control,
    get_evolution_and_skills,
    get_overview,
    get_pipeline,
    handle_history,
    post_control_action,
)
from evolvmem.web_v2.common import V2ApiError

_V2_ROOT = (Path(__file__).resolve().parent.parent / 'web_static' / 'v2').resolve()
_EXP_TRACE_RE = re.compile(r'^/experiences/([^/]+)/trace$')
_INT_TRACE_RE = re.compile(r'^/interceptions/([^/]+)/trace$')

_MIME_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
}


def _send_bytes(handler: Any, status: int, content_type: str, data: bytes) -> None:
    handler.send_response(status)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.end_headers()
    handler.wfile.write(data)


def _serve_v2_static(handler: Any, req_path: str) -> bool:
    rel = 'index.html' if req_path in ('/v2', '/v2/', '/v2/index.html') else unquote(req_path[len('/v2/'):])
    if not rel or '..' in Path(rel).parts or rel.startswith('/'):
        handler._send_json({'ok': False, 'error': 'forbidden_path'}, 403)
        return True
    target = (_V2_ROOT / rel).resolve()
    if not target.is_relative_to(_V2_ROOT) or not target.is_file() or target.suffix not in _MIME_TYPES:
        handler._send_json({'ok': False, 'error': 'not_found'}, 404)
        return True
    raw = target.read_bytes()
    if rel == 'index.html' and b'<base ' not in raw:
        raw = raw.replace(b'<head>', b'<head><base href="/v2/">', 1)
    _send_bytes(handler, 200, _MIME_TYPES[target.suffix], raw)
    return True


def serve_v2_index(handler: Any) -> bool:
    """Serve Web Console v2 entry page with <base href="/v2/"> for root routes."""
    return _serve_v2_static(handler, '/v2/index.html')


def _dispatch_api(console: Any, policy: Any, method: str, rel: str, params: dict[str, Any], session: Any) -> tuple[int, dict[str, Any]]:
    if method == 'GET' and rel == '/me':
        st, _, res = console.dispatch('GET', '/me', {'inspect': '1'} if params.get('inspect') == '1' else {}, session)
        if isinstance(res, dict) and st == 200:
            res = {**res, 'policy': {**(res.get('policy') or {}), **policy.as_dict()}}
        return st, res if isinstance(res, dict) else {'ok': False, 'error': 'unexpected_response'}

    if method == 'GET' and rel == '/overview':
        return 200, get_overview(console, policy, session, params)
    if method == 'GET' and rel == '/experiences':
        return 200, list_experiences(console, session, params)

    m_exp = _EXP_TRACE_RE.fullmatch(rel)
    if method == 'GET' and m_exp:
        return 200, get_experience_trace(console, session, m_exp.group(1), params)
    if method == 'POST' and rel == '/experiences/write':
        out = write_experience(console, session, params)
        return int(out.pop('http_status', 200)), out
    if method == 'POST' and rel == '/experiences/rollback':
        return 200, handle_model_rollback(console, session, params)

    if method == 'GET' and rel == '/interceptions':
        return 200, list_interceptions(console, session, params)
    m_int = _INT_TRACE_RE.fullmatch(rel)
    if method == 'GET' and m_int:
        return 200, trace_interception(console, session, m_int.group(1), params)
    if method == 'POST' and rel == '/interceptions/rescue':
        return 200, rescue_interception(console, session, params)

    if method == 'GET' and rel == '/review-inbox':
        return 200, get_review_inbox(console, policy, session, params)
    if method == 'POST' and rel == '/review-inbox/decide':
        return 200, post_review_decide(console, policy, session, params, batch=False)
    if method == 'POST' and rel == '/review-inbox/batch':
        return 200, post_review_decide(console, policy, session, params, batch=True)

    if method == 'GET' and rel == '/evidence-context':
        return 200, get_evidence_context(console, session, params)
    if method == 'GET' and rel == '/pipeline':
        return 200, get_pipeline(console, policy, session, params)
    if rel == '/history' and method == 'GET':
        out = handle_history(console, session, 'GET', params)
        return int(out.pop('http_status', 200)), out
    if rel == '/history/manage' and method == 'POST':
        out = handle_history(console, session, 'POST', params)
        return int(out.pop('http_status', 200)), out
    if method == 'GET' and rel == '/evolution-and-skills':
        return 200, get_evolution_and_skills(console, session, params)
    if method == 'GET' and rel == '/control':
        return 200, get_control(console, policy, session, params)
    if method == 'POST' and rel == '/control/action':
        out = post_control_action(console, session, params)
        return int(out.pop('http_status', 200)), out
    raise V2ApiError('not_found', 404)


def handle_web_v2_request(
    handler: Any,
    *,
    parsed: Any,
    policy: Any,
    auth: Any,
    auth_lock: Any,
    ensure_control_plane_ready: Any,
) -> bool:
    """Handle /v2* static pages and /api/console-v2/* JSON routes; return False if unrelated."""
    path = parsed.path
    is_static = (path == '/v2' or path.startswith('/v2/'))
    is_api = (path == '/api/console-v2' or path.startswith('/api/console-v2/'))
    if not (is_static or is_api):
        return False

    if is_static:
        if handler.command != 'GET':
            handler._send_json({'ok': False, 'error': 'method_not_allowed'}, 405)
            return True
        with auth_lock:
            if auth.handle_get(handler, parsed):
                return True
        if not policy.console_enabled:
            handler._send_json({'ok': False, 'error': 'knowledge_console_disabled'}, 410)
            return True
        return _serve_v2_static(handler, path)

    if not policy.console_enabled:
        handler._send_json({'ok': False, 'error': 'knowledge_console_disabled'}, 410)
        return True
    session = auth.session(handler) if auth.enabled else None
    if not auth.enabled or not session:
        handler._send_json({'ok': False, 'error': 'login_required'}, 401)
        return True
    if handler.command not in ('GET', 'POST'):
        handler._send_json({'ok': False, 'error': 'method_not_allowed'}, 405)
        return True

    raw_qs = parse_qs(parsed.query)
    if any(len(v) != 1 for v in raw_qs.values()):
        handler._send_json({'ok': False, 'error': 'duplicate_query_parameter'}, 400)
        return True
    params: dict[str, Any] = {k: v[0] for k, v in raw_qs.items()}

    if handler.command == 'POST':
        if not auth._valid_csrf(handler, session):
            handler._send_json({'ok': False, 'error': 'invalid_csrf'}, 403)
            return True
        body, failed = handler._read_body()
        if failed:
            return True
        if not isinstance(body, dict):
            handler._send_json({'ok': False, 'error': 'invalid_request'}, 400)
            return True
        params = {**body, **({'inspect': '1'} if params.get('inspect') == '1' else {})}

    rel = unquote(path[len('/api/console-v2'):] or '/').rstrip('/') or '/'
    try:
        status, payload = _dispatch_api(ensure_control_plane_ready(), policy, handler.command, rel, params, session)
        handler._send_json(payload, status)
    except V2ApiError as exc:
        err: dict[str, Any] = {'ok': False, 'error': exc.code}
        if exc.detail:
            err['detail'] = exc.detail
        handler._send_json(err, exc.status)
    return True
