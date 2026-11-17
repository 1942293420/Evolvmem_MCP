"""Real host/router scheduling, fake clock and catalog I/O; no database."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_transition as base
from evolvmem.library_host import LibraryHost
from evolvmem.library_foundation import AuthorityRouter, AuthorizationSnapshot, FoundationError
from evolvmem.library_catalog.adapter import RecallPinRefusal


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []
        self.on_sleep = lambda: None

    def monotonic(self):
        return self.now

    def sleep(self, amount):
        self.sleeps.append(amount)
        self.on_sleep()
        self.now += amount


class Guard:
    def __init__(self, clock, duration=10):
        self.clock = clock
        self.deadline = clock.now + duration
        self.cancelled = None

    def check(self):
        if self.cancelled is not None:
            raise self.cancelled
        if self.clock.now >= self.deadline:
            raise FoundationError('request_deadline_exceeded')

    def absolute_deadline(self):
        self.check()
        return self.deadline


class Catalog:
    def __init__(self, clock, snapshot):
        self.clock, self.snapshot = clock, snapshot
        self.observations, self.resolved = [], []
        self.active, self.exits = 0, 0

    @contextmanager
    def authorization_preflight(self, identity):
        self.observations.append(round(self.clock.now - 100, 6))
        self.active += 1
        try:
            def resolve(library_id):
                self.resolved.append(library_id)
                return self.snapshot(self.clock.now - 100, library_id, len(self.observations))
            yield resolve
        finally:
            self.active -= 1
            self.exits += 1


def snapshot(acl='updating', policy=7, library_id=base.L):
    return AuthorizationSnapshot(base.credential(),
        base.library(library_id, acl=acl, policy=policy,
                     project=base.P if library_id == base.L else base.P2), 'read')


def setup(clock, snapshots, *, duration=10):
    host = object.__new__(LibraryHost)
    host._embedding_provider = base.FakeProvider()
    host._config = object()
    host._model_contract = base.CONTRACT.key
    catalog = Catalog(clock, snapshots)
    router = AuthorityRouter(catalog, implemented_tools={'postgres': {'context_search'}})
    state = SimpleNamespace(router=router, guard=Guard(clock, duration),
        identity=base.RequestIdentity(base.A, base.C, base.D, 3, 2, 4),
        coordinator=object(), published=False)
    def no_open_preflight_while_waiting():
        assert catalog.active == 0
    clock.on_sleep = no_open_preflight_while_waiting
    return host, state, catalog


def invoke(host, state, clock, *, search=None, library_ids=None):
    searches, publications = [], []
    class Search:
        def __init__(self, *args, **kwargs):
            pass
        def search(self, routes, selections, request, **kwargs):
            searches.append(routes)
            if search is not None:
                search(routes, len(searches))
            kwargs['publish']('synthetic result')
            return 'ok'
    args = dict(query='synthetic query', project_key=base.P,
                library_ids=list(library_ids or [base.L]))
    with patch('evolvmem.library_host.time.monotonic', clock.monotonic), \
         patch('evolvmem.library_host.time.sleep', clock.sleep), \
         patch('evolvmem.pg_business_core.native_search.PgNativeSearch', Search):
        result = host._recall(state, 'context_search', args, publications.append)
    return result, searches, publications


def test_transition_ready_after_1_3_seconds_gets_observed_before_budget_exhausts():
    clock = Clock()
    host, state, catalog = setup(clock, lambda elapsed, lib, count:
        snapshot('updating' if elapsed < 1.3 else 'ready', 7 if elapsed < 1.3 else 8, lib))
    try:
        result, searches, publications = invoke(host, state, clock)
    except FoundationError as error:
        raise AssertionError(dict(code=error.code, observation_times=catalog.observations,
                                  elapsed=round(clock.now - 100, 6))) from error
    assert result == 'ok' and publications == ['synthetic result']
    assert len(searches) == 1 and host._embedding_provider.encodes == 1
    assert catalog.observations == [0, 0, .5, 1, 1.5]
    assert catalog.exits == 5 and catalog.active == 0
    assert max(clock.sleeps) <= .05


def test_initial_and_pin_refusal_keep_one_four_slot_absolute_schedule():
    clock = Clock()
    def snapshots(elapsed, lib, count):
        return snapshot('updating' if elapsed < 1 else 'ready',
                        7 if elapsed < 1.4 else 8, lib)
    host, state, catalog = setup(clock, snapshots)
    def pin_once(routes, count):
        if count == 1:
            raise RecallPinRefusal(routes, failed_route=routes[0])
    result, searches, publications = invoke(host, state, clock, search=pin_once)
    assert result == 'ok' and len(searches) == 2
    assert [routes[0].library.policy_epoch for routes in searches] == [7, 8]
    assert catalog.observations == [0, 0, .5, 1, 1.5]
    assert publications == ['synthetic result'] and host._embedding_provider.encodes == 1


def test_initial_three_slots_leave_only_one_after_pin_even_when_still_updating():
    clock = Clock()
    def snapshots(elapsed, lib, count):
        return snapshot('ready' if count == 4 else 'updating', 7 if count <= 4 else 8, lib)
    host, state, catalog = setup(clock, snapshots)
    searches = []
    def pin_once(routes, count):
        searches.append(routes)
        raise RecallPinRefusal(routes, failed_route=routes[0])
    with pytest.raises(FoundationError) as caught:
        invoke(host, state, clock, search=pin_once)
    assert caught.value.code == 'authority_changed'
    assert catalog.observations == [0, 0, .5, 1, 1.5]
    assert len(searches) == 1 and host._embedding_provider.encodes == 1


def test_real_router_permission_revocation_is_not_waited_away():
    clock = Clock()
    host, state, catalog = setup(clock, lambda elapsed, lib, count:
        snapshot(library_id=lib) if count < 3 else None)
    with pytest.raises(FoundationError) as caught:
        invoke(host, state, clock)
    assert caught.value.code == 'library_access_denied'
    assert len(catalog.observations) == 3 and host._embedding_provider.encodes == 0
    assert catalog.active == 0 and catalog.exits == 3


def test_cancel_during_fragmented_wait_preserves_error_and_stops_before_next_query():
    clock = Clock()
    host, state, catalog = setup(clock, lambda elapsed, lib, count: snapshot(library_id=lib))
    cancellation = FoundationError('request_deadline_exceeded')
    def cancel_after_150ms():
        assert catalog.active == 0
        if clock.now >= 100.15:
            state.guard.cancelled = cancellation
    clock.on_sleep = cancel_after_150ms
    with pytest.raises(FoundationError) as caught:
        invoke(host, state, clock)
    assert caught.value is cancellation
    assert len(catalog.observations) == 2 and host._embedding_provider.encodes == 0
    assert max(clock.sleeps) <= .05 and clock.now < 100.3


def test_short_original_deadline_scales_slots_and_is_never_extended():
    clock = Clock()
    host, state, catalog = setup(clock, lambda elapsed, lib, count: snapshot(library_id=lib), duration=.8)
    with pytest.raises(FoundationError) as caught:
        invoke(host, state, clock)
    assert caught.value.code == 'library_unavailable'
    assert catalog.observations == [0, 0, .2, .4, .6]
    assert state.guard.deadline == 100.8 and clock.now < state.guard.deadline
    assert host._embedding_provider.encodes == 0


def test_deadline_reached_during_wait_stops_without_another_observation():
    clock = Clock()
    host, state, catalog = setup(clock, lambda elapsed, lib, count: snapshot(library_id=lib), duration=.8)
    def delayed_wakeup():
        assert catalog.active == 0
        clock.now = 100.8
    clock.on_sleep = delayed_wakeup
    with pytest.raises(FoundationError) as caught:
        invoke(host, state, clock)
    assert caught.value.code == 'request_deadline_exceeded'
    assert len(catalog.observations) == 2 and state.guard.deadline == 100.8


def test_always_updating_never_runs_a_fifth_budget_observation_or_content():
    clock = Clock()
    host, state, catalog = setup(clock, lambda elapsed, lib, count: snapshot(library_id=lib))
    with pytest.raises(FoundationError) as caught:
        invoke(host, state, clock)
    assert caught.value.code == 'library_unavailable'
    assert catalog.observations == [0, 0, .5, 1, 1.5]
    assert catalog.exits == 5 and host._embedding_provider.encodes == 0


def test_late_ready_result_cannot_escape_transition_window():
    clock = Clock()
    def snapshots(elapsed, lib, count):
        if count == 5:
            clock.now = 102.1
            return snapshot('ready', 8, lib)
        return snapshot(library_id=lib)
    host, state, catalog = setup(clock, snapshots)
    with pytest.raises(FoundationError) as caught:
        invoke(host, state, clock)
    assert caught.value.code == 'library_unavailable'
    assert len(catalog.observations) == 5 and host._embedding_provider.encodes == 0


@pytest.mark.parametrize('change,code', [
    ('route_epoch', 'library_unavailable'), ('connection_ref', 'library_unavailable'),
    ('policy_regression', 'library_unavailable'), ('credential_epoch', 'unauthorized')])
def test_fresh_route_or_identity_changes_are_never_treated_as_a_completed_transition(change, code):
    clock = Clock()
    def snapshots(elapsed, lib, count):
        value = snapshot('updating' if count < 3 else 'ready', 7, lib)
        if count >= 3:
            if change == 'credential_epoch':
                value = replace(value, credential=replace(value.credential, epoch=4))
            else:
                changes = {'route_epoch': dict(route_epoch=12),
                           'connection_ref': dict(connection_ref='pg.other'),
                           'policy_regression': dict(policy_epoch=6)}[change]
                value = replace(value, library=replace(value.library, **changes))
        return value
    host, state, catalog = setup(clock, snapshots)
    with pytest.raises(FoundationError) as caught:
        invoke(host, state, clock)
    assert caught.value.code == code
    assert len(catalog.observations) == 3 and host._embedding_provider.encodes == 0


def test_generic_cleanup_failure_is_not_recovered_as_transition():
    clock = Clock()
    cleanup = FoundationError('control_cleanup_failed')
    host, state, catalog = setup(clock, lambda elapsed, lib, count: snapshot(library_id=lib))
    original = catalog.authorization_preflight
    @contextmanager
    def failing_cleanup(identity):
        with original(identity) as resolve:
            yield resolve
        if len(catalog.observations) == 3:
            raise cleanup
    catalog.authorization_preflight = failing_cleanup
    with pytest.raises(FoundationError) as caught:
        invoke(host, state, clock)
    assert caught.value is cleanup
    assert len(catalog.observations) == 3 and host._embedding_provider.encodes == 0
