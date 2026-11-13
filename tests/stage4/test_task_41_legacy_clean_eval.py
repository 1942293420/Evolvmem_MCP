"""TDD tests for Stage 4 Task 4.1: read-only legacy evaluation (L0 -> L1 -> L2)."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sqlite3

import pytest

from evolvmem.experience_v2.clean.evaluator import evaluate_legacy_clean
from evolvmem.experience_v2.clean.legacy_reader import (
    LegacyExperienceRecord,
    open_sqlite_readonly,
    read_legacy_experience_candidates,
)
from evolvmem.experience_v2.writer import ExperienceWriter, Op, TicketIssuer


class _DetEmbedder:
    """Deterministic unit-normalized 768d embedder grouping near-identical phrases."""

    def encode_document(self, text: str) -> list[float]:
        v = [0.0] * 768
        t = text.lower()
        if 'tenant_id' in t or '租户' in t:
            v[0] = 1.0
        elif 'claim_id' in t or '索赔' in t:
            v[1] = 1.0
        elif 'pgvector' in t or 'hnsw' in t:
            v[2] = 1.0
        else:
            v[3 + (abs(hash(t)) % 200)] = 1.0
        return v


def _build_legacy_sqlite(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE context_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identity_key TEXT NOT NULL,
            content_type TEXT NOT NULL,
            project TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT 'project',
            status TEXT NOT NULL DEFAULT 'candidate',
            tier TEXT NOT NULL DEFAULT 'normal',
            tags TEXT NOT NULL DEFAULT '',
            importance REAL NOT NULL DEFAULT 5.0,
            confidence REAL NOT NULL DEFAULT 0.5,
            source_state TEXT NOT NULL DEFAULT 'none',
            source_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            access_count INTEGER NOT NULL DEFAULT 0,
            last_accessed TEXT,
            last_verified_at TEXT,
            expires_at TEXT,
            supersedes INTEGER,
            superseded_by INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            experience_payload TEXT NOT NULL DEFAULT '',
            personal_general INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE context_layers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            layer TEXT NOT NULL CHECK (layer IN ('l0', 'l1', 'l2')),
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            generator TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(item_id, layer)
        );
        CREATE TABLE capability_memories (
            memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            keywords TEXT NOT NULL DEFAULT '[]',
            source_project TEXT NOT NULL DEFAULT '',
            source_ref TEXT NOT NULL DEFAULT '',
            occurred_at TEXT,
            fingerprint TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE learning_memories (
            item_id INTEGER PRIMARY KEY,
            category TEXT NOT NULL,
            payload TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, raw TEXT);
        INSERT INTO sessions VALUES (1, 'MUST_NEVER_BE_READ_BY_STAGE4');
        """
    )

    ep_good = json.dumps({
        'problem': '索赔附件误走普通消息路径导致 404 或 403',
        'rationale': '索赔渠道需使用专用 claims 附件端点并省略 post_sale 标签',
        'steps': ['判断 channel 等于 claim 后调用专用 claims 附件下载接口'],
        'result': '下载返回 200',
        'transferable': True,
        'project': 'eva',
    }, ensure_ascii=False)
    ep_bad_l2 = json.dumps({
        'problem': 'pgvector HNSW 索引参数调整建议检查列表长短与环境差异',
        'rationale': '若建索引前未调高 maintenance_work_mem 可能导致构图退化',
        'steps': ['建表前根据数据规模确认 HNSW 参数'],
        'result': '查询延时稳定',
        'transferable': True,
        'project': 'evolvmem',
    }, ensure_ascii=False)
    ep_superseded = json.dumps({
        'problem': '旧版脚本重试超时过短',
        'rationale': '已废弃旧方案',
        'transferable': True,
    }, ensure_ascii=False)
    ep_nontransfer = json.dumps({
        'problem': '某特定工单人工跳过校验',
        'rationale': '仅单次应急不可复用',
        'transferable': False,
    }, ensure_ascii=False)

    # context_items rows: 1=active good, 2=candidate low-value(L2 reject), 3=superseded, 4=nontransfer, 5=static fact for learning_memories, 6=exp matching learning_memories 1
    conn.execute(
        "INSERT INTO context_items(id, identity_key, content_type, project, status, created_at, updated_at, experience_payload) VALUES (1, 'k1', 'experience', 'eva', 'active', '2026-10-01', '2026-10-05', ?)",
        (ep_good,),
    )
    conn.execute(
        "INSERT INTO context_items(id, identity_key, content_type, project, status, created_at, updated_at, experience_payload) VALUES (2, 'k2', 'experience', 'evolvmem', 'candidate', '2026-10-01', '2026-10-06', ?)",
        (ep_bad_l2,),
    )
    conn.execute(
        "INSERT INTO context_items(id, identity_key, content_type, project, status, created_at, updated_at, experience_payload) VALUES (3, 'k3', 'experience', 'eva', 'superseded', '2026-10-01', '2026-10-04', ?)",
        (ep_superseded,),
    )
    conn.execute(
        "INSERT INTO context_items(id, identity_key, content_type, project, status, created_at, updated_at, experience_payload) VALUES (4, 'k4', 'experience', 'eva', 'candidate', '2026-10-01', '2026-10-04', ?)",
        (ep_nontransfer,),
    )
    conn.execute(
        "INSERT INTO context_items(id, identity_key, content_type, project, status, created_at, updated_at, experience_payload) VALUES (5, 'k5', 'fact', 'demo', 'candidate', '2026-10-01', '2026-10-04', '')",
    )
    conn.execute(
        "INSERT INTO context_layers(item_id, layer, content, content_hash, generator, created_at, updated_at) VALUES (5, 'l0', 'demo stack uses react', 'h1', 'g', '2026-10-01', '2026-10-04'), (5, 'l1', 'demo project uses react and tailwind', 'h2', 'g', '2026-10-01', '2026-10-04')",
    )
    conn.execute("INSERT INTO learning_memories(item_id, category, payload) VALUES (5, 'reference', '{\"rationale\":\"static fact\"}')")
    conn.execute("INSERT INTO learning_memories(item_id, category, payload) VALUES (1, 'experience', '{\"rationale\":\"linked exp\"}')")

    # capability_memories rows: dup of manual note, conv snapshot dup + latest, host/secret ops, pending inactive
    conn.execute(
        "INSERT INTO capability_memories(memory_id, category, title, content, source_project, source_ref, fingerprint, status, created_at, updated_at) VALUES (10, 'backend', '计费接口必须注入 tenant_id 请求头', '调用内部计费门户前必须设置 tenant_id 标头否则返回 400。', 'billing', 'conversation:aaa#1', 'fp1', 'active', '2026-10-01', '2026-10-01')",
    )
    conn.execute(
        "INSERT INTO capability_memories(memory_id, category, title, content, source_project, source_ref, fingerprint, status, created_at, updated_at) VALUES (11, 'architecture', 'SSH 隔离接入边界（部分实施）', '限制用户仅访问 192.168.1.135:2223 /workspace/app 目录', 'ops', 'conversation:bbb#1', 'fp2', 'active', '2026-10-01', '2026-10-01')",
    )
    conn.execute(
        "INSERT INTO capability_memories(memory_id, category, title, content, source_project, source_ref, fingerprint, status, created_at, updated_at) VALUES (12, 'backend', '索赔附件下载需区分 claim 渠道（初版）', '发现索赔接口不同于普通下载，请用 claim_id 参数请求', 'eva', 'conversation:ccc#1', 'fp3', 'active', '2026-10-01', '2026-10-01')",
    )
    conn.execute(
        "INSERT INTO capability_memories(memory_id, category, title, content, source_project, source_ref, fingerprint, status, created_at, updated_at) VALUES (13, 'backend', '待定记录', '暂时挂起还未验证的想法和记录，需要等待后续处理流程完成。', 'eva', 'conversation:ddd#1', 'fp4', 'pending', '2026-10-01', '2026-10-01')",
    )
    conn.commit()
    conn.close()


def test_open_sqlite_readonly_rejects_writes_and_avoids_history_tables(tmp_path: Path):
    db_path = tmp_path / 'legacy.db'
    _build_legacy_sqlite(db_path)

    ro = open_sqlite_readonly(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM context_items WHERE id = 1")
    finally:
        ro.close()

    rows = read_legacy_experience_candidates(db_path)
    assert len(rows) == 10  # 4 context_items(exp) + 2 learning_memories + 4 capability_memories
    assert all(isinstance(r, LegacyExperienceRecord) for r in rows)

    import evolvmem.experience_v2.clean.legacy_reader as lr_mod
    src = Path(lr_mod.__file__).read_text(encoding='utf-8')
    tree = ast.parse(src)
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    sql_blob = '\n'.join(strings).lower()
    for forbidden in ('from sessions', 'from messages', 'from observations', 'history_archive'):
        assert forbidden not in sql_blob


def test_evaluate_legacy_clean_l0_l1_l2_and_zero_mutations(pg_s4_migrated, tmp_path: Path):
    conn, _schema, _dsn = pg_s4_migrated
    db_path = tmp_path / 'legacy.db'
    _build_legacy_sqlite(db_path)

    # Seed 1 active manual note matching capability_memories:10 ('tenant_id')
    writer = ExperienceWriter(conn, b's4-t41-secret')
    issuer = TicketIssuer(b's4-t41-secret')
    tkt = issuer.issue(proposal_uid='p-manual-1', decision_id='d-manual-1', lane='user_fast')
    emb = _DetEmbedder()
    writer.commit(
        tkt,
        Op(
            kind='INSERT',
            payload={
                'owner_id': 'owner-41',
                'origin': 'manual',
                'category': '业务经验',
                'problem': '计费请求缺失 tenant_id 导致失败',
                'solution': '调用前注入 tenant_id 请求头',
                'reusable_experience': '内部计费接口必传 tenant_id',
                'embedding': emb.encode_document('tenant_id'),
            },
        ),
    )

    notes_before = conn.execute('SELECT COUNT(*) FROM experience_notes').fetchone()[0]
    props_before = conn.execute('SELECT COUNT(*) FROM experience_proposals').fetchone()[0]

    def flash(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        user_msg = messages[-1]['content']
        if '索赔' in user_msg:
            return {'text': json.dumps({'consistent': False, 'confidence': 0.92, 'detail': 'wrong guess'}), 'input_tokens': 40, 'output_tokens': 20}
        if 'pgvector' in user_msg:
            return {'text': json.dumps({'consistent': True, 'confidence': 0.95, 'detail': 'trivial/known'}), 'input_tokens': 40, 'output_tokens': 20}
        return {'text': '索赔渠道走专用 claims 接口且不加 post_sale 标签；pgvector HNSW 参数按规模配置。', 'input_tokens': 50, 'output_tokens': 30}

    def strong(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        user_msg = messages[-1]['content']
        if '索赔' in user_msg:
            return {'text': json.dumps({'consistent': False, 'confidence': 0.94, 'detail': 'non-trivial'}), 'input_tokens': 50, 'output_tokens': 20}
        return {'text': json.dumps({'consistent': True, 'confidence': 0.96, 'detail': 'obvious'}), 'input_tokens': 50, 'output_tokens': 20}

    res = evaluate_legacy_clean(
        sqlite_path=db_path,
        conn=conn,
        owner_id='owner-41',
        flash_model='deepseek-v4-flash',
        strong_model='deepseek-chat',
        flash_transport=flash,
        strong_transport=strong,
        embedder=emb,
        extra_scrub_terms=('owner-41',),
    )

    keep = {r['legacy_id']: r for r in res['keep_manifest']}
    delete = {r['legacy_id']: r for r in res['delete_manifest']}
    assert len(keep) + len(delete) == 10
    assert 'context_items:1' in keep
    assert 'learning_memories:1' in keep  # FK child inherits parent experience verdict
    assert delete['context_items:2']['gate_layer'] == 'L2'
    assert delete['context_items:3']['reason_code'] == 'legacy_inactive_status'
    assert delete['context_items:4']['reason_code'] == 'legacy_non_transferable'
    assert delete['learning_memories:5']['reason_code'] == 'legacy_static_fact_not_experience'
    assert delete['capability_memories:10']['reason_code'] == 'duplicate_of_active_manual_note'
    assert delete['capability_memories:11']['reason_code'] == 'secret_or_host_specific_config'
    assert delete['capability_memories:12']['gate_layer'] == 'L1'
    assert delete['capability_memories:13']['reason_code'] == 'legacy_inactive_status'
    assert res['live_tables_mutated'] is False
    assert conn.execute('SELECT COUNT(*) FROM experience_notes').fetchone()[0] == notes_before
    assert conn.execute('SELECT COUNT(*) FROM experience_proposals').fetchone()[0] == props_before
    assert res['llm_usage']['calls'] > 0
