"""TDD tests for Stage 4 Task 4.2: seeded 50-item random delete sample & scrubbing."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from evolvmem.experience_v2.pipeline.pilot import scrub_pilot_text
from evolvmem.experience_v2.clean.sampler import (
    assert_zero_sqlite_writes,
    sample_delete_manifest,
)


def _fake_delete_entries(n: int = 80) -> list[dict]:
    out = []
    layers = ('L0', 'L1', 'L2')
    reasons = (
        'legacy_inactive_status',
        'duplicate_conversation_snapshot',
        'l2_no_value_known',
    )
    for i in range(n):
        idx = i % 3
        out.append(
            {
                'legacy_id': f'capability_memories:{1000 + i}',
                'source_table': 'capability_memories',
                'legacy_status': 'active',
                'legacy_category': 'delivery',
                'problem': f'Item {i} on /home/alice/work host 192.168.1.99',
                'solution': f'Check Bearer sk-secretkey99999999 for item {i}',
                'raw_preview': f'Raw preview {i} from C:\\Users\\alice\\repo',
                'gate_layer': layers[idx],
                'reason_code': reasons[idx],
                'detail': 'filtered by stage4 clean',
            }
        )
    return out


def test_sample_delete_manifest_seeded_50_and_scrubbed():
    entries = _fake_delete_entries(80)
    s1 = sample_delete_manifest(entries, sample_size=50, seed=20261114, extra_scrub_terms=('alice',))
    s2 = sample_delete_manifest(entries, sample_size=50, seed=20261114, extra_scrub_terms=('alice',))

    assert len(s1) == 50
    assert s1 == s2  # reproducible seed
    assert len({x['legacy_id'] for x in s1}) == 50

    required_keys = {
        'sample_index', 'legacy_id', 'source_table', 'legacy_status', 'legacy_category',
        'problem', 'solution', 'raw_preview', 'gate_layer', 'reason_code', 'detail',
    }
    for row in s1:
        assert required_keys.issubset(row.keys())
    blob = json.dumps(s1, ensure_ascii=False)
    assert '192.168.1.99' not in blob
    assert 'sk-secretkey' not in blob
    assert '/home/alice' not in blob
    assert 'Users\\alice' not in blob


def test_sample_delete_manifest_handles_fewer_than_50():
    entries = _fake_delete_entries(12)
    s = sample_delete_manifest(entries, sample_size=50, seed=42)
    assert len(s) == 12


def test_assert_zero_sqlite_writes_detects_or_confirms_counts(tmp_path: Path):
    db_path = tmp_path / 's.db'
    c = sqlite3.connect(db_path)
    c.executescript(
        """
        CREATE TABLE context_items (id INTEGER PRIMARY KEY, content_type TEXT);
        CREATE TABLE capability_memories (memory_id INTEGER PRIMARY KEY);
        CREATE TABLE learning_memories (item_id INTEGER PRIMARY KEY);
        INSERT INTO context_items VALUES (1, 'experience');
        INSERT INTO capability_memories VALUES (10);
        INSERT INTO learning_memories VALUES (1);
        """
    )
    c.commit()
    c.close()

    pre_counts = {'context_items_experience': 1, 'capability_memories': 1, 'learning_memories': 1}
    assert_zero_sqlite_writes(db_path, pre_counts)

    c = sqlite3.connect(db_path)
    c.execute('DELETE FROM capability_memories WHERE memory_id = 10')
    c.commit()
    c.close()
    with pytest.raises(RuntimeError, match='SQLITE_COUNT_CHANGED'):
        assert_zero_sqlite_writes(db_path, pre_counts)


def test_sample_delete_manifest_scrubs_cjk_adjacent_lan_ip_and_feishu_id():
    entries = [
        {
            'legacy_id': 'capability_memories:999',
            'source_table': 'capability_memories',
            'legacy_status': 'active',
            'legacy_category': 'architecture',
            'problem': '入口192.168.1.135:2223绑定机器人cli_a92c3ff4a9391bd2说明',
            'solution': '用户ou_abc1234567890def访问192.168.1.3接口',
            'raw_preview': '入口192.168.1.135/app',
            'gate_layer': 'L0',
            'reason_code': 'secret_or_host_specific_config',
            'detail': '检测到内网地址192.168.1.135',
        }
    ]
    sample = sample_delete_manifest(entries, sample_size=50, seed=20261114)
    blob = json.dumps(sample, ensure_ascii=False)
    assert '192.168.1.135' not in blob
    assert '192.168.1.3' not in blob
    assert 'cli_a92c3ff4a9391bd2' not in blob
    assert 'ou_abc1234567890def' not in blob
    assert '<lan-host>' in blob


def test_sample_delete_manifest_scrubs_enterprise_urls_accounts_ids_and_truncated_ips(monkeypatch):
    import ast
    import evolvmem.experience_v2.clean.sampler as sampler_mod

    monkeypatch.delenv('USER', raising=False)
    monkeypatch.setenv('USERNAME', 'WinUser99')
    entries = [
        {
            'legacy_id': 'capability_memories:1001',
            'source_table': 'capability_memories',
            'legacy_status': 'active',
            'legacy_category': 'architecture',
            'problem': '仓库 https://github.com/1942293420/Evolvmem_MCP 与 device_id:"win-01c2ee8f-41d1-479c-ba46-97c08c527bca"',
            'solution': '世晟登录页已填入账号 SHENGWEI，短信发送至尾号8792的手机，联系 13975085505，服务 https://omp.xlwms.com/wms/stock/sku',
            'raw_preview': '入口192. 地址127.… 127.0.0.1:19387 10.12.34.56 172.18.0.5 tenant https://acnv6w0sy3br.feishu.cn/docx/1',
            'gate_layer': 'L0',
            'reason_code': 'secret_or_host_specific_config',
            'detail': 'WinUser99 running PostgreSQL 10.2.0 on internal',
        }
    ]
    sample = sample_delete_manifest(entries, sample_size=50, seed=20261114)
    blob = json.dumps(sample, ensure_ascii=False)
    for leaked in (
        '1942293420',
        'win-01c2ee8f-41d1-479c-ba46-97c08c527bca',
        'SHENGWEI',
        '8792',
        '13975085505',
        'omp.xlwms.com',
        'acnv6w0sy3br',
        '127.0.0.1',
        '10.12.34.56',
        '172.18.0.5',
        '192.',
        '127.',
        'WinUser99',
    ):
        assert leaked not in blob, f'leaked: {leaked} in {blob}'
    assert '10.2.0' in blob
    assert '<device-id>' in blob
    assert 'github.com/<owner>/Evolvmem_MCP' in blob

    tree = ast.parse(Path(sampler_mod.__file__).read_text(encoding='utf-8'))
    imports = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert 'sqlite3' not in imports

def test_scrub_pilot_text_redacts_feishu_oc_and_short_win_device_ids():
    raw = (
        '群 oc_5756d9312c50d21eef44cdfd62f39c25 执行器 win-d3f1f26a '
        'package executor-enrollment-win-145a6243.zip on win-x64'
    )
    out = scrub_pilot_text(raw)
    assert 'oc_5756d9312c50d21eef44cdfd62f39c25' not in out
    assert 'win-d3f1f26a' not in out
    assert 'win-145a6243' not in out
    assert '[REDACTED_FEISHU_ID]' in out
    assert '<device-id>' in out
    assert 'win-x64' in out

def test_scrub_pilot_text_cjk_adjacent_truncated_and_preserves_safe_tokens():
    raw = (
        'device_id为win-01c2ee8f-41d1-479c-ba46-97c08c527bca，执行器win-d3f1f26a已离线；'
        '截断 win-01c2ee8… 与 win-01c2ee… 在 DESKTOP-OTTJ1G2、desktop-ottj1g2-p0-01、laptop-6gvmjvjo-01；'
        '访问omp.xlwms.com失败，飞书 cli_a92c3ff4… cli_aa3228aaf… 短链 https://xp… 与 http://1…；'
        '保留标识 on_transferable on_control cli_version win-x64。'
    )
    out = scrub_pilot_text(raw)
    for leaked in (
        'win-01c2ee8f-41d1-479c-ba46-97c08c527bca',
        'win-d3f1f26a',
        'win-01c2ee8…',
        'win-01c2ee…',
        'DESKTOP-OTTJ1G2',
        'desktop-ottj1g2-p0-01',
        'laptop-6gvmjvjo-01',
        'omp.xlwms.com',
        'cli_a92c3ff4…',
        'cli_aa3228aaf…',
        'https://xp…',
        'http://1…',
    ):
        assert leaked not in out, f'leaked: {leaked} in {out}'
    assert '<device-id>' in out
    assert '<host-id>' in out
    assert '<internal-host>' in out
    assert '<internal-url>' in out
    assert 'http://<lan-host>' in out
    assert '[REDACTED_FEISHU_ID]' in out
    for safe in ('on_transferable', 'on_control', 'cli_version', 'win-x64'):
        assert safe in out, f'safe token clobbered: {safe} in {out}'
