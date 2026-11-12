"""Policy regression tests; all fixtures are isolated from production data."""
import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from tests.test_lan_sharing import lan


def policy_module():
    assert importlib.util.find_spec('evolvmem.product_policy') is not None, 'runtime product policy is missing'
    return importlib.import_module('evolvmem.product_policy')


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / 'product-policy.json'
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(path))
    path.write_text(json.dumps({'console_enabled': True, 'legacy_web_enabled': False,
                                'ai_processing_enabled': False}))
    return path


def test_runtime_policy_reloads_without_restarting_consumer(policy_file):
    p = policy_module()
    assert p.load_policy().ai_processing_enabled is False
    policy_file.write_text(json.dumps({'ai_processing_enabled': True}))
    assert p.load_policy().ai_processing_enabled is True


def test_unconfigured_policy_preserves_legacy_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv('EVOLVMEM_PRODUCT_POLICY_FILE', raising=False)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    p = policy_module().load_policy()
    assert (p.console_enabled, p.legacy_web_enabled, p.ai_processing_enabled, p.valid) == (False, True, True, True)


@pytest.mark.parametrize('body', ['{', '[]', '{"ai_processing_enabled":"false"}', '{"unknown":true}'])
def test_invalid_configured_policy_fails_closed(policy_file, body):
    policy_file.write_text(body)
    p = policy_module().load_policy()
    assert not p.valid
    assert not any((p.console_enabled, p.legacy_web_enabled, p.ai_processing_enabled))


def test_missing_explicit_policy_is_not_silent_enable(policy_file):
    policy_file.unlink()
    assert not policy_module().load_policy().ai_processing_enabled


def test_provider_guard_blocks_cached_credentials_before_network(policy_file, monkeypatch):
    p = policy_module()
    from evolvmem import kimi_hooks as kh
    monkeypatch.setattr(kh.urllib.request, 'build_opener', lambda *a: pytest.fail('provider network request escaped pause'))
    credentials = kh.LLMConfig('deepseek', 'test-only', 'https://invalid.example', 'fixture')
    with pytest.raises(p.AIProcessingDisabled, match='ai_processing_disabled'):
        kh._call_llm_with_retry('private fixture prompt', credentials)


def test_paused_lan_work_is_not_claimed_or_daily_marked_complete(policy_file):
    from evolvmem.lan_tools import LanTools
    from evolvmem.lan_capture import LanCapture
    from evolvmem.lan_daily_rollup import LanDailyRollup
    fail = lambda *a, **k: pytest.fail('paused worker touched queue or state')
    assert LanTools.process_pending(SimpleNamespace(captures={'bad': SimpleNamespace(claim_pending=fail)})) == 0
    assert LanCapture.claim_pending(SimpleNamespace(conn=SimpleNamespace(execute=fail))) is None
    daily = LanDailyRollup.__new__(LanDailyRollup)
    daily._check_lock = SimpleNamespace(acquire=fail)
    assert daily.check() is False


def test_paused_organization_never_opens_derived_store(policy_file):
    from evolvmem.auto_organization import OrganizationWorker
    worker = OrganizationWorker(SimpleNamespace())
    worker._connect = lambda: pytest.fail('disabled legacy organizer opened store')
    assert worker.tick() is False


def test_local_raw_scan_does_not_depend_on_auto_new(policy_file, monkeypatch):
    from evolvmem import local_codex_capture as local
    service = SimpleNamespace(config=SimpleNamespace(data_dir=policy_file.parent), store=object())
    class Capture:
        ready = True
        def __init__(self, *args): pass
        def poll_seconds(self): return 1
        def scan(self): return {'archives': 2}
    monkeypatch.setattr(local, 'LocalCodexCapture', Capture)
    assert local.capture_once(service, {}, now=10, arrival_required=False) == 2


def test_new_console_organization_does_not_duplicate_local_raw_scan(policy_file):
    from evolvmem.auto_organization import OrganizationWorker
    worker = OrganizationWorker(SimpleNamespace())
    worker._connect = lambda: pytest.fail('legacy worker attempted duplicate raw scan')
    assert worker.capture_once() is False


def test_kimi_archives_before_pause_and_keeps_ai_unfinished(policy_file, monkeypatch):
    from evolvmem import kimi_hooks as kh
    import evolvmem.raw_hook_archive as raw
    monkeypatch.setattr(kh, '_find_wire', lambda sid: '/fixture/wire.jsonl')
    messages = [{'role': 'user', 'content': 'keep the raw input'}]
    monkeypatch.setattr(kh, '_read_messages', lambda wire: messages)
    seen = []
    monkeypatch.setattr(raw, 'archive_hook_messages', lambda **args: seen.append(args) or 17)
    monkeypatch.setattr(kh, '_load_llm_config', lambda **kw: pytest.fail('paused hook loaded provider'))
    result = kh.session_end({'session_id': 'fixture'})
    assert result.status == 'paused' and result.reason == 'ai_processing_disabled'
    assert seen[0]['messages'] == messages and seen[0]['adapter'] == 'kimi'


def test_dsh_archives_before_pause_and_keeps_ai_unfinished(policy_file, monkeypatch):
    from evolvmem import dsh_bridge, kimi_hooks
    import evolvmem.raw_hook_archive as raw
    seen = []
    monkeypatch.setattr(raw, 'archive_hook_messages', lambda **args: seen.append(args) or 18)
    monkeypatch.setattr(kimi_hooks, '_load_llm_config', lambda **kw: pytest.fail('paused DSH loaded provider'))
    status, details = dsh_bridge.extract_from_messages([{'role': 'user', 'content': 'raw'}], 'fixture', 'demo')
    assert status == 'paused' and details['reason'] == 'ai_processing_disabled'
    assert seen[0]['adapter'] == 'dsh'


def test_real_raw_upload_and_continuity_stay_available_while_ai_paused(policy_file, lan):
    from tests.test_lan_capture import upload, transcript
    from evolvmem.session_archive import SessionArchiver
    runtime, adapter = lan
    begun = adapter.call_tool('jiangli', 'continuity_begin', {
        'project': 'demo', 'workspace_path': r'C:\work\demo', 'device_id': 'windows-main',
        'objective': 'keep real raw evidence and checkpoint', 'request_id': 'policy-continuity'})
    assert 'workstream_id' in begun, begun
    raw = transcript()
    saved = upload(adapter, raw, extract=True)
    assert saved['status'] == 'archived' and saved['processing_state'] == 'paused'
    assert saved['extraction_status'] == 'pending'
    assert adapter.process_pending() == 0
    server = runtime.server_for('jiangli')
    original = json.loads(SessionArchiver(server.config, server.context_service.store).read_payload(saved['archive_id']))
    assert original['transcript'] == raw.decode()
    row = adapter.call_tool('jiangli', 'session_archive_status', {'device_id': 'windows-main', 'session_id': 'session-1'})
    assert row['extraction_status'] == 'pending' and 'processing_error' not in row
    resumed = adapter.call_tool('jiangli', 'continuity_resume', {
        'workspace_path': r'C:\work\demo', 'project_hint': 'demo', 'device_id': 'windows-main'})
    assert 'error' not in resumed
    assert adapter.call_tool('kane', 'session_archive_status', {'device_id': 'windows-main', 'session_id': 'session-1'})['status'] == 'not_found'


def test_raw_hook_archive_is_encrypted_and_replayable(policy_file, tmp_path):
    from evolvmem.config import Config
    from evolvmem.context_models import ContextMode
    from evolvmem.context_service import ContextService
    from evolvmem.raw_hook_archive import archive_hook_messages
    from evolvmem.session_archive import SessionArchiver
    config = Config(data_dir=tmp_path / 'raw-data', apply_environment=False)
    messages = [{'role': 'user', 'content': '原始证据必须保留。'}]
    aid = archive_hook_messages(config=config, project='demo', adapter='dsh', source_session='dsh:fixture', messages=messages)
    assert aid is not None
    service = ContextService(config)
    service.initialize(mode=ContextMode.LEGACY, adapter='fixture')
    try:
        payload = SessionArchiver(config, service.store).read_payload(aid)
        assert json.loads(payload)['messages'] == messages
        assert service.store._connection().execute('SELECT count(*) FROM context_items').fetchone()[0] == 0
        for file in config.data_dir.rglob('*.bin'):
            assert messages[0]['content'].encode() not in file.read_bytes()
    finally:
        service.close()


def test_capture_lease_blocks_another_live_scan_and_releases(tmp_path):
    from evolvmem.capture_lease import capture_lease
    with capture_lease(tmp_path) as first:
        assert first is True
        with capture_lease(tmp_path) as second:
            assert second is False
    with capture_lease(tmp_path) as third:
        assert third is True


def test_direct_review_tick_preserves_disabled_queue(policy_file):
    from evolvmem.organization_semantic_review import tick
    assert tick(SimpleNamespace()) is False


def test_paused_unit_cannot_publish_cached_derivations(policy_file):
    from evolvmem.pipeline_state import assert_unit_current
    with pytest.raises(policy_module().AIProcessingDisabled):
        assert_unit_current(SimpleNamespace(), {}, {})


def test_independent_raw_scan_archives_real_codex_bytes_with_arrival_off(policy_file, tmp_path):
    from tests.test_local_codex_capture import rows, write_session, write_config, NEW
    from evolvmem.config import Config
    from evolvmem.context_models import ContextMode
    from evolvmem.context_service import ContextService
    from evolvmem.local_codex_capture import capture_once
    from evolvmem.session_archive import SessionArchiver
    config = Config(data_dir=tmp_path / 'independent', apply_environment=False)
    service = ContextService(config)
    service.initialize(mode=ContextMode.SHADOW, adapter='policy-test')
    try:
        root = tmp_path / 'rollouts'
        root.mkdir()
        file = write_session(root, rows())
        write_config(service, root, since=NEW)
        from evolvmem.organization_arrival import update_settings
        update_settings(service, {'auto_new': False})
        assert capture_once(service, {}, now=10, arrival_required=False) == 1
        assert capture_once(service, {}, now=20, arrival_required=False) == 0
        archives = service.store._connection().execute('SELECT id FROM session_archives').fetchall()
        assert len(archives) == 1
        payload = json.loads(SessionArchiver(config, service.store).read_payload(archives[0][0]))
        assert payload['transcript'].encode() == file.read_bytes()
    finally:
        service.close()


def test_disabled_legacy_housekeeping_keeps_old_library_unchanged(policy_file, tmp_path, monkeypatch):
    from evolvmem import hooks
    from evolvmem.config import Config
    from evolvmem.context_models import ContextMode
    config = Config(data_dir=tmp_path / 'untouched-legacy', apply_environment=False)
    monkeypatch.setattr(hooks.ForgettingEngine, 'run', lambda self: pytest.fail('legacy forgetting ran'))
    hooks._maybe_run_forgetting(config, object())
    hooks._maybe_run_consolidation(config, object())
    hooks._maybe_sweep_archives(SimpleNamespace(sweep_archives=lambda **kw: pytest.fail('summary retirement ran')), ContextMode.PRIMARY)
    assert not (config.data_dir / '.last_forget').exists()
    assert not (config.data_dir / '.last_consolidate').exists()
