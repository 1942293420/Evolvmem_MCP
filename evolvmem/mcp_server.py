#!/usr/bin/env python3
"""EvolvMem memory plugin — stdio MCP Server.

Tools (legacy, always registered):
  memory_search   — FTS5/trigram + HNSW hybrid search
  memory_status   — statistics
  memory_add      — manually add a memory
  memory_replace  — replace a memory (mark old as superseded)
  memory_remove   — soft-delete a memory
  memory_consolidate — find/merge near-duplicate memories

Context tools (Codex/Kimi shadow/primary with a ready ContextService):
  context_session_start — bounded rendered L1 history block
  context_search        — thresholded Core retrieval, L0 metadata only
  context_read          — exact-ID L1/L2 read
  context_status        — content-free diagnostic snapshot
  context_confirm       — promote one candidate to active (review path)
  context_record_outcome — record success/failure/confirmed/contradicted evidence
  context_archive_project — immediately purge one project's session archives
  context_sweep         — TTL purge sweep over expired session archives

Continuity tools (Codex/Kimi compat/shadow/primary; call-time readiness):
  continuity_begin      — idempotent declare→register→bind→workstream entry
  continuity_find       — cross-project discovery by name/alias/task keyword
  continuity_resume     — exact focus-pointer resume, bounded checkpoint
  continuity_checkpoint — action whitelist + revision CAS mutation
  continuity_list       — unfinished workstream L0 summaries

Project-board tools (same adapters/modes; optional private configuration):
  project_board_sync    — manually deliver committed checkpoint progress
  project_board_status  — read local delivery state without network activity

The exposed tool set comes from one registry (evolvmem.mcp_contract):
tools/list and tools/call share it, so a hidden tool cannot still be
called. MCP dictionaries are parsed only at this boundary; the Context
side speaks the typed ContextService API.
"""

import json
import math
import select
import sys
import os
import threading
import time
import traceback
from evolvmem.config import Config
from evolvmem.context_lifecycle import ContextLifecycleError
from evolvmem.context_models import (
    ContextContentType,
    ContextLayer,
    ContextMode,
    ContextReadRequest,
    ContextSearchRequest,
    ContextServiceError,
    ContextSessionStartRequest,
    ContextValidationError,
)
from evolvmem.context_service import ContextService
from evolvmem.continuity_models import (
    ContinuityAction,
    ContinuityBeginRequest,
    ContinuityCheckpointRequest,
    ContinuityError,
    ContinuityFindRequest,
    ContinuityResumeRequest,
    ContinuityValidationError,
)
from evolvmem.continuity_service import ContinuityService
from evolvmem.project_board_sync import ProjectBoardSync
from evolvmem.cutover_checks import compare_shadow
from evolvmem.legacy_models import (
    LegacyAddRequest,
    LegacyRemoveRequest,
    LegacyReplaceRequest,
)
from evolvmem.mcp_contract import (
    CONTEXT_CORE_ADAPTERS,
    initialization_instructions,
    tool_specs,
)
from evolvmem.vector_index import VectorIndex
from evolvmem.embedding import EmbeddingEngine
from evolvmem.retriever import Retriever
from evolvmem.conflict_detector import ConflictDetector
from evolvmem.forgetting import ForgettingEngine
from evolvmem.consolidator import Consolidator
from evolvmem.semantic_merge import find_semantic_match
from evolvmem.workspace_identity import WorkspaceIdentityProvider


# 低信息过渡语：自动摘要里常见的"零价值"句式（命中即拒收）
_LOW_INFO_PATTERNS = (
    "等待用户", "会话继续", "等待下一步", "等待用户确认",
    "等待用户后续", "no action required",
)

# 分发层按次过写门禁的旧写工具；memory_consolidate 的 dry_run 是只读分支，
# 其门禁留在处理器内部（仅 dry_run=False 拦截）
_DISPATCH_WRITE_TOOLS = frozenset({
    "memory_add", "memory_replace", "memory_remove", "experience_record",
})

# context 协议错误的稳定文案：不含 traceback、正文或路径
_CONTEXT_ERROR_MESSAGES = {
    "invalid_arguments": "invalid arguments for the context tool",
    "not_found": "no context item with the exact given id",
    "not_readable": "the exact context item is not readable",
    "expired": "the exact context item has expired",
    "invalid_layer": "the requested layer is unavailable for the exact id",
    "item_not_found": "no context item with the exact given id",
    "invalid_item_state": "the context item state does not allow this operation",
    # invalid_source 服务两个域：context_record_outcome 的 outcome source
    # 与 continuity_checkpoint 的 source_context_ids；文案保持域中立
    "invalid_source": "the source reference is invalid for this operation",
    "sensitive_note": "the note was rejected by the sensitive-content policy",
    "identity_conflict": "an active item already owns this identity",
    "context_not_enabled": "context reads are disabled in the current mode",
    "degraded_legacy": "context primary mode is degraded; serving is fail-closed",
    "not_initialized": "context service is not initialized yet",
    "invalid_mode": "context mode is invalid; context features fail closed",
    "invalid_config": "context configuration is invalid; context features fail closed",
    "context_unavailable": "context tool failed; continue without memory",
    # continuity 续接域的稳定文案：同样无正文、无路径、无 traceback
    "revision_conflict": "the workstream or focus revision does not match; resume again before writing",
    "invalid_transition": "the action is not allowed from the current workstream status",
    "invalid_action": "the checkpoint action is unknown or misses a required CAS token",
    "workspace_key_missing": "the workspace identity is unavailable",
    "workstream_not_found": "no workstream with the exact given id in this project/workspace",
    "focus_conflict": "the focus pointer revision does not match; resume again before writing",
    "invalid_parent": "the parent workstream is invalid (missing, terminal, cross-project, or cyclic)",
    "content_rejected": "the checkpoint content was rejected by the content policy",
    "project_unresolved": "no active project binding resolves for this workspace and hint",
    "invalid_project_name": "the project name is missing, too long, or path-shaped; declare an explicit project name",
    "alias_conflict": "the alias already belongs to a different project",
    "project_archived": "the project is archived; reactivate it explicitly before beginning work",
    "project_not_registered": "no registered project, alias, or unfinished task matches the query",
    "no_open_workstream": "the project is registered but has no unfinished workstream",
    "continuity_not_ready": "continuity storage or workspace identity is not ready",
    "dangling_focus": "the focus pointer targets a missing or terminal workstream",
    "ambiguous": "multiple unfinished workstreams; pick one before resuming",
    "no_continuation": "no unfinished workstream for this project/workspace",
    "needs_focus_confirmation": "one unfinished workstream candidate needs focus confirmation",
}


def _is_low_info(value: str) -> bool:
    # 整句匹配语义：strip 后以模式开头才算低信息（句中出现不误伤）；casefold 兼容大小写变体
    v = value.strip().casefold()
    return any(v.startswith(p.casefold()) for p in _LOW_INFO_PATTERNS)


class MemoryMCPServer:
    """stdio MCP Server — JSON-RPC protocol."""

    # 握手等待轻量 Context 健康评估的上限；绝不等待 embedding 模型加载
    _HEALTH_WAIT_TIMEOUT_S = 5

    def __init__(self, config: Config | None = None, context_service=None):
        self.config = config if config is not None else Config.from_file()
        self.adapter = self.config.adapter or "mcp"
        try:
            self.context_mode: ContextMode | None = ContextMode(
                self.config.context_mode
            )
        except ValueError:
            # 未知 mode：Context 功能 fail-closed（只留 context_status 诊断）
            self.context_mode = None
        self.vidx = VectorIndex(self.config)
        self.engine = EmbeddingEngine(self.config)
        # 所有 legacy 读写都经 ContextService 兼容门面（access 计数也不例外）；
        # 本模块不再持有裸 MemoryStore
        self.context_service = context_service
        # 续接服务与 workspace identity 惰性构建（首次 continuity_* 调用时）；
        # 借用 context_service 的 store，与其同生命周期（shutdown 统一关闭）
        self._continuity_service = None
        self._workspace_identity_provider = None
        self._project_board_adapter = None
        self.retriever = None
        self.conflict_detector = None
        self.forgetting = None
        self.consolidator = None
        # 初始化门闩：run() 里由后台线程完成重初始化后置位，
        # 旧 tools/call 等待它；握手与 context_* 工具不等
        self._init_done = threading.Event()
        self._init_error: Exception | None = None
        # 后台 initialize() 完成轻量 Context 健康评估（服务初始化）后置位；
        # 握手最多有界等待它，绝不等待可选的 embedding 模型加载
        self._service_evaluated = threading.Event()

    def initialize(self):
        """Initialize all components."""
        if self.context_mode is None:
            # 非法 Context 配置：fail-closed。握手仍应答，注册表只剩
            # context_status 诊断入口，写被分发层拒绝。
            self._log("Invalid context_mode; context features fail closed")
            return
        try:
            service = self.context_service
            if service is None:
                # 共享同一引擎与 legacy 投影向量索引：合并判定与写后同步看到
                # 同一份内存态；服务生命周期由 shutdown() 统一关闭
                service = ContextService(
                    self.config, embedding_engine=self.engine
                )
                service._legacy_vector = self.vidx
                service.initialize(
                    mode=self.context_mode,
                    adapter=self.adapter,
                )
                self.context_service = service
            # 打开 context 向量缓存（mmap 恢复或空索引；不加载模型），让
            # primary 的按次健康复查反映真实不变量
            try:
                service.vector_index.initialize(dim=self.config.embedding_dim)
            except Exception:
                pass  # 打不开的索引由健康评估如实报告为不可用
            # legacy 投影 schema（memories 表）在任何 mode 下都必须存在；经服务
            # 自有的 legacy 后端幂等引导（唯一允许实例化 MemoryStore 的位置）。
            # 在健康评估置位前完成，握手看到的健康结论才是确定性的。
            service._legacy_backend()
        finally:
            self._service_evaluated.set()
        self.vidx.initialize(dim=self.config.embedding_dim)

        # Try loading the embedding model (FTS5 search works without it)
        try:
            self.engine.initialize()
        except Exception:
            # 宽捕获：模型加载的任何瞬时失败（缺文件/缺依赖/内存不足）都降级为
            # 仅 FTS 搜索，而不是让整个会话的 tools/call 被 _init_error 堵死
            self._log("Embedding engine unavailable; FTS-only mode")

        facade = service.legacy_facade()
        # Check USearch vs SQLite consistency (needs engine for rebuild)
        sqlite_count = len(facade.all_ids())
        if not self.vidx.check_consistency(sqlite_count):
            self._rebuild_under_lock()

        self.retriever = Retriever(self.config, facade, self.vidx, self.engine)
        self.conflict_detector = ConflictDetector(facade)
        self.forgetting = ForgettingEngine(self.config, facade)
        self.consolidator = Consolidator(
            self.config,
            facade,
            self.vidx,
            self.engine,
        )

    def shutdown(self):
        """Clean up resources."""
        # 等后台初始化结束，避免关闭与初始化并发操作同一资源
        self._init_done.wait()
        try:
            service = getattr(self, "context_service", None)
            if service:
                service.close()
        except Exception:
            pass
        try:
            if self.vidx:
                self.vidx.save()
                self.vidx.close()
        except Exception:
            pass
        try:
            if self.engine:
                self.engine.close()
        except Exception:
            pass

    # ---- tool handlers ----

    def handle_tool_call(self, tool_name: str, args: dict) -> dict:
        """Route tool calls."""
        handler = self._tool_handlers().get(tool_name)
        if handler is None:
            return {"error": f"Unknown tool: {tool_name}"}
        if tool_name in _DISPATCH_WRITE_TOOLS:
            gate_error = self._write_gate_error()
            if gate_error is not None:
                return {"error": gate_error}
        return handler(args)

    def _tool_handlers(self):
        return {
            "memory_search": self._memory_search,
            "memory_status": self._memory_status,
            "memory_add": self._memory_add,
            "memory_replace": self._memory_replace,
            "memory_remove": self._memory_remove,
            "memory_consolidate": self._memory_consolidate,
            "context_session_start": self._context_session_start,
            "context_search": self._context_search,
            "experience_recall": self._experience_recall,
            "experience_record": self._experience_record,
            "context_read": self._context_read,
            "context_status": self._context_status,
            "context_confirm": self._context_confirm,
            "context_record_outcome": self._context_record_outcome,
            "context_archive_project": self._context_archive_project,
            "context_sweep": self._context_sweep,
            "continuity_resume": self._continuity_resume,
            "continuity_checkpoint": self._continuity_checkpoint,
            "continuity_list": self._continuity_list,
            "continuity_begin": self._continuity_begin,
            "continuity_find": self._continuity_find,
            "project_board_sync": self._project_board_sync,
            "project_board_status": self._project_board_status,
        }

    def _memory_search(self, args: dict) -> dict:
        query = args.get("query", "")
        top_k = int(args.get("top_k", 10))
        if not query:
            return {"error": "query parameter cannot be empty"}
        status = self._live_status()
        if (
            status is not None
            and status.mode is ContextMode.PRIMARY
            and status.ready
            and status.adapter in CONTEXT_CORE_ADAPTERS
        ):
            return self._memory_search_primary(query, top_k)
        if self.retriever is None:
            # 非法配置/未初始化的 fail-closed 状态：只给诊断，不假装检索可用
            return {"error": "memory_unavailable: context configuration is "
                             "invalid or the service is not initialized"}
        results = self.retriever.search(query, top_k=top_k)
        if status is not None and status.mode is ContextMode.SHADOW:
            self._shadow_compare(query, results)
        return {
            "results": [
                {
                    "id": r["id"],
                    "key": r["key"],
                    "value": r["value"],
                    "status": r["status"],
                    "attribute": r["attribute"],
                    "tags": r["tags"],
                    "score": r.get("score"),
                    "match_type": r.get("match_type"),
                    "created_at": r["created_at"],
                }
                for r in results
            ],
            "count": len(results),
        }

    def _memory_search_primary(self, query: str, top_k: int) -> dict:
        """primary（codex/kimi）：Core 排序后把精确 ID 映射回投影行。

        保留旧 `id/key/value/...` 字段，context 字段只做可选增量；没有
        精确映射的 Core 邻居直接略过，绝不替换。
        """
        service = self.context_service
        clamped_top_k = min(max(int(top_k), 1), 20)
        try:
            results = service.search(
                ContextSearchRequest(
                    query=query, top_k=clamped_top_k, cross_project=True
                )
            )
        except ContextServiceError as exc:
            return self._context_error(exc.code)
        except Exception:
            return self._context_error("context_unavailable")
        rows = []
        for result in results:
            row = self._projection_row_for_context(service, result)
            if row is None:
                continue
            rows.append({
                "id": row["id"],
                "key": row["key"],
                "value": row["value"],
                "status": row["status"],
                "attribute": row["attribute"],
                "tags": row["tags"],
                "score": result.score,
                "match_type": "+".join(
                    match.value for match in result.match_types
                ) or None,
                "created_at": row["created_at"],
                "context_id": result.id,
                "available_layers": [
                    layer.value for layer in result.available_layers
                ],
            })
        return {"results": rows, "count": len(rows)}

    @staticmethod
    def _projection_row_for_context(service, result):
        """context→投影行的精确映射；无映射返回 None（绝不取近邻顶替）。"""
        for row in service.legacy_facade().get_by_key(result.identity_key):
            if (
                row.get("status") == "active"
                and service.store.resolve_legacy_mapping(row["id"]) == result.id
            ):
                return row
        return None

    def _shadow_compare(self, query: str, results: list) -> None:
        """shadow：legacy 结果原样返回后，旁路跑一次 Core 检索比较。

        只记录 overlap/计数/阈值排除/耗时等无正文指标；比较失败绝不
        影响 legacy 响应。直接调 ContextRetriever（它不做 access 计数），
        避免比较行为污染 Core 遥测。
        """
        if not results:
            return
        service = getattr(self, "context_service", None)
        if service is None:
            return
        try:
            store = service.store
            mapping = {}
            for row in results:
                context_id = store.resolve_legacy_mapping(row["id"])
                if context_id is not None:
                    mapping[row["id"]] = context_id
            started = time.monotonic()
            core_results = service.retriever.search(
                ContextSearchRequest(
                    query=query,
                    top_k=min(max(len(results), 1), 20),
                    cross_project=True,
                )
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            excluded = frozenset(
                mapping[row["id"]]
                for row in results
                if row["id"] in mapping
                and self._below_core_vector_threshold(row)
            )
            comparison = compare_shadow(
                [row["id"] for row in results],
                [result.id for result in core_results],
                mapping,
                expected_relevant=len(results),
                below_threshold_core_ids=excluded,
            )
            self._log(
                "shadow compare: "
                f"legacy={comparison.legacy_count} "
                f"core={comparison.core_count} "
                f"mapped={comparison.mapped_legacy_count} "
                f"unmapped={comparison.unmapped_legacy_count} "
                f"threshold_excluded={comparison.below_threshold_excluded} "
                f"top1_match={comparison.top1_match} "
                f"overlap_at_5={comparison.overlap_at_5:.2f} "
                f"duration_ms={duration_ms}"
            )
        except Exception:
            pass  # 旁路指标失败不影响已返回的 legacy 结果

    def _below_core_vector_threshold(self, row: dict) -> bool:
        """legacy 纯向量命中且相似度低于 Core 阈值 → 合理阈值排除。"""
        if row.get("match_type") != "vector":
            return False
        weight = self.config.vector_weight
        if not weight:
            return False
        # legacy 向量通道得分 = similarity * vector_weight，可无损还原
        similarity = (row.get("score") or 0.0) / weight
        return similarity < self.config.context_vector_min_similarity

    def _memory_status(self, args: dict) -> dict:
        status = self._live_status()
        if status is None:
            # 无可用服务（非法配置/未初始化）：只给安全诊断，不伪造计数
            return {
                "available": False,
                "reason": (
                    "invalid_mode" if self.context_mode is None
                    else "not_initialized"
                ),
                "embedding_loaded": self.engine.is_loaded,
                "diagnostics": list(
                    self.config.validate_runtime(require_model=True)
                )[:8],
            }
        facade = self.context_service.legacy_facade()
        return {
            "active_memories": facade.count_active(),
            "total_records": len(facade.all_ids()),
            "vector_count": self.vidx.count(),
            "embedding_loaded": self.engine.is_loaded,
            "embedding_dim": self.config.embedding_dim,
            "embedding_diagnostics": list(
                self.config.validate_runtime(require_model=True)
            )[:8],
            # 安全的可用性/dirty 诊断；绝不输出绝对数据目录
            "legacy_vector_dirty": status.legacy_vector_dirty,
            "context_mode": status.mode.value,
            "context_adapter": status.adapter,
            "context_ready": status.ready,
            "context_vector_dirty": status.context_vector_dirty,
        }

    def _memory_add(self, args: dict) -> dict:
        key = args.get("key", "")
        value = args.get("value", "")
        attribute = args.get("attribute", "fact")
        tags = args.get("tags", [])

        if not key or not value:
            return {"error": "key and value parameters cannot be empty"}

        if len(value) > self.config.value_max_chars:
            return {"error": f"value too long ({len(value)} > {self.config.value_max_chars} chars); "
                             "split or summarize before adding"}

        if len(value.strip()) < self.config.value_min_chars:
            return {"error": f"value too short ({len(value.strip())} < {self.config.value_min_chars} chars); "
                             "no information content"}
        if _is_low_info(value):
            return {"error": "value looks like a low-information placeholder "
                             "(transitional/chatter); not persisting"}

        importance = args.get("importance")
        if importance is not None:
            importance = float(importance)
            if math.isnan(importance):  # min(10.0, nan) 返回 10.0，置 None 走默认路径
                importance = None
        if importance is not None:
            importance = max(1.0, min(10.0, importance))
        tier = args.get("tier")
        if tier not in ("pinned", "normal", "reference"):
            tier = None
        expires_at = args.get("expires_at")

        facade = self.context_service.legacy_facade()
        # Conflict detection
        decision = self.conflict_detector.check(key, value)
        if decision.action == "skip":
            return {"status": "skipped", "reason": decision.reason}
        elif decision.action == "conflict":
            return {
                "status": "conflict",
                "reason": decision.reason,
                "existing_id": decision.existing_id,
            }
        if decision.action == "replace":
            # Conflict detector determined replace: use replace() to mark old as superseded
            old_id = decision.existing_id
            result = self.context_service.legacy_replace(
                LegacyReplaceRequest(
                    key=key,
                    new_value=value,
                    importance=importance,
                    tier=tier,
                    expires_at=expires_at,
                )
            )

            return self._mutation_response(
                {"status": "replaced", "new_id": result.legacy_id,
                 "old_id": old_id},
                result,
            )
        else:
            # decision.action == "add": 同 key 无冲突 → 再做跨 key 语义合并
            # （tier == "reference" 的新值同样不参与合并：永不 supersede 别人）
            if self.engine.is_loaded and tier != "reference":
                match = find_semantic_match(
                    facade, self.vidx, self.engine, value,
                    self.config.add_merge_threshold,
                    key=key, attribute=args.get("attribute", "fact"), tags=args.get("tags", ()))
                if match:
                    result = self.context_service.legacy_replace(
                        LegacyReplaceRequest(
                            key=match["key"],
                            new_value=value,
                            importance=importance,
                            tier=tier,
                            expires_at=expires_at,
                        )
                    )
                    return self._mutation_response(
                        {"status": "merged", "merged_into": match["id"],
                         "key": match["key"],
                         "similarity": match["similarity"],
                         "new_id": result.legacy_id},
                        result,
                    )
            # no existing key and no semantic match, insert directly
            result = self.context_service.legacy_add(
                LegacyAddRequest(
                    key=key,
                    value=value,
                    attribute=attribute,
                    tags=tuple(tags) if tags else (),
                    importance=importance if importance is not None else 5.0,
                    tier=tier if tier is not None else "normal",
                    expires_at=expires_at,
                )
            )

        return self._mutation_response({"status": "added", "id": result.legacy_id},
                                       result)

    def _memory_replace(self, args: dict) -> dict:
        key = args.get("key", "")
        new_value = args.get("value", "")

        if not key or not new_value:
            return {"error": "key and value parameters cannot be empty"}

        if len(new_value) > self.config.value_max_chars:
            return {"error": f"value too long ({len(new_value)} > {self.config.value_max_chars} chars); "
                             "split or summarize before adding"}

        if len(new_value.strip()) < self.config.value_min_chars:
            return {"error": f"value too short ({len(new_value.strip())} < {self.config.value_min_chars} chars); "
                             "no information content"}
        if _is_low_info(new_value):
            return {"error": "value looks like a low-information placeholder "
                             "(transitional/chatter); not persisting"}

        result = self.context_service.legacy_replace(
            LegacyReplaceRequest(key=key, new_value=new_value)
        )

        return self._mutation_response(
            {"status": "replaced", "new_id": result.legacy_id}, result
        )

    def _memory_remove(self, args: dict) -> dict:
        mem_id = int(args.get("id", 0))
        if not mem_id:
            return {"error": "id parameter cannot be empty"}
        result = self.context_service.legacy_remove(
            LegacyRemoveRequest(legacy_id=mem_id)
        )
        return self._mutation_response({"status": "deleted", "id": mem_id}, result)

    def _memory_consolidate(self, args: dict) -> dict:
        if not self.engine.is_loaded:
            return {"error": "embedding engine not loaded"}
        dry_run = bool(args.get("dry_run", True))
        threshold = args.get("threshold")
        if not dry_run:
            gate_error = self._write_gate_error()
            if gate_error is not None:
                return {"error": gate_error}
        result = self.consolidator.consolidate(
            dry_run=dry_run,
            threshold=float(threshold) if threshold is not None else None,
        )
        # 压缩输出，避免把整条 value 灌回上下文
        for p in result.get("pairs", []):
            for side in ("keep", "drop"):
                m = p[side]
                p[side] = {"id": m["id"], "key": m["key"],
                           "preview": m["value"][:80],
                           "importance": m["importance"]}
        return result

    # ---- context tool handlers (MCP dict ↔ typed API boundary) ----

    @staticmethod
    def _context_error(code: str) -> dict:
        """Stable, content-free protocol error: no traceback/content/path."""
        return {"error": code,
                "message": _CONTEXT_ERROR_MESSAGES.get(code, code)}

    def _context_gate_error(self) -> dict | None:
        """None when context reads may be served, else a stable error."""
        if self.context_mode is None:
            return self._context_error("invalid_config")
        status = self._live_status()
        if status is None:
            return self._context_error("not_initialized")
        if status.mode in (ContextMode.LEGACY, ContextMode.COMPAT):
            return self._context_error("context_not_enabled")
        if status.mode is ContextMode.PRIMARY and not status.ready:
            return self._context_error("degraded_legacy")
        return None

    def _context_session_start(self, args: dict) -> dict:
        try:
            request = ContextSessionStartRequest(
                project=args.get("project"),
                query=args.get("query"),
                max_chars=args.get("max_chars"),
                # 新可选入参：缺省/显式 null 归一为 ""（未提供），Task 9 接线消费
                workspace_path=args.get("workspace_path") or "",
            )
        except (ContextValidationError, TypeError):
            return self._context_error("invalid_arguments")
        gate_error = self._context_gate_error()
        if gate_error is not None:
            return gate_error
        try:
            result = self.context_service.session_start(request)
        except ContextServiceError as exc:
            return self._context_error(exc.code)
        except Exception:
            # Codex/Kimi fail-open：绝不回退注入所有旧 active memory
            return self._context_error("context_unavailable")
        payload = {
            "block": result.block,
            "selected_ids": list(result.selected_ids),
            "used_chars": result.used_chars,
            "excluded_counts": [
                {"reason": item.reason, "count": item.count}
                for item in result.excluded_counts
            ],
            # 续接路由信号（Task 9）：未触发续接分支时为 ""；结构体内绝无
            # L2 原文或绝对路径
            "continuation_code": result.continuation_code or "",
        }
        if result.continuation is not None:
            payload["continuation"] = result.continuation
        return payload

    def _context_search(self, args: dict) -> dict:
        try:
            content_types = tuple(
                ContextContentType(value)
                for value in (args.get("content_types") or ())
            )
            request = ContextSearchRequest(
                query=args.get("query"),
                project=args.get("project", ""),
                top_k=args.get("top_k", 10),
                content_types=content_types,
            )
        except (ContextValidationError, ValueError, TypeError):
            return self._context_error("invalid_arguments")
        gate_error = self._context_gate_error()
        if gate_error is not None:
            return gate_error
        try:
            results = self.context_service.search(request)
        except ContextServiceError as exc:
            return self._context_error(exc.code)
        except Exception:
            return self._context_error("context_unavailable")
        return {
            "results": [
                {
                    "id": r.id,
                    "identity_key": r.identity_key,
                    "l0": r.l0,
                    "content_type": r.content_type.value,
                    "scope": r.scope.value,
                    "project": r.project,
                    "status": r.status.value,
                    "tier": r.tier.value,
                    "confidence": r.confidence,
                    "importance": r.importance,
                    "score": r.score,
                    "match_types": [m.value for m in r.match_types],
                    "match_layers": [layer.value for layer in r.match_layers],
                    "available_layers": [
                        layer.value for layer in r.available_layers
                    ],
                }
                for r in results
            ],
            "count": len(results),
        }

    def _context_read(self, args: dict) -> dict:
        try:
            layer = ContextLayer(args.get("layer", "l1"))
            request = ContextReadRequest(id=args.get("id"), layer=layer)
        except (ContextValidationError, ValueError, TypeError):
            return self._context_error("invalid_arguments")
        gate_error = self._context_gate_error()
        if gate_error is not None:
            return gate_error
        try:
            result = self.context_service.read(request)
        except ContextServiceError as exc:
            return self._context_error(exc.code)
        except Exception:
            return self._context_error("context_unavailable")
        if result.error_code is not None:
            # 不存在/不可读/过期/层无效：稳定错误码，绝不回退相似项
            return self._context_error(result.error_code)
        return {
            "id": result.id,
            "layer": result.layer.value,
            "content": result.content,
        }

    def _context_status(self, args: dict) -> dict:
        status = self._live_status()
        if status is None:
            return {
                "mode": self.config.context_mode,
                "adapter": self.adapter,
                "ready": False,
                "reason_codes": [
                    "invalid_mode" if self.context_mode is None
                    else "not_initialized"
                ],
                "diagnostics": list(self.config.validate_runtime())[:8],
            }
        return {
            "mode": status.mode.value,
            "adapter": status.adapter,
            "ready": status.ready,
            "status_counts": dict(status.status_counts),
            "mapping_count": status.mapping_count,
            "projection_lag": status.projection_lag,
            "context_vector_ready": status.context_vector_ready,
            "context_vector_dirty": status.context_vector_dirty,
            "legacy_vector_ready": status.legacy_vector_ready,
            "legacy_vector_dirty": status.legacy_vector_dirty,
            "diagnostics": list(status.diagnostics),
            "reason_codes": list(status.reason_codes),
        }

    # ---- lifecycle/archive write tools (same gate, same stable errors) ----

    def _context_confirm(self, args: dict) -> dict:
        if not isinstance(args, dict) or set(args) - {"id"}:
            return self._context_error("invalid_arguments")
        item_id = args.get("id")
        if type(item_id) is not int or item_id <= 0:
            return self._context_error("invalid_arguments")
        gate_error = self._context_gate_error()
        if gate_error is not None:
            return gate_error
        try:
            report = self.context_service.confirm(item_id)
        except (ContextServiceError, ContextLifecycleError) as exc:
            return self._context_error(exc.code)
        except Exception:
            return self._context_error("context_unavailable")
        return {
            "id": report.item_id,
            "status": report.status.value,
            "confidence": report.confidence,
            "evidence_id": report.evidence_id,
        }

    def _experience_recall(self, args: dict) -> dict:
        if not isinstance(args, dict) or set(args) - {"project", "query", "constraints", "workstream_id"}:
            return self._context_error("invalid_arguments")
        error = self._context_gate_error()
        if error is not None:
            return error
        try:
            return self.context_service.experiences().recall(
                project=args.get("project", ""), query=args.get("query"),
                constraints=args.get("constraints"), workstream_id=args.get("workstream_id"))
        except (ValueError, TypeError, ContextValidationError):
            return self._context_error("invalid_arguments")
        except ContextServiceError as exc:
            return self._context_error(exc.code)

    def _experience_record(self, args: dict) -> dict:
        error = self._context_gate_error()
        if error is not None:
            return error
        try:
            if not isinstance(args, dict) or set(args) - {"case", "evidence"}:
                raise ValueError("invalid arguments")
            return self._after_experience_write(self.context_service.experiences().record(
                args.get("case"), evidence=args.get("evidence")))
        except (ValueError, TypeError, ContextValidationError):
            return self._context_error("invalid_arguments")
        except ContextServiceError as exc:
            return self._context_error(exc.code)

    def _after_experience_write(self, result: dict) -> dict:
        """Qualified new source sets may produce an unverified method.

        Existing generator coverage deduplicates the LLM work. Generation is
        best effort after the evidence commit; failure preserves saved cases.
        """
        core = self.context_service
        engine = core.embedding_engine
        if result.get('status') != 'active' or not getattr(engine, 'is_loaded', False):
            return result
        try:
            eligibility = core._context_lifecycle().evaluate_playbook_eligibility(
                embedding_engine=engine)
            if eligibility.clusters:
                from evolvmem.kimi_hooks import _load_llm_callable
                report = core.run_consolidation(llm=_load_llm_callable(log_errors=False))
                if report.playbook_created_ids:
                    result['candidate_method_ids'] = list(report.playbook_created_ids)
        except Exception:
            result['method_generation'] = 'deferred'
        return result

    def _context_record_outcome(self, args: dict) -> dict:
        if not isinstance(args, dict):
            return self._context_error("invalid_arguments")
        item_id = args.get("id")
        if type(item_id) is not int or item_id <= 0:
            return self._context_error("invalid_arguments")
        error = self._context_gate_error()
        if error is not None:
            return error
        structured = self.context_service.store._connection().execute(
            'SELECT experience_payload FROM context_items WHERE id=?', (item_id,)
        ).fetchone()
        if (structured and structured[0]) or "task_id" in args or "event_id" in args:
            error = self._context_gate_error()
            if error is not None:
                return error
            try:
                return self._after_experience_write(self.context_service.experiences().outcome(
                    args.get("id"), {k:v for k,v in args.items() if k != "id"}))
            except (ValueError, TypeError, ContextValidationError):
                result = self._context_error("invalid_arguments")
                result['requirements'] = (
                    "Structured feedback needs event_id (a stable name for this "
                    "verification), task_id (the actual native session ID, not ws_*; "
                    "use current for the current Codex session), source_kind, exact "
                    "quote, note and conditions. Positive outcomes also need level "
                    "and exact case conditions. A source must resolve to a native "
                    "transcript event; never invent an ID or source."
                )
                return result
            except ContextServiceError as exc:
                return self._context_error(exc.code)
        if not isinstance(args, dict) or set(args) - {"id", "outcome", "note", "source_id"}:
            return self._context_error("invalid_arguments")
        item_id = args.get("id")
        outcome = args.get("outcome")
        note = args.get("note", "")
        if (
            type(item_id) is not int
            or item_id <= 0
            or outcome not in ("success", "failure", "confirmed", "contradicted")
            or not isinstance(note, str)
            or (args.get("source_id") is not None and
                (type(args["source_id"]) is not int or args["source_id"] <= 0))
        ):
            return self._context_error("invalid_arguments")
        gate_error = self._context_gate_error()
        if gate_error is not None:
            return gate_error
        try:
            report = self.context_service.record_outcome(
                item_id, outcome, note=note, source_id=args.get("source_id")
            )
        except (ContextServiceError, ContextLifecycleError) as exc:
            return self._context_error(exc.code)
        except Exception:
            return self._context_error("context_unavailable")
        return {
            "id": report.item_id,
            "evidence_id": report.evidence_id,
            "outcome": report.outcome,
            "status": report.status.value,
            "confidence": report.confidence,
            "archived": report.archived,
            "demoted_playbook_ids": list(report.demoted_playbook_ids),
        }

    def _context_archive_project(self, args: dict) -> dict:
        if not isinstance(args, dict) or set(args) - {"project"}:
            return self._context_error("invalid_arguments")
        project = args.get("project")
        if not isinstance(project, str) or not project.strip():
            return self._context_error("invalid_arguments")
        gate_error = self._context_gate_error()
        if gate_error is not None:
            return gate_error
        try:
            report = self.context_service.archive_project(project)
        except (ContextServiceError, ContextLifecycleError) as exc:
            return self._context_error(exc.code)
        except Exception:
            return self._context_error("context_unavailable")
        return self._purge_response(report)

    def _context_sweep(self, args: dict) -> dict:
        if not isinstance(args, dict) or args:
            return self._context_error("invalid_arguments")
        gate_error = self._context_gate_error()
        if gate_error is not None:
            return gate_error
        try:
            report = self.context_service.sweep_archives()
        except (ContextServiceError, ContextLifecycleError) as exc:
            return self._context_error(exc.code)
        except Exception:
            return self._context_error("context_unavailable")
        return self._purge_response(report)

    @staticmethod
    def _purge_response(report) -> dict:
        """Purge counts plus archive ids; never paths or payload content."""
        return {
            "purged": len(report.purged_archive_ids),
            "failed": len(report.failed_archive_ids),
            "purged_archive_ids": list(report.purged_archive_ids),
            "failed_archive_ids": list(report.failed_archive_ids),
        }

    # ---- continuity tool handlers (MCP dict ↔ typed API boundary) ----

    def _workspace_identity(self) -> WorkspaceIdentityProvider:
        """Server-shared provider keyed by the data dir's workspace.key."""
        if self._workspace_identity_provider is None:
            self._workspace_identity_provider = WorkspaceIdentityProvider(
                key_path=self.config.data_dir / "workspace.key"
            )
        return self._workspace_identity_provider

    def _continuity(self):
        """惰性构建 ContinuityService；借用 context_service 的 store。

        与 context_service 同生命周期：store 由 context_service 持有并在
        shutdown() 关闭，ContinuityService 只是借用连接（与 ProjectStore/
        ProjectRollupGenerator 同一模式）。context_service 缺失（未初始化）
        时返回 None，由门禁映射为 continuity_not_ready。
        """
        service = self._continuity_service
        if service is None:
            context = getattr(self, "context_service", None)
            store = getattr(context, "store", None)
            if store is None:
                return None
            service = ContinuityService(
                self.config, store, self._workspace_identity()
            )
            self._continuity_service = service
        return service

    def _continuity_gate_error(self) -> dict | None:
        """continuity 专用就绪检查：schema 表存在 + workspace key 可用。

        不复用 ``_context_gate_error``：续接不依赖 Core serving gate，
        compat/降级 primary 下续接仍可用，不得被 ``context_not_enabled``
        或 ``degraded_legacy`` 提前拒掉。
        """
        service = self._continuity()
        if service is None:
            return self._context_error("continuity_not_ready")
        try:
            if not service._schema_ready():
                return self._context_error("continuity_not_ready")
            if self._workspace_identity().status().state != "ready":
                return self._context_error("continuity_not_ready")
        except Exception:
            return self._context_error("continuity_not_ready")
        return None

    def _continuity_read_request(self, args: dict):
        """Strict ContinuityResumeRequest build; an error dict on bad input."""
        if not isinstance(args, dict) or set(args) - {
            "workspace_path", "project_hint"
        }:
            return self._context_error("invalid_arguments")
        try:
            return ContinuityResumeRequest(
                workspace_path=args.get("workspace_path"),
                project_hint=args.get("project_hint", ""),
            )
        except (ContinuityValidationError, TypeError):
            return self._context_error("invalid_arguments")

    # ---- continuity begin/find handlers (MCP dict ↔ typed API boundary) ----

    # ContinuityBeginRequest/ContinuityFindRequest 的字段全集（schema
    # additionalProperties: False 的服务端镜像）：未知键直接 invalid_arguments
    _BEGIN_FIELDS = frozenset({
        "workspace_path", "project", "alias", "objective",
        "accepted_decisions", "completed_steps", "current_step",
        "next_action", "blockers", "make_focus",
    })
    _FIND_FIELDS = frozenset({"query", "workspace_path", "limit"})

    def _continuity_begin(self, args: dict) -> dict:
        if not isinstance(args, dict) or set(args) - self._BEGIN_FIELDS:
            return self._context_error("invalid_arguments")
        try:
            request = ContinuityBeginRequest(
                workspace_path=args.get("workspace_path"),
                project=args.get("project"),
                alias=args.get("alias", ""),
                objective=args.get("objective", ""),
                accepted_decisions=args.get("accepted_decisions", ()),
                completed_steps=args.get("completed_steps", ()),
                current_step=args.get("current_step", ""),
                next_action=args.get("next_action", ""),
                blockers=args.get("blockers", ()),
                make_focus=args.get("make_focus", True),
            )
        except ContinuityError as exc:
            # 项目名/别名策略错误是稳定码（如 invalid_project_name）
            return self._context_error(exc.code)
        except (ContinuityValidationError, TypeError):
            return self._context_error("invalid_arguments")
        gate_error = self._continuity_gate_error()
        if gate_error is not None:
            return gate_error
        try:
            result = self._continuity().begin(request)
        except ContinuityError as exc:
            return self._context_error(exc.code)
        except Exception:
            return self._context_error("context_unavailable")
        # 只有指针/修订状态与幂等标记：无正文、无绝对路径、无指纹材料
        return {
            "project": result.project,
            "workstream_id": result.workstream_id,
            "checkpoint_revision": result.checkpoint_revision,
            "state_version": result.state_version,
            "focus_revision": result.focus_revision,
            "status": result.status,
            "context_id": result.context_id,
            "created": result.created,
            "updated": result.updated,
            "registered": result.registered,
            "alias_added": result.alias_added,
            "bound": result.bound,
        }

    def _continuity_find(self, args: dict) -> dict:
        if not isinstance(args, dict) or set(args) - self._FIND_FIELDS:
            return self._context_error("invalid_arguments")
        try:
            request = ContinuityFindRequest(
                query=args.get("query"),
                workspace_path=args.get("workspace_path", "") or "",
                limit=args.get("limit", 10),
            )
        except (ContinuityValidationError, TypeError):
            return self._context_error("invalid_arguments")
        # find 从通用目录也应可用：只要求 continuity schema，workspace key
        # 缺失时降级为“未核验工作区”，绝不因此隐藏 continuity 能力
        service = self._continuity()
        if service is None:
            return self._context_error("continuity_not_ready")
        try:
            if not service._schema_ready():
                return self._context_error("continuity_not_ready")
        except Exception:
            return self._context_error("continuity_not_ready")
        try:
            result = service.find(request)
        except ContinuityError as exc:
            return self._context_error(exc.code)
        except Exception:
            return self._context_error("context_unavailable")
        payload = {
            "code": result.code,
            "candidates": [
                {
                    "project": candidate.project,
                    "matched_via": list(candidate.matched_via),
                    "focus_state": candidate.focus_state,
                    "workstreams": [
                        {
                            "workstream_id": item.workstream_id,
                            "project": item.project,
                            "status": item.status,
                            "checkpoint_revision": item.checkpoint_revision,
                            "state_version": item.state_version,
                            "l0": item.l0,
                            "updated_at": item.updated_at,
                            "workspace_match": item.workspace_match,
                            "staleness": item.staleness,
                        }
                        for item in candidate.workstreams
                    ],
                }
                for candidate in result.candidates
            ],
            # 唯一候选时附可回读 checkpoint（与 resume 同一有界形状）
            "checkpoint": result.checkpoint,
        }
        if result.code != "ok":
            payload["message"] = _CONTEXT_ERROR_MESSAGES.get(
                result.code, result.code
            )
        return payload

    def _continuity_resume(self, args: dict) -> dict:
        request = self._continuity_read_request(args)
        if isinstance(request, dict):
            return request
        gate_error = self._continuity_gate_error()
        if gate_error is not None:
            return gate_error
        try:
            result = self._continuity().resume(request)
        except ContinuityError as exc:
            # resume 设计上对数据不抛错；边界处仍做稳定映射兜底
            return self._context_error(exc.code)
        except Exception:
            return self._context_error("context_unavailable")
        if result.code == "continuity_not_ready":
            # 门禁与服务之间的瞬时竞态（key/schema 在调用间被移除）
            return self._context_error("continuity_not_ready")
        payload = {
            "code": result.code,
            "workstream_id": result.workstream_id,
            "context_id": result.context_id,
            "checkpoint_revision": result.checkpoint_revision,
            "state_version": result.state_version,
            "focus_revision": result.focus_revision,
            "status": result.status,
            "staleness": result.staleness,
            # L0/L1 + L2 权威字段；绝不回传 L2 原文
            "checkpoint": result.checkpoint,
            "candidates": [
                self._workstream_summary(item) for item in result.candidates
            ],
        }
        if result.code != "ok":
            # 短回路码不是协议错误：附稳定文案但不置 isError
            payload["message"] = _CONTEXT_ERROR_MESSAGES.get(
                result.code, result.code
            )
        return payload

    def _continuity_list(self, args: dict) -> dict:
        request = self._continuity_read_request(args)
        if isinstance(request, dict):
            return request
        gate_error = self._continuity_gate_error()
        if gate_error is not None:
            return gate_error
        try:
            summaries = self._continuity().list_open(request)
        except ContinuityError as exc:
            return self._context_error(exc.code)
        except Exception:
            return self._context_error("context_unavailable")
        rows = [self._workstream_summary(item) for item in summaries]
        return {"workstreams": rows, "count": len(rows)}

    # ---- optional existing-project progress synchronization ----

    _PROJECT_BOARD_FIELDS = frozenset({
        "workspace_path", "project_hint", "workstream_id",
    })

    def _project_board(self):
        adapter = self._project_board_adapter
        if adapter is None:
            context = getattr(self, "context_service", None)
            store = getattr(context, "store", None)
            if store is None:
                return None
            adapter = ProjectBoardSync(
                self.config, store, self._workspace_identity()
            )
            self._project_board_adapter = adapter
        return adapter

    def _project_board_args(self, args: dict) -> dict | None:
        if not isinstance(args, dict) or set(args) - self._PROJECT_BOARD_FIELDS:
            return None
        workspace_path = args.get("workspace_path")
        project_hint = args.get("project_hint", "")
        workstream_id = args.get("workstream_id", "")
        if (
            not isinstance(workspace_path, str)
            or not workspace_path.strip()
            or not isinstance(project_hint, str)
            or not isinstance(workstream_id, str)
        ):
            return None
        return {
            "workspace_path": workspace_path,
            "project_hint": project_hint,
            "workstream_id": workstream_id,
        }

    def _project_board_sync(self, args: dict) -> dict:
        request = self._project_board_args(args)
        if request is None:
            return self._context_error("invalid_arguments")
        adapter = self._project_board()
        if adapter is None:
            return {
                "status": "disabled",
                "message": "project board sync is disabled",
            }
        try:
            return adapter.sync(**request)
        except Exception:
            return {
                "status": "pending",
                "message": "project board sync remains pending",
            }

    def _project_board_status(self, args: dict) -> dict:
        request = self._project_board_args(args)
        if request is None:
            return self._context_error("invalid_arguments")
        adapter = self._project_board()
        if adapter is None:
            return {
                "status": "disabled",
                "message": "project board sync is disabled",
            }
        try:
            return adapter.status(**request)
        except Exception:
            return {
                "status": "pending",
                "message": "project board sync status is unavailable",
            }

    @staticmethod
    def _workstream_summary(summary) -> dict:
        """Bounded candidate projection: metadata plus L0, never L1/L2."""
        return {
            "workstream_id": summary.workstream_id,
            "project": summary.project,
            "status": summary.status,
            "checkpoint_revision": summary.checkpoint_revision,
            "state_version": summary.state_version,
            "l0": summary.l0,
            "updated_at": summary.updated_at,
        }

    # ContinuityCheckpointRequest 的字段全集（schema additionalProperties:
    # False 的服务端镜像）：未知键直接 invalid_arguments
    _CHECKPOINT_FIELDS = frozenset({
        "action", "workspace_path", "project_hint", "workstream_id",
        "objective", "accepted_decisions", "completed_steps", "current_step",
        "next_action", "blockers", "parent_workstream_id",
        "source_context_ids", "make_focus", "expected_checkpoint_revision",
        "expected_state_version", "expected_focus_revision",
    })

    def _continuity_checkpoint(self, args: dict) -> dict:
        if not isinstance(args, dict) or set(args) - self._CHECKPOINT_FIELDS:
            return self._context_error("invalid_arguments")
        try:
            request = ContinuityCheckpointRequest(
                action=args.get("action"),
                workspace_path=args.get("workspace_path"),
                project_hint=args.get("project_hint", ""),
                workstream_id=args.get("workstream_id", ""),
                objective=args.get("objective", ""),
                accepted_decisions=args.get("accepted_decisions", ()),
                completed_steps=args.get("completed_steps", ()),
                current_step=args.get("current_step", ""),
                next_action=args.get("next_action", ""),
                blockers=args.get("blockers", ()),
                parent_workstream_id=args.get("parent_workstream_id", ""),
                source_context_ids=args.get("source_context_ids", ()),
                make_focus=args.get("make_focus", False),
                expected_checkpoint_revision=args.get(
                    "expected_checkpoint_revision", 0
                ),
                expected_state_version=args.get("expected_state_version", 0),
                expected_focus_revision=args.get("expected_focus_revision"),
            )
        except (ContinuityValidationError, TypeError):
            return self._context_error("invalid_arguments")
        gate_error = self._continuity_gate_error()
        if gate_error is not None:
            return gate_error
        try:
            result = self._continuity().checkpoint(request)
        except ContinuityError as exc:
            return self._context_error(exc.code)
        except Exception:
            return self._context_error("context_unavailable")
        sync_receipt = None
        if request.action in {
            ContinuityAction.UPDATE.value,
            ContinuityAction.PAUSE.value,
            ContinuityAction.RESUME.value,
            ContinuityAction.BLOCK.value,
            ContinuityAction.UNBLOCK.value,
            ContinuityAction.COMPLETE.value,
            ContinuityAction.CANCEL.value,
        }:
            # Persistence has committed before this best-effort transport.
            # Any config/state/network failure remains delivery state and must
            # never turn a saved checkpoint into an apparent failed write.
            try:
                adapter = self._project_board()
                if adapter is not None:
                    candidate = adapter.sync(
                        request.workspace_path,
                        project_hint=request.project_hint,
                        workstream_id=result.workstream_id,
                    )
                    if candidate.get("status") != "disabled":
                        sync_receipt = candidate
            except Exception:
                sync_receipt = {
                    "status": "pending",
                    "message": "project board sync remains pending",
                }
        # 只有指针/修订状态：无正文、无绝对路径、无指纹材料
        payload = {
            "workstream_id": result.workstream_id,
            "checkpoint_revision": result.checkpoint_revision,
            "state_version": result.state_version,
            "focus_revision": result.focus_revision,
            "status": result.status,
            "context_id": result.context_id,
        }
        if sync_receipt is not None:
            payload["project_board_sync"] = sync_receipt
        return payload

    # ---- mode/health views and mutation boundary helpers ----

    def _live_status(self):
        """已初始化服务的健康快照；不可用返回 None。

        PRIMARY 每次调用都用与设计门禁相同的评估器重新复查，服务在
        早前 tools/list 之后降级也不能凭旧健康结论继续服务。
        """
        service = getattr(self, "context_service", None)
        if service is None:
            return None
        try:
            status = service.status()
        except Exception:
            return None
        if status.mode is ContextMode.PRIMARY:
            try:
                service._refresh_health()  # 与正式门禁同一评估器，按次复查
                status = service.status()
            except Exception:
                return None
        return status

    def _contract_view(self):
        """(adapter, mode, health) 三元组，喂给单一工具注册表。"""
        status = self._live_status()
        if status is not None:
            return status.adapter, status.mode, status
        if self.context_mode is None:
            return self.adapter, None, None
        if (
            self.adapter in CONTEXT_CORE_ADAPTERS
            and self.context_mode in (ContextMode.SHADOW, ContextMode.PRIMARY)
            and not self._init_done.is_set()
        ):
            # 有界轻量等待：后台 initialize() 先完成 Context 健康评估并
            # 置位 _service_evaluated，再加载可选的 embedding 模型；超时按
            # health 未知 fail-closed，绝不等待模型加载。
            self._service_evaluated.wait(timeout=self._HEALTH_WAIT_TIMEOUT_S)
            status = self._live_status()
            if status is not None:
                return status.adapter, status.mode, status
        return self.adapter, self.context_mode, None

    def _write_gate_error(self) -> str | None:
        """Refuse writes on invalid config or a degraded primary gate."""
        if self.context_mode is None:
            return ("context configuration is invalid; "
                    "legacy writes are rejected (fail-closed)")
        status = self._live_status()
        if status is None:
            if self.context_mode is ContextMode.PRIMARY:
                # 健康未知与读侧同向 fail-closed：primary 的瞬时评估
                # 异常不得放行写；legacy/compat 不依赖 Context 健康
                return ("context primary health is unknown; "
                        "legacy writes are rejected (fail-closed)")
            return None
        if status.mode is ContextMode.PRIMARY and not status.ready:
            return ("context primary mode is degraded_legacy; "
                    "legacy writes are rejected until the gate recovers")
        return None

    def _mutation_response(self, base: dict, result) -> dict:
        """Attach optional context fields and a non-secret degraded index flag."""
        base["context_id"] = result.context_id
        base["old_context_id"] = result.old_context_id
        base["available_layers"] = [
            layer.value for layer in result.available_layers
        ]
        if self._index_degraded():
            base["index_state"] = "degraded"
        return base

    def _index_degraded(self) -> bool:
        """True when a post-commit vector sync left an independent dirty marker."""
        status = self._live_status()
        if status is None:
            return False
        return bool(status.legacy_vector_dirty or status.context_vector_dirty)

    # ---- internals ----

    def _rebuild_under_lock(self) -> None:
        """跨进程串行化全量重建。

        多个 kimi 窗口并发启动时若各自全量重建，N 份 llama 编码互相拖慢
        （2026-08-06 实测 4 进程并发单次重建从 ~25s 拖到 >10min），init 门闩
        120s 超时导致 tools/call 全部超时。flock 串行化；拿到锁先复查一致性
        ——多数情况下前一个进程已重建落盘，本轮直接跳过。
        锁在重建期间持有；持锁进程崩溃时 flock 随 fd 关闭自动释放。
        """
        import fcntl
        facade = self.context_service.legacy_facade()
        lock_path = self.config.vector_path.with_suffix(
            f"{self.config.vector_path.suffix}.rebuild.lock")
        with open(lock_path, "a") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                sqlite_count = len(facade.all_ids())
                if self.vidx.check_consistency(sqlite_count):
                    self._log("Index already rebuilt by another process, skipping")
                    return
                self._rebuild_vector_index()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _rebuild_vector_index(self):
        """Rebuild USearch index from SQLite."""
        self._log("Vector index out of sync with SQLite, rebuilding...")
        facade = self.context_service.legacy_facade()
        all_ids = facade.all_ids()
        if not all_ids:
            self._log("No records in SQLite, skipping rebuild")
            return
        if not self.engine.is_loaded:
            self._log("Embedding engine not loaded, cannot rebuild vector index")
            return

        records = facade.get_by_ids(all_ids)
        ids = []
        embeddings = []
        for r in records:
            try:
                vec = self.engine.encode_document(r["value"])
                import numpy as np
                ids.append(r["id"])
                embeddings.append(np.array(vec, dtype=np.float32))
            except Exception as e:
                self._log(f"Encoding failed (id={r['id']}): {e}")

        if ids:
            self.vidx.rebuild(ids, embeddings)
            # 立即落盘，不要等 shutdown：进程被客户端超时强杀时shutdown 跑不到，
            # 索引文件缺失会让下次启动又全量重建（2026-08-06 复发超时的恶性循环）
            try:
                self.vidx.save()
                # 全量重建已覆盖 SQLite 现状并落盘，此前任何进程留下的
                # 未落盘标记都已了结（新增漂移由 count 一致性检查兜底）
                self.vidx.clear_dirty()
                self._log(f"Rebuild complete: {len(ids)} vectors (saved)")
            except Exception as e:
                self._log(f"Rebuild complete: {len(ids)} vectors, but save failed: {e}")

    @staticmethod
    def _log(msg: str):
        print(f"[evolvmem] {msg}", file=sys.stderr, flush=True)

    # ---- MCP protocol ----

    _PARENT_CHECK_INTERVAL_S = 60  # stdin 空闲多久检查一次父进程存活
    _INIT_WAIT_TIMEOUT_S = 120     # tools/call 等待初始化完成的上限

    def _start_init_thread(self) -> None:
        """Run heavy initialize() in a daemon thread so the MCP handshake
        is answered immediately. Model loading and a full vector-index
        rebuild can exceed the client's startup timeout (2026-07-31:
        200-vector rebuild took >60s and the handshake timed out)."""
        def _init():
            try:
                self.initialize()
            except Exception as e:
                self._init_error = e
                self._log(f"Initialization failed: {e}")
                traceback.print_exc(file=sys.stderr)
            finally:
                self._init_done.set()

        threading.Thread(target=_init, name="evolvmem-init", daemon=True).start()

    def _init_gate_error(self) -> str | None:
        """None when ready to serve tools/call, else a human-readable error."""
        if not self._init_done.is_set():
            if not self._init_done.wait(timeout=self._INIT_WAIT_TIMEOUT_S):
                return (f"server still initializing after "
                        f"{self._INIT_WAIT_TIMEOUT_S}s (model loading / "
                        f"vector index rebuild); retry shortly")
        if self._init_error is not None:
            return f"server initialization failed: {self._init_error}"
        return None

    def _parent_gone(self) -> bool:
        """Parent (kimi CLI) died → we were re-parented (to init or a
        subreaper like systemd --user, whose pid is NOT 1). Compare against
        the ppid we started with instead of assuming orphan ⇒ ppid 1."""
        return os.getppid() != self._original_ppid

    @staticmethod
    def _dbg(msg: str):
        """Raw I/O debug trace, enabled via EVOLVMEM_DEBUG_LOG=<path>."""
        path = os.environ.get("EVOLVMEM_DEBUG_LOG")
        if not path:
            return
        try:
            import time as _time
            with open(path, "a") as f:
                f.write(f"[{_time.strftime('%H:%M:%S')}] pid={os.getpid()} ppid={os.getppid()} {msg}\n")
        except Exception:
            pass

    def run(self):
        """stdio MCP main loop.

        Exits on stdin EOF (normal shutdown) or when the parent process is
        gone (kimi killed/crashed): without this, an orphaned server blocks
        on readline forever, holding its SQLite connection — and any
        uncommitted write transaction — hostage (2026-07-29 lockup).
        """
        self._original_ppid = os.getppid()
        self._log("MCP Server starting")
        self._dbg("run() entered")
        self._start_init_thread()

        stdin_fd = sys.stdin.fileno()
        pending = b""
        while True:
            # 只在缓冲区没有完整行时才碰 fd。
            # 不能用 select + sys.stdin.readline()：TextIOWrapper 会把多条消息
            # 预读进 userspace 缓冲，select 在裸 fd 上看不到它们，于是整条消息
            # 被饿死，直到客户端关管道（2026-07-31 kimi 握手挂起 180s 的根因：
            # tools/list 紧跟 notification 到达，被预读吞掉，select 空转）。
            if b"\n" not in pending:
                ready, _, _ = select.select([stdin_fd], [], [],
                                            self._PARENT_CHECK_INTERVAL_S)
                if not ready:
                    self._dbg("select tick (no stdin data)")
                    if self._parent_gone():
                        self._log("parent process gone, exiting")
                        break
                    continue
                chunk = os.read(stdin_fd, 65536)
                self._dbg(f"os.read -> {len(chunk)}B")
                if chunk:
                    pending += chunk
                    continue
                if not pending:  # EOF — client closed the pipe
                    break
            line_b, _, pending = pending.partition(b"\n")
            line = line_b.decode("utf-8", errors="replace").strip()
            self._dbg(f"line -> {line[:80]!r}")
            if not line:
                continue
            request = None
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                self._log(f"Invalid JSON: {line[:100]}")
                continue
            try:
                response = self._handle_request(request)
                if response is not None:
                    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                    sys.stdout.flush()
                    self._dbg(f"responded to id={request.get('id')} method={request.get('method')}")
            except Exception:
                self._log("Request handling error")
                traceback.print_exc(file=sys.stderr)
                error_response = {
                    "jsonrpc": "2.0",
                    "id": request.get("id") if request else None,
                    "error": {
                        "code": -32603,
                        "message": "Internal error",
                    },
                }
                try:
                    sys.stdout.write(json.dumps(error_response, ensure_ascii=False) + "\n")
                    sys.stdout.flush()
                except Exception:
                    self._log("Failed to write error response")

        self._log("MCP Server exiting")
        self.shutdown()

    def _handle_request(self, request: dict) -> dict | None:
        method = request.get("method", "")
        req_id = request.get("id")

        # JSON-RPC notifications (no id) must not be answered
        if req_id is None:
            return None

        if method == "initialize":
            params = request.get("params") or {}
            protocol_version = params.get("protocolVersion", "2024-11-05")
            result = {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "evolvmem",
                    "version": "0.1.0",
                },
            }
            adapter, mode, health = self._contract_view()
            instructions = initialization_instructions(
                adapter=adapter, mode=mode, health=health
            )
            if instructions is not None:
                result["instructions"] = instructions
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result,
            }

        elif method == "ping":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {},
            }

        elif method == "tools/list":
            adapter, mode, health = self._contract_view()
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": spec.name,
                            "description": spec.description,
                            "inputSchema": spec.input_schema,
                            "annotations": spec.annotations,
                        }
                        for spec in tool_specs(
                            adapter=adapter, mode=mode, health=health
                        )
                    ]
                },
            }

        elif method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            adapter, mode, health = self._contract_view()
            exposed = {
                spec.name
                for spec in tool_specs(adapter=adapter, mode=mode, health=health)
            }
            if tool_name not in exposed:
                # 与 tools/list 同一注册表：隐藏工具不能被调用；
                # 未知工具也无需等待初始化门闩
                result = {"error": f"Unknown tool: {tool_name}"}
            elif tool_name.startswith(("context_", "continuity_", "project_board_")):
                # context_*/continuity_* 不等重初始化门闩：服务未就绪即返回
                # 各自的稳定错误（continuity_not_ready / context_* gate）
                result = self.handle_tool_call(tool_name, tool_args)
            else:
                gate_error = self._init_gate_error()
                if gate_error is not None:
                    result = {"error": gate_error}
                else:
                    result = self.handle_tool_call(tool_name, tool_args)
            response_result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False),
                    }
                ]
            }
            if isinstance(result, dict) and "error" in result:
                response_result["isError"] = True
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": response_result,
            }

        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}",
                },
            }


def main():
    server = MemoryMCPServer()
    server.run()


if __name__ == "__main__":
    main()
