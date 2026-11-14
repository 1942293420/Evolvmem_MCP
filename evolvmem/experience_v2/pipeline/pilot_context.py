"""Provenance & scrubbed verbatim context enrichment for shadow pilot review (no SQLite)."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable, Sequence

from evolvmem.experience_v2.pipeline.events import parse_session_events
from evolvmem.experience_v2.pipeline.history_reader import HistoryReader
from evolvmem.experience_v2.pipeline.moments import detect_key_moments

_WIN_USER_RE = re.compile(r'[A-Za-z]:\\Users\\([^\\/:*?"<>|\r\n]+)')
_SNIPPET_IDX_RE = re.compile(r'^\[(\d+)\]', re.M)
_BOILERPLATE_RE = (
    re.compile(r'<!--\s*BEGIN EVOLVMEM MEMORY\s*-->[\s\S]*?<!--\s*END EVOLVMEM MEMORY\s*-->', re.I),
    re.compile(r'#\s*AGENTS\.md instructions[^\n]*\n(?:<INSTRUCTIONS>[\s\S]*?</INSTRUCTIONS>)?', re.I),
    re.compile(r'<INSTRUCTIONS>[\s\S]*?</INSTRUCTIONS>', re.I),
    re.compile(r'<environment_context>[\s\S]*?</environment_context>', re.I),
    re.compile(r'<permissions instructions>[\s\S]*?</permissions instructions>', re.I),
    re.compile(r'<collaboration_mode>[\s\S]*?</collaboration_mode>', re.I),
    re.compile(r'<external_codex_apps_[^>]+>[\s\S]*?</external_codex_apps_[^>]+>', re.I),
)


def _clean_boilerplate(text: str) -> str:
    out = str(text or '')
    for pat in _BOILERPLATE_RE:
        out = pat.sub('', out)
    return re.sub(r'\n{3,}', '\n\n', out).strip()


def _clip(text: str, limit: int = 600) -> str:
    s = str(text or '').strip()
    return s if len(s) <= limit else s[: max(1, limit - 3)] + '...'


def _scrub(text: Any, *, scrub_fn: Callable[..., str], terms: Sequence[str]) -> str:
    return scrub_fn(text, extra_terms=terms)


def _extract_raw_meta(raw_bytes: bytes | None) -> dict[str, str]:
    meta = {'originator': '', 'cli_version': '', 'agent_model': '', 'cwd': '', 'source_kind': ''}
    if not raw_bytes:
        return meta
    for line in raw_bytes.decode('utf-8', errors='replace').splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        payload = obj.get('payload') if isinstance(obj.get('payload'), dict) else {}
        rtype = obj.get('type')
        if rtype == 'session_meta':
            for k, pk in (('originator', 'originator'), ('cli_version', 'cli_version'), ('cwd', 'cwd'), ('agent_model', 'model')):
                meta[k] = str(payload.get(pk) or meta[k])
            src = payload.get('source')
            meta['source_kind'] = ','.join(str(k) for k in src.keys()) if isinstance(src, dict) else str(src or meta['source_kind'])
        elif rtype == 'turn_context':
            meta['agent_model'] = meta['agent_model'] or str(payload.get('model') or '')
            meta['cwd'] = meta['cwd'] or str(payload.get('cwd') or '')
        if all(meta[k] for k in ('originator', 'cli_version', 'agent_model', 'cwd')):
            break
    return meta


def _format_turn(ev: dict[str, Any], *, scrub_fn: Callable[..., str], terms: Sequence[str]) -> dict[str, Any] | None:
    idx = int(ev.get('index', 0))
    kind = str(ev.get('kind') or '')
    if kind == 'message':
        role = str(ev.get('role') or 'unknown')
        raw_txt = str(ev.get('text') or '')
        cleaned = _clean_boilerplate(raw_txt) if role == 'user' else raw_txt.strip()
        if not cleaned:
            return None
        label = 'User' if role == 'user' else ('Assistant' if role == 'assistant' else role)
        return {'index': idx, 'kind': kind, 'label': label, 'text': _scrub(_clip(cleaned, 600), scrub_fn=scrub_fn, terms=terms)}
    if kind == 'tool_call':
        tool = str(ev.get('tool') or 'tool')
        body = ev.get('command') or ev.get('path') or json.dumps({k: v for k, v in ev.items() if k not in ('index', 'kind', 'call_id')}, ensure_ascii=False)
        return {'index': idx, 'kind': kind, 'label': f'ToolCall:{tool}', 'text': _scrub(_clip(str(body), 600), scrub_fn=scrub_fn, terms=terms)}
    if kind == 'tool_result':
        return {'index': idx, 'kind': kind, 'label': f"ToolResult(exit={ev.get('exit_code')})", 'text': _scrub(_clip(str(ev.get('text') or ''), 600), scrub_fn=scrub_fn, terms=terms)}
    return None


def _select_turns(parsed, moments, mkind: str, ev_ptr: int, *, scrub_fn: Callable[..., str], terms: Sequence[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    matched = [m for m in moments if m.kind == mkind]
    moment = min(matched, key=lambda m: abs(m.event_range[0] - ev_ptr)) if matched else None
    lo, hi = moment.event_range if moment else (max(0, ev_ptr - 2), ev_ptr + 2)
    by_idx = {int(ev.get('index', 0)): ev for ev in parsed.events}
    init_task, prec_prompt = '', ''
    anchor = max(hi, ev_ptr)
    for ev in parsed.events:
        if ev.get('kind') == 'message' and ev.get('role') == 'user':
            cleaned = _clean_boilerplate(str(ev.get('text') or ''))
            if not cleaned:
                continue
            if not init_task:
                init_task = _scrub(_clip(cleaned, 400), scrub_fn=scrub_fn, terms=terms)
            if int(ev.get('index', 0)) <= anchor:
                prec_prompt = _scrub(_clip(cleaned, 400), scrub_fn=scrub_fn, terms=terms)

    selected: set[int] = set()
    snippet_idxs = [int(x) for x in _SNIPPET_IDX_RE.findall(moment.snippet_text)] if moment else []
    for idx in range(max(0, lo - 3), min(hi + 4, lo + 20)):
        if idx in by_idx:
            selected.add(idx)
    for idx in snippet_idxs:
        for near in (idx - 1, idx, idx + 1):
            if near in by_idx:
                selected.add(near)
    if mkind == 'user_correction':
        prior = 0
        for idx in range(lo - 1, -1, -1):
            ev = by_idx.get(idx)
            if ev and ev.get('kind') == 'message' and _format_turn(ev, scrub_fn=scrub_fn, terms=terms) is not None:
                selected.add(idx)
                prior += 1
                if prior >= 2:
                    break
    turns = [t for idx in sorted(selected) if idx in by_idx and (t := _format_turn(by_idx[idx], scrub_fn=scrub_fn, terms=terms)) is not None]
    if len(turns) > 12:
        s_set = set(snippet_idxs)
        pri = [t for t in turns if t['index'] in s_set][:8]
        oth = [t for t in turns if t['index'] not in s_set]
        chosen = {t['index']: t for t in (pri + oth[:2] + oth[-2:])}
        turns = [chosen[k] for k in sorted(chosen)][:12]
    info = {
        'moment_title': _scrub(moment.title, scrub_fn=scrub_fn, terms=terms) if moment else '',
        'moment_event_range': [lo, hi],
        'initial_user_task': init_task,
        'preceding_user_prompt': prec_prompt,
        'turns': turns,
    }
    return info, turns


def enrich_pilot_samples(
    samples: list[dict[str, Any]],
    *,
    history_reader: HistoryReader,
    run_id: str,
    gate_traces: dict[tuple[str, str, str], dict[str, Any]],
    scrub_fn: Callable[..., str],
    extra_scrub_terms: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Attach scrubbed Agent provenance, verbatim context turns, and L2/L3 trace to pilot samples."""
    sessions_cache: dict[str, Any] = {}
    win_users: set[str] = set()
    for s in samples:
        raw_sid = str(s.get('_raw_session_id') or s.get('session_ref') or '')
        if raw_sid and raw_sid not in sessions_cache:
            sess = history_reader.get_session(raw_sid)
            sessions_cache[raw_sid] = sess
            if sess is not None:
                raw_meta = _extract_raw_meta(sess.raw_bytes)
                for m in _WIN_USER_RE.finditer(raw_meta.get('cwd') or ''):
                    win_users.add(m.group(1))
                for rec in sess.records[:20]:
                    for m in _WIN_USER_RE.finditer(rec.content or ''):
                        win_users.add(m.group(1))
    terms = sorted({str(t) for t in (*extra_scrub_terms, *win_users) if t and t != 'owner' and len(str(t)) >= 2})

    parsed_cache: dict[str, tuple[Any, list[Any], dict[str, str]]] = {}
    for raw_sid, sess in sessions_cache.items():
        if sess is None:
            continue
        parsed_cache[raw_sid] = (parse_session_events(sess), detect_key_moments(parse_session_events(sess)), _extract_raw_meta(sess.raw_bytes))

    for s in samples:
        raw_sid = str(s.pop('_raw_session_id', '') or s.get('session_ref') or '')
        raw_prob = str(s.pop('_raw_problem', '') or s.get('problem') or '')
        sess = sessions_cache.get(raw_sid)
        ev_list = s.get('evidence') or []
        ev_ptr = int(ev_list[0].get('event', 1)) if ev_list and isinstance(ev_list[0], dict) else 1
        if sess is not None and raw_sid in parsed_cache:
            parsed, moments, rmeta = parsed_cache[raw_sid]
            cwd_raw = rmeta.get('cwd') or ''
            proj = Path(cwd_raw.replace('\\', '/')).name if cwd_raw else (sess.summary.project or '')
            s['source_meta'] = {
                'adapter': sess.summary.adapter,
                'source_format': sess.summary.source_format,
                'originator': _scrub(rmeta.get('originator') or sess.summary.adapter, scrub_fn=scrub_fn, terms=terms),
                'cli_version': rmeta.get('cli_version') or '',
                'agent_model': rmeta.get('agent_model') or '',
                'source_kind': rmeta.get('source_kind') or '',
                'project_name': _scrub(proj, scrub_fn=scrub_fn, terms=terms),
                'cwd': _scrub(cwd_raw, scrub_fn=scrub_fn, terms=terms),
                'created_at': sess.summary.created_at,
            }
            ctx_info, _ = _select_turns(parsed, moments, str(s.get('moment_kind') or ''), ev_ptr, scrub_fn=scrub_fn, terms=terms)
            s['context_excerpt'] = ctx_info
        else:
            s['source_meta'] = {'adapter': '', 'source_format': '', 'originator': '', 'cli_version': '', 'agent_model': '', 'source_kind': '', 'project_name': '', 'cwd': '', 'created_at': ''}
            s['context_excerpt'] = {'moment_title': '', 'moment_event_range': [ev_ptr, ev_ptr], 'initial_user_task': '', 'preceding_user_prompt': '', 'turns': []}

        tr_raw = gate_traces.get((run_id, raw_sid, raw_prob)) or {}
        s['gate_trace'] = {
            k: (_scrub(v, scrub_fn=scrub_fn, terms=terms) if isinstance(v, str) else v)
            for k, v in tr_raw.items()
        }
        for fk in ('session_ref', 'problem', 'solution', 'reusable_experience', 'misbelief', 'reason_code'):
            s[fk] = _scrub(s.get(fk, ''), scrub_fn=scrub_fn, terms=terms)
    return samples
