"""Stage 0 Task 0.6 TDD tests: legacy_web_enabled=false blocks forget, consolidate, archive purge, and summary retention."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from evolvmem import hooks
from evolvmem.config import Config
from evolvmem.consolidator import Consolidator
from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextMode,
    ContextScope,
    ContextServiceError,
    ContextStatus,
)
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.forgetting import ForgettingEngine
from evolvmem.mcp_server import MemoryMCPServer
from evolvmem.memory_store import MemoryStore
from evolvmem.project_rollup import ProjectRollupGenerator
from evolvmem.project_store import ProjectStore
from evolvmem.session_archive import SessionArchiver
from evolvmem.summary_retention import SummaryRetention
from evolvmem.vector_index import VectorIndex


class _FakeEmbeddingEngine:
    is_loaded = True

    def __init__(self, dim: int = 8) -> None:
        self.dim = dim

    def encode_document(self, text: str) -> list[float]:
        seed = int.from_bytes(hashlib.md5(text.encode("utf-8")).digest()[:4], "big")
        rng = np.random.RandomState(seed)
        vec = rng.randn(self.dim).astype(np.float32)
        return (vec / np.linalg.norm(vec)).tolist()


def _write_disabled_policy(tmp_path: Path) -> Path:
    policy_path = tmp_path / "product-policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "console_enabled": True,
                "legacy_web_enabled": False,
                "ai_processing_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    return policy_path


@pytest.fixture
def disabled_legacy_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    policy_path = _write_disabled_policy(tmp_path)
    monkeypatch.setenv("EVOLVMEM_PRODUCT_POLICY_FILE", str(policy_path))
    return policy_path


def test_hooks_forgetting_consolidation_and_sweep_never_run_when_legacy_web_disabled(
    tmp_path: Path, disabled_legacy_policy: Path
) -> None:
    cfg = Config(
        data_dir=tmp_path / "hook_data",
        apply_environment=False,
        forget_days_threshold=0,
        forget_access_count_threshold=5,
        forget_rate_limit_days=0,
    )
    store = MemoryStore(cfg)
    store.initialize()
    try:
        m1 = store.add(key="project:demo:fact:item", value="旧库记忆条目A", importance=3.0)
        m2 = store.add(key="demo:fact:item", value="旧库记忆条目A", importance=8.0)

        spy_service = SimpleNamespace(
            sweep_archives=lambda **_kw: (_ for _ in ()).throw(AssertionError("sweep_archives must not be called"))
        )
        hooks._maybe_run_forgetting(cfg, store)
        hooks._maybe_run_consolidation(cfg, store)
        hooks._maybe_sweep_archives(spy_service, ContextMode.PRIMARY)

        assert store.get_by_id(m1)["status"] == "active"
        assert store.get_by_id(m2)["status"] == "active"
        assert not (cfg.data_dir / ".last_forget").exists()
        assert not (cfg.data_dir / ".last_consolidate").exists()
    finally:
        store.close()


def test_forgetting_engine_run_and_direct_archive_blocked_when_legacy_web_disabled(
    tmp_path: Path, disabled_legacy_policy: Path
) -> None:
    cfg = Config(
        data_dir=tmp_path / "forget_data",
        apply_environment=False,
        forget_days_threshold=0,
        forget_access_count_threshold=5,
        forget_rate_limit_days=0,
    )
    store = MemoryStore(cfg)
    store.initialize()
    try:
        expired_id = store.add(
            key="project:demo:fact:expired",
            value="已到期的旧库记忆",
            expires_at="2020-01-01T00:00:00+00:00",
        )
        stale_id = store.add(
            key="project:demo:fact:stale",
            value="长期未访问的旧库记忆",
        )

        engine = ForgettingEngine(cfg, store)
        assert engine.run() == 0
        engine.archive(stale_id)

        assert store.get_by_id(expired_id)["status"] == "active"
        assert store.get_by_id(stale_id)["status"] == "active"
    finally:
        store.close()


def test_consolidator_and_mcp_tool_block_merge_when_legacy_web_disabled(
    tmp_path: Path, disabled_legacy_policy: Path
) -> None:
    cfg = Config(data_dir=tmp_path / "consolidate_data", apply_environment=False)
    store = MemoryStore(cfg)
    store.initialize()
    vidx = VectorIndex(cfg)
    vidx.initialize(dim=8)
    engine = _FakeEmbeddingEngine(dim=8)
    try:
        low_id = store.add(key="project:demo:db:choice", value="完全一致的旧库记忆内容", importance=3.0)
        high_id = store.add(key="demo:db:choice", value="完全一致的旧库记忆内容", importance=9.0)
        for mid in (low_id, high_id):
            vidx.add(mid, np.array(engine.encode_document("完全一致的旧库记忆内容"), dtype=np.float32))

        consolidator = Consolidator(cfg, store, vidx, engine)
        direct_res = consolidator.consolidate(dry_run=False)
        assert direct_res["merged"] == 0
        assert direct_res.get("disabled") == "legacy_web_disabled"
        assert store.get_by_id(low_id)["status"] == "active"
        assert store.get_by_id(high_id)["status"] == "active"

        fake_server = SimpleNamespace(
            engine=engine,
            consolidator=consolidator,
            _write_gate_error=lambda: None,
        )
        mcp_res = MemoryMCPServer._memory_consolidate(fake_server, {"dry_run": False})
        assert mcp_res["merged"] == 0
        assert mcp_res.get("disabled") == "legacy_web_disabled"
        assert store.get_by_id(low_id)["status"] == "active"
        assert store.get_by_id(high_id)["status"] == "active"
    finally:
        vidx.close()
        store.close()


def test_context_sweep_and_archive_project_blocked_across_archiver_service_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When legacy_web_enabled=false, neither context_sweep nor context_archive_project may delete archives."""
    cfg = Config(data_dir=tmp_path / "archive_guard_data", apply_environment=False)
    with MemoryStore(cfg):
        pass
    cstore = ContextStore(cfg)
    cstore.initialize()
    service = ContextService(cfg, store=cstore)
    service.initialize(mode=ContextMode.SHADOW, adapter="codex")
    try:
        ps = ProjectStore(cstore._connection(), cstore._require_transaction, generic_names=())
        with cstore.transaction():
            ps.register_project("demo")

        archiver = SessionArchiver(cfg, cstore)
        old_t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        rec1 = archiver.archive_session("demo", "codex", "sess-expired-1", "历史原始对话片段一", now=old_t0)
        rec2 = archiver.archive_session("demo", "codex", "sess-expired-2", "历史原始对话片段二", now=old_t0)
        assert rec1 is not None and rec2 is not None
        p1 = cfg.data_dir / rec1.payload_path
        p2 = cfg.data_dir / rec2.payload_path
        assert p1.exists() and p2.exists()

        # Activate production-equivalent policy (legacy_web_enabled=false)
        policy_path = _write_disabled_policy(tmp_path)
        monkeypatch.setenv("EVOLVMEM_PRODUCT_POLICY_FILE", str(policy_path))

        # 1. Direct SessionArchiver calls blocked
        with pytest.raises(ContextServiceError) as exc_sweep:
            archiver.sweep_expired()
        assert exc_sweep.value.code == "context_not_enabled"

        with pytest.raises(ContextServiceError) as exc_purge:
            archiver.purge_project("demo")
        assert exc_purge.value.code == "context_not_enabled"

        # 2. ContextService calls blocked
        with pytest.raises(ContextServiceError) as exc_svc_sweep:
            service.sweep_archives()
        assert exc_svc_sweep.value.code == "context_not_enabled"

        with pytest.raises(ContextServiceError) as exc_svc_arch:
            service.archive_project("demo")
        assert exc_svc_arch.value.code == "context_not_enabled"

        # 3. MCP tool handlers return structured context_not_enabled error
        fake_mcp = SimpleNamespace(
            context_service=service,
            _context_gate_error=lambda: None,
            _context_error=MemoryMCPServer._context_error,
            _purge_response=MemoryMCPServer._purge_response,
        )
        mcp_sweep = MemoryMCPServer._context_sweep(fake_mcp, {})
        assert mcp_sweep["error"] == "context_not_enabled"

        mcp_arch = MemoryMCPServer._context_archive_project(fake_mcp, {"project": "demo"})
        assert mcp_arch["error"] == "context_not_enabled"

        # Verify zero physical file deletion and DB state remains 'available'
        assert p1.exists() and p2.exists()
        states = {
            int(r["id"]): r["state"]
            for r in cstore._connection().execute("SELECT id, state FROM session_archives").fetchall()
        }
        assert states == {rec1.id: "available", rec2.id: "available"}
    finally:
        service.close()
        cstore.close()


def test_summary_retention_sweep_blocked_when_legacy_web_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covered session summaries beyond keep count must stay active when legacy_web_enabled=false."""
    cfg = Config(
        data_dir=tmp_path / "retention_guard_data",
        apply_environment=False,
        context_session_summary_keep=1,
    )
    with MemoryStore(cfg):
        pass
    cstore = ContextStore(cfg)
    cstore.initialize()
    service = ContextService(cfg, store=cstore)
    service.initialize(mode=ContextMode.SHADOW, adapter="codex")
    try:
        ps = ProjectStore(cstore._connection(), cstore._require_transaction, generic_names=())
        with cstore.transaction():
            ps.register_project("demo")

        s_ids = []
        for idx in range(3):
            item = cstore.create_item(
                ContextItemDraft(
                    identity_key=f"project:demo:progress:log:s{idx}",
                    content_type=ContextContentType.SESSION_SUMMARY,
                    layers=ContextLayers(
                        l0=f"会话摘要 {idx}",
                        l1=f"细节 {idx}",
                        l2=f"完整正文 {idx}",
                        generator="test-suite",
                    ),
                    project="demo",
                    scope=ContextScope.PROJECT,
                    status=ContextStatus.ACTIVE,
                )
            )
            s_ids.append(item.id)

        rollup_resp = json.dumps({"l0": "汇总要点", "l1": "进展详情", "l2": "完整闭包"}, ensure_ascii=False)
        rollup_report = ProjectRollupGenerator(cfg, cstore, llm=lambda _p: rollup_resp).rollup_project("demo")
        assert rollup_report.status == "ready"

        # Activate production-equivalent policy (legacy_web_enabled=false)
        policy_path = _write_disabled_policy(tmp_path)
        monkeypatch.setenv("EVOLVMEM_PRODUCT_POLICY_FILE", str(policy_path))

        ret_report = SummaryRetention(cfg, cstore).sweep("2026-11-12 12:00:00")
        assert ret_report.archived_ids == ()
        service.sweep_archives(preserve_originals=True)

        statuses = {
            int(r["id"]): r["status"]
            for r in cstore._connection()
            .execute("SELECT id, status FROM context_items WHERE id IN (?, ?, ?)", tuple(s_ids))
            .fetchall()
        }
        assert statuses == {sid: "active" for sid in s_ids}
    finally:
        service.close()
        cstore.close()
