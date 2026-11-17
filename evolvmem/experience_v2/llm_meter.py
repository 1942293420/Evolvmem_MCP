"""Single mandatory metered LLM entry, budget enforcement & static bypass guard."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import time
from typing import Any, Callable, Literal
import urllib.error
import urllib.request

VALID_TRACKS = frozenset({'live', 'backfill'})
_PROVIDER_DEFAULTS = {
    'deepseek': ('https://api.deepseek.com/chat/completions', 'deepseek-v4-flash'),
    'mimo': ('https://token-plan-cn.xiaomimimo.com/v1/chat/completions', 'mimo-v2.6-flash'),
    'kimi': ('https://api.kimi.com/coding/v1/chat/completions', 'kimi-for-coding'),
}
_FORBIDDEN_IMPORTS = frozenset({'urllib.request', 'requests', 'httpx', 'aiohttp', 'openai', 'anthropic'})
_FORBIDDEN_CALLS = frozenset({'urlopen', '_call_llm', '_call_llm_with_retry', '_call_single_provider_with_retry'})


class MeteringBypassError(RuntimeError):
    """Raised when an LLM call attempts to run without a DB metering handle."""


class BudgetDeferred(RuntimeError):
    """Raised when track, stage, session budget or paused circuit defers a call."""

    def __init__(self, scope: str, detail: str = ''):
        self.scope = scope
        self.detail = detail
        super().__init__(f'budget_deferred:{scope}:{detail}' if detail else f'budget_deferred:{scope}')


class LlmTransportError(RuntimeError):
    """Raised when the configured LLM transport fails; metered cleanly without leaking prompts."""


@dataclass(frozen=True)
class BudgetConfig:
    live_daily_tokens: int = 2_000_000
    backfill_daily_tokens: int = 3_000_000
    session_max_tokens: int = 30_000
    stage_daily_limits: dict[str, int] = field(default_factory=dict)

    def daily_limit(self, track: str) -> int:
        return self.live_daily_tokens if track == 'live' else self.backfill_daily_tokens


@dataclass(frozen=True)
class LlmEndpoint:
    provider: str
    model: str
    base_url: str
    api_key: str = field(default='', repr=False)


@dataclass(frozen=True)
class LlmResult:
    call_id: int
    track: str
    stage: str
    model: str
    text: str
    input_tokens: int
    output_tokens: int
    cost_estimate: float
    outcome: str = 'ok'

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def estimate_tokens(text: str | list[dict[str, Any]]) -> int:
    """Deterministic CJK + ASCII token estimator (never requires remote calls)."""
    if isinstance(text, list):
        joined = '\n'.join(str(m.get('content') or '') for m in text if isinstance(m, dict))
    else:
        joined = str(text or '')
    if not joined:
        return 0
    cjk = sum(1 for ch in joined if '\u4e00' <= ch <= '\u9fff')
    other = len(joined) - cjk
    return max(1, math.ceil(cjk * 1.15 + other / 3.8))


def load_llm_endpoints_from_file(path: Path | None = None) -> dict[str, LlmEndpoint]:
    """Load configured flash & strong endpoints without caching keys in module globals."""
    cfg_path = path or (Path.home() / '.claude' / 'evolvmem' / 'llm_credentials.json')
    if not cfg_path.is_file():
        return {}
    raw = json.loads(cfg_path.read_text(encoding='utf-8'))
    out: dict[str, LlmEndpoint] = {}

    def parse(slot: dict[str, Any] | None) -> LlmEndpoint | None:
        if not isinstance(slot, dict):
            return None
        provider = str(slot.get('provider') or 'deepseek').strip().casefold()
        default_url, default_model = _PROVIDER_DEFAULTS.get(provider, ('', ''))
        model = str(slot.get('model') or default_model).strip()
        base_url = str(slot.get('base_url') or default_url).strip()
        api_key = str(slot.get('api_key') or '').strip()
        if not model or not base_url or not api_key:
            return None
        return LlmEndpoint(provider=provider, model=model, base_url=base_url, api_key=api_key)

    primary = parse(raw)
    if primary is not None:
        out['primary'] = primary
        out['flash'] = primary
    fallback = parse(raw.get('fallback'))
    if fallback is not None:
        out['fallback'] = fallback
        out['strong'] = parse(raw.get('strong')) or fallback
    elif 'strong' in raw and parse(raw.get('strong')) is not None:
        out['strong'] = parse(raw.get('strong'))  # type: ignore[assignment]
    return out


def default_http_transport(
    endpoint: LlmEndpoint,
    *,
    timeout_seconds: float = 45.0,
    disable_thinking: bool = True,
) -> Callable[[str, list[dict[str, Any]], int], dict[str, Any]]:
    """Build an OpenAI-compatible HTTP transport bound to one LlmEndpoint."""

    def _send(model: str, messages: list[dict[str, Any]], max_output_tokens: int) -> dict[str, Any]:
        active_model = model or endpoint.model
        payload: dict[str, Any] = {
            'model': active_model,
            'messages': messages,
            'temperature': 0.2,
        }
        if endpoint.provider != 'mimo' and disable_thinking and 'reasoner' not in active_model.casefold():
            payload['thinking'] = {'type': 'disabled'}
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if endpoint.provider == 'mimo':
            payload['max_completion_tokens'] = int(max_output_tokens)
            headers['api-key'] = endpoint.api_key
        else:
            payload['max_tokens'] = int(max_output_tokens)
            headers['Authorization'] = f'Bearer {endpoint.api_key}'
        req = urllib.request.Request(
            endpoint.base_url,
            data=json.dumps(payload).encode('utf-8'),
            headers=headers,
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise LlmTransportError(type(exc).__name__) from None
        choice = (data.get('choices') or [{}])[0]
        msg = choice.get('message') if isinstance(choice, dict) else {}
        text = str((msg or {}).get('content') or '')
        usage = data.get('usage') if isinstance(data.get('usage'), dict) else {}
        in_tok = int(usage.get('prompt_tokens') or estimate_tokens(messages))
        out_tok = int(usage.get('completion_tokens') or estimate_tokens(text))
        return {
            'text': text,
            'input_tokens': in_tok,
            'output_tokens': out_tok,
            'cost_estimate': round((in_tok + out_tok) * 1e-6, 6),
        }

    return _send


def _check_circuit_and_budget(
    conn,
    *,
    track: str,
    stage: str,
    session_id: str | None,
    owner_id: str | None,
    estimated_in: int,
    max_output_tokens: int,
    budget: BudgetConfig,
) -> None:
    if owner_id:
        cb = conn.execute(
            'SELECT paused, pause_reason FROM pipeline_circuit_state WHERE owner_id = %s AND track = %s',
            (owner_id, track),
        ).fetchone()
        if cb and cb[0]:
            raise BudgetDeferred('circuit_paused', str(cb[1] or ''))

    daily_used = int(conn.execute(
        """
        SELECT COALESCE(SUM(input_tokens + output_tokens), 0)
        FROM llm_calls
        WHERE track = %s AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
        """,
        (track,),
    ).fetchone()[0] or 0)
    if daily_used + estimated_in > budget.daily_limit(track):
        raise BudgetDeferred('daily', f'used={daily_used},limit={budget.daily_limit(track)}')

    stage_limit = budget.stage_daily_limits.get(stage)
    if stage_limit is not None:
        stage_used = int(conn.execute(
            """
            SELECT COALESCE(SUM(input_tokens + output_tokens), 0)
            FROM llm_calls
            WHERE track = %s AND stage = %s AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            """,
            (track, stage),
        ).fetchone()[0] or 0)
        if stage_used + estimated_in > int(stage_limit):
            raise BudgetDeferred('stage', f'stage={stage},used={stage_used},limit={stage_limit}')

    if session_id:
        sess_used = int(conn.execute(
            'SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM llm_calls WHERE track = %s AND session_id = %s',
            (track, session_id),
        ).fetchone()[0] or 0)
        if sess_used + estimated_in + max_output_tokens > budget.session_max_tokens:
            raise BudgetDeferred('session', f'used={sess_used},limit={budget.session_max_tokens}')


def metered_llm(
    *,
    track: Literal['live', 'backfill'],
    stage: str,
    model: str,
    messages: list[dict[str, Any]],
    session_id: str | None,
    max_output_tokens: int,
    conn=None,
    owner_id: str | None = None,
    proposal_uid: str | None = None,
    is_shadow: bool = False,
    budget: BudgetConfig | None = None,
    transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]] | None = None,
) -> LlmResult:
    """Only allowed LLM entry: enforces circuit + budget before calling, logs every call to llm_calls."""
    if track not in VALID_TRACKS:
        raise ValueError(f'invalid_track:{track}')
    if not stage or not model or max_output_tokens <= 0:
        raise ValueError('invalid_llm_call_parameters')
    if conn is None:
        raise MeteringBypassError('llm_meter_conn_required')
    if transport is None:
        raise MeteringBypassError('llm_transport_required')

    cfg = budget or BudgetConfig()
    estimated_in = estimate_tokens(messages)
    _check_circuit_and_budget(
        conn,
        track=track,
        stage=stage,
        session_id=session_id,
        owner_id=owner_id,
        estimated_in=estimated_in,
        max_output_tokens=max_output_tokens,
        budget=cfg,
    )

    started = time.monotonic()
    try:
        raw = transport(model, messages, max_output_tokens)
    except BudgetDeferred:
        raise
    except Exception as exc:
        err_tok = max(0, estimated_in)
        conn.execute(
            """
            INSERT INTO llm_calls(track, stage, model, session_id, input_tokens, output_tokens, cost_estimate, outcome, proposal_uid, is_shadow)
            VALUES (%s, %s, %s, %s, %s, 0, 0, %s, %s, %s)
            """,
            (track, stage, model, session_id, err_tok, f'error:{type(exc).__name__}', proposal_uid, bool(is_shadow)),
        )
        raise LlmTransportError(type(exc).__name__) from None
    _ = started

    text = str(raw.get('text') or '')
    in_tok = max(0, int(raw.get('input_tokens') if raw.get('input_tokens') is not None else estimated_in))
    out_tok = max(0, int(raw.get('output_tokens') if raw.get('output_tokens') is not None else estimate_tokens(text)))
    cost = float(raw.get('cost_estimate') if raw.get('cost_estimate') is not None else round((in_tok + out_tok) * 1e-6, 6))
    outcome = str(raw.get('outcome') or 'ok')

    row = conn.execute(
        """
        INSERT INTO llm_calls(track, stage, model, session_id, input_tokens, output_tokens, cost_estimate, outcome, proposal_uid, is_shadow)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (track, stage, model, session_id, in_tok, out_tok, cost, outcome, proposal_uid, bool(is_shadow)),
    ).fetchone()
    return LlmResult(
        call_id=int(row[0]),
        track=track,
        stage=stage,
        model=model,
        text=text,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_estimate=cost,
        outcome=outcome,
    )


def scan_unmetered_llm_calls(root: Path) -> list[dict[str, Any]]:
    """AST & pattern scan ensuring no file under root bypasses llm_meter.metered_llm()."""
    violations: list[dict[str, Any]] = []
    for path in sorted(Path(root).rglob('*.py')):
        if path.name == 'llm_meter.py' or '__pycache__' in path.parts:
            continue
        source = path.read_text(encoding='utf-8')
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _FORBIDDEN_IMPORTS or alias.name.startswith('urllib.request'):
                        violations.append({'file': str(path), 'line': node.lineno, 'kind': 'forbidden_import', 'name': alias.name})
            elif isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module == imp or node.module.startswith(imp + '.') for imp in _FORBIDDEN_IMPORTS) or node.module == 'evolvmem.kimi_hooks':
                    violations.append({'file': str(path), 'line': node.lineno, 'kind': 'forbidden_import', 'name': node.module})
            elif isinstance(node, ast.Call):
                fn_name = node.func.attr if isinstance(node.func, ast.Attribute) else (node.func.id if isinstance(node.func, ast.Name) else '')
                if fn_name in _FORBIDDEN_CALLS:
                    violations.append({'file': str(path), 'line': node.lineno, 'kind': 'forbidden_call', 'name': fn_name})
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if 'chat/completions' in node.value:
                    violations.append({'file': str(path), 'line': node.lineno, 'kind': 'forbidden_endpoint', 'name': 'chat/completions'})
    return violations
