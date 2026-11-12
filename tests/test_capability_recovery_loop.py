from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from evolvmem.capability_recovery import CapabilityVectorRecoveryLoop


def test_recovery_is_bounded_and_does_not_run_on_every_poll():
    now = [100]
    index = SimpleNamespace(reconcile=Mock(return_value={"status": "pending", "remaining": 100}),
                            vector_index=SimpleNamespace(close=Mock()))
    loop = CapabilityVectorRecoveryLoop(clock=lambda: now[0], batch_size=16, index_factory=lambda _: index)
    assert loop.tick([object()])[0]["remaining"] == 100
    index.reconcile.assert_called_once_with(limit=16)
    index.vector_index.close.assert_called_once()
    assert loop.tick([object()]) == ()
    now[0] += 60
    assert loop.due()
    assert loop.tick([object()])


def test_one_failed_namespace_does_not_stop_the_next_and_backoff_is_bounded():
    now = [0]
    fail = SimpleNamespace(reconcile=Mock(side_effect=OSError("private path hidden")), vector_index=SimpleNamespace(close=Mock()))
    good = SimpleNamespace(reconcile=Mock(return_value={"status": "ready"}), vector_index=SimpleNamespace(close=Mock()))
    loop = CapabilityVectorRecoveryLoop(clock=lambda: now[0], index_factory=lambda s: s)
    for _ in range(9):
        reports = loop.tick([fail, good])
        assert reports[0] == {"status": "failed", "processed": 0, "error": "OSError"}
        assert reports[1]["status"] == "ready"
        assert 60 <= loop.next_attempt_at - now[0] <= 900
        now[0] = loop.next_attempt_at
    fail.reconcile.side_effect = None
    fail.reconcile.return_value = {"status": "ready"}
    loop.tick([fail])
    assert loop.failures == 0
    assert loop.next_attempt_at - now[0] == 60


@pytest.mark.parametrize("args", [{"interval_seconds": 1}, {"batch_size": 0}, {"batch_size": 300}, {"batch_size": True}])
def test_invalid_unbounded_configuration_rejected(args):
    with pytest.raises(ValueError):
        CapabilityVectorRecoveryLoop(**args)
