"""Runtime product gates shared by HTTP, workers, hooks and provider calls.

No credentials, database access or process-wide cache. A configured malformed
or missing policy disables every gate; an absent default file preserves the
pre-console installation defaults.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path

_GATE_MODES = frozenset({'review', 'partial', 'auto'})
_GATE_CATEGORIES = frozenset({'前端经验', '业务经验', '系统环境经验'})
_BOOL_NAMES = frozenset({
    'console_enabled',
    'legacy_web_enabled',
    'ai_processing_enabled',
    'experience_v2_enabled',
    'experience_pipeline_enabled',
    'experience_backfill_enabled',
    'history_pg_enabled',
})
_ALL_NAMES = _BOOL_NAMES | {'experience_gate_mode'}


class AIProcessingDisabled(RuntimeError):
    """An intentional pause, never a retryable provider failure."""
    def __init__(self):
        super().__init__('ai_processing_disabled')


@dataclass(frozen=True)
class ProductPolicy:
    console_enabled: bool = False
    legacy_web_enabled: bool = True
    ai_processing_enabled: bool = True
    valid: bool = True
    source_error: str = ''
    experience_v2_enabled: bool = False
    experience_gate_mode: str | dict[str, str] = 'review'
    experience_pipeline_enabled: bool = False
    experience_backfill_enabled: bool = False
    history_pg_enabled: bool = False

    def as_dict(self):
        out = {
            name: getattr(self, name)
            for name in (
                'console_enabled',
                'legacy_web_enabled',
                'ai_processing_enabled',
                'valid',
                'experience_v2_enabled',
                'experience_gate_mode',
                'experience_pipeline_enabled',
                'experience_backfill_enabled',
                'history_pg_enabled',
            )
        }
        if isinstance(out['experience_gate_mode'], dict):
            out['experience_gate_mode'] = dict(out['experience_gate_mode'])
        return out


def _valid_gate_mode(value) -> bool:
    if isinstance(value, str):
        return value in _GATE_MODES
    if isinstance(value, dict) and value:
        return (
            set(value).issubset(_GATE_CATEGORIES)
            and all(isinstance(v, str) and v in _GATE_MODES for v in value.values())
        )
    return False


def load_policy() -> ProductPolicy:
    override = os.environ.get('EVOLVMEM_PRODUCT_POLICY_FILE')
    path = Path(override).expanduser() if override else Path.home() / '.config/evolvmem/product-policy.json'
    try:
        with path.open('rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError('policy_too_large')
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) - _ALL_NAMES:
            raise ValueError('invalid_policy')
        for name in _BOOL_NAMES & set(body):
            if type(body[name]) is not bool:
                raise ValueError('invalid_policy')
        if 'experience_gate_mode' in body:
            if not _valid_gate_mode(body['experience_gate_mode']):
                raise ValueError('invalid_policy')
            body = {
                **body,
                'experience_gate_mode': (
                    dict(body['experience_gate_mode'])
                    if isinstance(body['experience_gate_mode'], dict)
                    else body['experience_gate_mode']
                ),
            }
        return ProductPolicy(**body)
    except FileNotFoundError:
        if not override:
            return ProductPolicy()
    except (OSError, ValueError, UnicodeError):
        pass
    return ProductPolicy(False, False, False, False, 'policy_unavailable', False, 'review', False, False, False)


def require_ai_processing(*, task_history_grant=None) -> None:
    policy = load_policy()
    if task_history_grant is not None:
        from evolvmem.task_history_processing import TaskHistoryGrant
        if not policy.valid or type(task_history_grant) is not TaskHistoryGrant:
            raise AIProcessingDisabled()
        task_history_grant.assert_current()
        return
    if not policy.ai_processing_enabled:
        raise AIProcessingDisabled()


def legacy_automation_enabled() -> bool:
    policy = load_policy()
    return policy.legacy_web_enabled and policy.ai_processing_enabled
