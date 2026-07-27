"""scoring module tests."""

from datetime import datetime, timezone
from evolvmem.scoring import compute_score


def _mem(importance=5.0, access_count=0, last_accessed=None, updated_at=None):
    return {
        "importance": importance,
        "access_count": access_count,
        "last_accessed": last_accessed,
        "updated_at": updated_at or "2026-07-28 00:00:00",
    }


NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)


class TestComputeScore:
    def test_higher_importance_scores_higher(self, test_config):
        low = compute_score(_mem(importance=3.0), test_config, now=NOW)
        high = compute_score(_mem(importance=9.0), test_config, now=NOW)
        assert high > low

    def test_recency_decays_with_age(self, test_config):
        recent = _mem(last_accessed="2026-07-27 00:00:00")
        old = _mem(last_accessed="2026-06-28 00:00:00")
        assert compute_score(recent, test_config, now=NOW) > \
               compute_score(old, test_config, now=NOW)

    def test_recency_uses_last_accessed_over_updated_at(self, test_config):
        # updated_at 很老但昨天被访问过 → 仍按新鲜算
        m = _mem(last_accessed="2026-07-27 00:00:00",
                 updated_at="2026-01-01 00:00:00")
        recent = _mem(last_accessed="2026-07-27 00:00:00")
        assert compute_score(m, test_config, now=NOW) == \
               compute_score(recent, test_config, now=NOW)

    def test_frequency_rewards_access_count(self, test_config):
        cold = compute_score(_mem(access_count=0), test_config, now=NOW)
        hot = compute_score(_mem(access_count=10), test_config, now=NOW)
        assert hot > cold

    def test_frequency_capped(self, test_config):
        # 超过 norm cap 后不再增长
        a = compute_score(_mem(access_count=1000000), test_config, now=NOW)
        b = compute_score(_mem(access_count=test_config.inject_freq_norm_cap),
                          test_config, now=NOW)
        assert abs(a - b) < 1e-9

    def test_weights_respected(self, test_config):
        test_config.inject_w_importance = 1.0
        test_config.inject_w_recency = 0.0
        test_config.inject_w_frequency = 0.0
        score = compute_score(_mem(importance=8.0), test_config, now=NOW)
        assert abs(score - 0.8) < 1e-9

    def test_null_last_accessed_falls_back_to_updated_at(self, test_config):
        m = _mem(last_accessed=None, updated_at="2026-07-27 00:00:00")
        recent = _mem(last_accessed="2026-07-27 00:00:00")
        assert compute_score(m, test_config, now=NOW) == \
               compute_score(recent, test_config, now=NOW)
