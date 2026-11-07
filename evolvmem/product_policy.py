"""Runtime product gates shared by HTTP, workers, hooks and provider calls.

No credentials, database access or process-wide cache. A configured malformed
or missing policy disables every gate; an absent default file preserves the
pre-console installation defaults.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path


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

    def as_dict(self):
        return {name: getattr(self, name) for name in
                ('console_enabled', 'legacy_web_enabled', 'ai_processing_enabled', 'valid')}


def load_policy() -> ProductPolicy:
    override = os.environ.get('EVOLVMEM_PRODUCT_POLICY_FILE')
    path = Path(override).expanduser() if override else Path.home() / '.config/evolvmem/product-policy.json'
    try:
        with path.open('rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError('policy_too_large')
        body = json.loads(raw)
        names = {'console_enabled', 'legacy_web_enabled', 'ai_processing_enabled'}
        if not isinstance(body, dict) or set(body) - names or any(type(v) is not bool for v in body.values()):
            raise ValueError('invalid_policy')
        return ProductPolicy(**body)
    except FileNotFoundError:
        if not override:
            return ProductPolicy()
    except (OSError, ValueError, UnicodeError):
        pass
    return ProductPolicy(False, False, False, False, 'policy_unavailable')


def require_ai_processing() -> None:
    if not load_policy().ai_processing_enabled:
        raise AIProcessingDisabled()


def legacy_automation_enabled() -> bool:
    policy = load_policy()
    return policy.legacy_web_enabled and policy.ai_processing_enabled
