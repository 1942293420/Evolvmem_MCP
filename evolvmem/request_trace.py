"""Bounded, content-free diagnostics for slow or failed recall requests."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import logging
from pathlib import Path
import re
import time
import traceback
import uuid

SLOW_MS = 1000
_current = ContextVar('recall_trace', default=None)
_STAGES = frozenset({'queue', 'dispatch', 'health', 'retrieval', 'lexical', 'vector', 'embedding', 'rerank',
                     'health_state', 'health_physical', 'health_projection', 'health_vector',
                     'health_documents', 'health_counts'})
HEALTH_STATE_FIELDS = (
    'service', 'connection', 'database_changes', 'data_version', 'schema_version',
    'transaction', 'config', 'configured_mode', 'configured_adapter', 'service_mode',
    'service_adapter', 'embedding', 'embedding_loaded', 'context_index', 'legacy_index',
    'physical_cache', 'files',
)
_HEALTH_EVENTS = frozenset({
    'physical_baseline_initial', 'physical_baseline_schema', 'physical_baseline_identity',
    'physical_uncached_transaction', 'physical_cache_disabled', 'physical_pending',
    'physical_failed_cached', 'generation_fallback', 'state_unavailable',
})


def health_event(name):
    trace = _current.get()
    if trace is None:
        return
    if name == 'cache_hit':
        trace['health_cache_hits'] = trace.get('health_cache_hits', 0) + 1
    elif name in _HEALTH_EVENTS:
        events = trace.setdefault('health_events', {})
        events[name] = events.get(name, 0) + 1


def health_evaluation(elapsed_ms, ready, changed):
    trace = _current.get()
    if trace is None:
        return
    trace['health_evaluations'] = trace.get('health_evaluations', 0) + 1
    trace['health_evaluate_ms'] = round(trace.get('health_evaluate_ms', 0) + elapsed_ms, 3)
    passes = trace.setdefault('health_passes', [])
    if len(passes) < 8:
        passes.append(dict(elapsed_ms=round(elapsed_ms, 3),
                           ready=ready if type(ready) is bool else None,
                           changed=[name for name in HEALTH_STATE_FIELDS if name in changed]))


def failure(exc, stage_name='dispatch'):
    trace = _current.get()
    if trace is None:
        return
    frames = [dict(file=Path(f.filename).name, line=f.lineno, function=f.name)
              for f in traceback.extract_tb(exc.__traceback__)
              if Path(f.filename).parent.name == 'evolvmem'][-4:]
    if trace['error'] is None:
        trace['error'] = dict(category=type(exc).__name__, stage=stage_name, frames=frames)
    elif len(frames) > len(trace['error']['frames']):
        trace['error']['frames'] = frames


@contextmanager
def stage(name):
    trace = _current.get()
    if trace is None or name not in _STAGES:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    except Exception as exc:
        failure(exc, name)
        raise
    finally:
        elapsed = (time.perf_counter()-started)*1000
        trace['stages_ms'][name] = round(trace['stages_ms'].get(name, 0)+elapsed, 3)


@contextmanager
def timed_lock(lock):
    with stage('queue'):
        lock.acquire()
    try:
        yield
    finally:
        lock.release()


@contextmanager
def request_trace(name):
    if _current.get() is not None:
        yield
        return
    name = name if isinstance(name, str) and re.fullmatch(r'[a-z_]{1,64}', name) else 'invalid'
    trace = dict(trace_id=uuid.uuid4().hex, tool=name, stages_ms={}, error=None)
    token = _current.set(trace)
    started = time.perf_counter()
    try:
        yield
    except Exception as exc:
        failure(exc)
        raise
    finally:
        trace['total_ms'] = round((time.perf_counter()-started)*1000, 3)
        _current.reset(token)
        if trace['error'] is not None or trace['total_ms'] >= SLOW_MS:
            logging.getLogger(__name__).warning('request_trace %s', json.dumps(trace))


def traced_tool(function):
    @wraps(function)
    def wrapped(self, user, name, *args, **kwargs):
        with request_trace(name):
            return function(self, user, name, *args, **kwargs)
    return wrapped


def traced_rpc(function):
    @wraps(function)
    def wrapped(self, user, request, *args, **kwargs):
        params = request.get('params', {}) if isinstance(request, dict) else {}
        name = params.get('name') if isinstance(params, dict) else None
        with request_trace(name):
            return function(self, user, request, *args, **kwargs)
    return wrapped
