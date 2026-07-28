"""Memory scoring: importance + recency decay + access frequency + project relevance.

Modelled on the Generative Agents retrieval score (recency/importance/relevance).
SessionStart injection has no query, so usage frequency stands in for query
relevance; the relevance factor instead matches the session's working-directory
name (or its configured alias) against memory keys.
"""

import math
from datetime import datetime, timezone

from evolvmem.config import Config

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _relevance_norm(memory: dict, config: Config,
                    context: dict | None) -> float:
    """1.0 when the memory key mentions the current project (or its alias), else 0.0."""
    if not context:
        return 0.0
    project = context.get("project") or ""
    if not project:
        return 0.0
    alias = config.inject_project_aliases.get(project, project)
    key = memory.get("key") or ""
    return 1.0 if alias in key else 0.0


def compute_score(memory: dict, config: Config,
                  now: datetime | None = None,
                  context: dict | None = None) -> float:
    """Compute injection priority score in [0, w_i + w_r + w_f + w_rel].

    Factors (each normalized to [0, 1] before weighting):
    - importance: memory["importance"] / 10
    - recency: exp(-age_days / inject_recency_tau_days), age from
      last_accessed, falling back to updated_at
    - frequency: log1p(access_count) / log1p(inject_freq_norm_cap), capped at 1
    - relevance: 1 when the memory key mentions the session project
      (context["project"], resolved through inject_project_aliases), else 0;
      always 0 when context is None, so existing callers are unaffected
    """
    if now is None:
        now = datetime.now(timezone.utc)

    importance_norm = max(0.0, min(1.0, float(memory.get("importance") or 5.0) / 10.0))

    ts = _parse_ts(memory.get("last_accessed")) or _parse_ts(memory.get("updated_at"))
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0) if ts else 365.0
    recency_norm = math.exp(-age_days / config.inject_recency_tau_days)

    access_count = max(0, int(memory.get("access_count") or 0))
    cap = max(1, int(config.inject_freq_norm_cap))
    frequency_norm = min(1.0, math.log1p(access_count) / math.log1p(cap))

    return (config.inject_w_importance * importance_norm
            + config.inject_w_recency * recency_norm
            + config.inject_w_frequency * frequency_norm
            + config.inject_w_relevance * _relevance_norm(memory, config, context))
