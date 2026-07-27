"""Three-factor memory scoring: importance + recency decay + access frequency.

Modelled on the Generative Agents retrieval score (recency/importance/relevance).
SessionStart injection has no query, so relevance is replaced by usage frequency.
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


def compute_score(memory: dict, config: Config,
                  now: datetime | None = None) -> float:
    """Compute injection priority score in [0, w_i + w_r + w_f].

    Factors (each normalized to [0, 1] before weighting):
    - importance: memory["importance"] / 10
    - recency: exp(-age_days / inject_recency_tau_days), age from
      last_accessed, falling back to updated_at
    - frequency: log1p(access_count) / log1p(inject_freq_norm_cap), capped at 1
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
            + config.inject_w_frequency * frequency_norm)
