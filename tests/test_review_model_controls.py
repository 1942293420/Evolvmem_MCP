"""Explicit model selection and truthful, credential-free review progress."""
import json
import pytest

from tests.test_review_result_cache import service, base_service, setup_job
from tests.test_llm_failover import pair
from evolvmem import kimi_hooks as hooks
from evolvmem import organization_semantic_review as semantic


def test_explicit_model_never_silently_switches():
    config = pair()
    assert hooks.select_llm_config(config, 'auto') is config
    primary = hooks.select_llm_config(config, 'primary')
    backup = hooks.select_llm_config(config, 'fallback')
    assert primary.provider == 'deepseek' and primary.fallback is None
    assert backup.provider == 'mimo' and backup.fallback is None
    with pytest.raises(ValueError, match='review_model_unavailable'):
        hooks.select_llm_config(primary, 'fallback')
    with pytest.raises(ValueError, match='invalid_review_model'):
        hooks.select_llm_config(config, 'browser-supplied-model')


@pytest.mark.parametrize('choice,provider', [('primary','deepseek'), ('fallback','mimo')])
def test_reasoner_receives_only_the_selected_provider(service, monkeypatch, choice, provider):
    from evolvmem import auto_organization, ownership_reasoning
    from tests.unit_model_fixture import UnitProvider
    setup_job(service, monkeypatch, count=2)
    seen = []
    def call(prompt, config, **kw):
        seen.append(config)
        return UnitProvider().classify(prompt)
    monkeypatch.setattr(auto_organization, '_load_llm', lambda: (pair(), call))
    units = list(semantic._units(service).values())
    result = ownership_reasoning.analyze(service, units, criteria='保留原始编号',
                                         target={'project':'evo', 'model_choice':choice})
    assert len(result) == 2 and seen
    assert all(c.provider == provider and c.fallback is None for c in seen)


def test_provider_trace_records_failure_then_backup_success(monkeypatch):
    events = []
    monkeypatch.setattr(hooks, '_PROVIDER_RETRY_AFTER', {})
    monkeypatch.setattr(hooks.time, 'sleep', lambda _: None)
    def call(prompt, config, **kw):
        if config.provider == 'deepseek':
            raise TimeoutError('sensitive payload must never be exposed')
        return 'ok'
    monkeypatch.setattr(hooks, '_call_llm', call)
    with hooks.llm_provider_status(events.append):
        assert hooks._call_llm_with_retry('private prompt', pair()) == 'ok'
    assert [(x['provider'], x['state']) for x in events] == [
        ('deepseek', 'running'), ('deepseek', 'failed'), ('mimo', 'running'), ('mimo', 'succeeded')]
    assert events[0]['call_id'] == events[1]['call_id']
    assert events[2]['call_id'] == events[3]['call_id'] != events[0]['call_id']
    assert events[-1]['elapsed_ms'] >= 0
    assert 'private' not in json.dumps(events) and 'sensitive' not in json.dumps(events)


def test_model_choices_are_safe_and_manual_change_does_not_reuse_old_verdicts(service, monkeypatch):
    _, request, calls = setup_job(service, monkeypatch, count=2)
    monkeypatch.setattr(hooks, '_load_llm_config', lambda **kw: pair())
    options = semantic.model_options()
    assert [x['id'] for x in options['items']] == ['auto', 'primary', 'fallback']
    assert 'test-key' not in json.dumps(options) and 'https://' not in json.dumps(options)
    first = semantic.start(service, {**request, 'model_choice':'primary'})
    semantic.tick(service)
    second = semantic.start(service, {**request, 'model_choice':'fallback'})
    assert second['id'] != first['id'] and second['processed'] == 0
    assert second['request']['model_choice'] == 'fallback'
    semantic.tick(service)
    assert len(calls) == 4
    assert semantic.start(service, {**request, 'model_choice':'fallback'})['id'] == second['id']


def test_review_keeps_model_call_results_and_freezes_completed_elapsed(service, monkeypatch):
    _, request, _ = setup_job(service, monkeypatch, count=2)
    original = semantic.judge
    def judging(*args):
        hooks._report_provider(pair().fallback, fallback=True, state='running', call_id='call1')
        hooks._report_provider(pair().fallback, fallback=True, state='succeeded', call_id='call1', elapsed_ms=1250)
        return original(*args)
    monkeypatch.setattr(semantic, 'judge', judging)
    job = semantic.start(service, request)
    semantic.tick(service)
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_review_jobs SET created_at='2026-10-19 00:00:00',updated_at='2026-10-19 00:00:12' WHERE id=?", (job['id'],))
    result = semantic.get(service, job['id'])
    assert result['elapsed_seconds'] == 12
    assert result['model_calls'] == [dict(provider='mimo', model='mimo-v2.6-flash', fallback=True,
                                         state='succeeded', call_id='call1', elapsed_ms=1250)]
    assert 'test-key' not in json.dumps(result)
