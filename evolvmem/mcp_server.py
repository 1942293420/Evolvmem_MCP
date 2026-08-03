#!/usr/bin/env python3
"""EvolvMem memory plugin — stdio MCP Server.

Tools:
  memory_search   — FTS5/trigram + HNSW hybrid search
  memory_status   — statistics
  memory_add      — manually add a memory
  memory_replace  — replace a memory (mark old as superseded)
  memory_remove   — soft-delete a memory
  memory_consolidate — find/merge near-duplicate memories
"""

import json
import math
import select
import sys
import os
import threading
import traceback
from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex
from evolvmem.embedding import EmbeddingEngine
from evolvmem.retriever import Retriever
from evolvmem.conflict_detector import ConflictDetector
from evolvmem.forgetting import ForgettingEngine
from evolvmem.consolidator import Consolidator
from evolvmem.semantic_merge import find_semantic_match


# 低信息过渡语：自动摘要里常见的"零价值"句式（命中即拒收）
_LOW_INFO_PATTERNS = (
    "等待用户", "会话继续", "等待下一步", "等待用户确认",
    "等待用户后续", "no action required",
)


def _is_low_info(value: str) -> bool:
    # 整句匹配语义：strip 后以模式开头才算低信息（句中出现不误伤）；casefold 兼容大小写变体
    v = value.strip().casefold()
    return any(v.startswith(p.casefold()) for p in _LOW_INFO_PATTERNS)


class MemoryMCPServer:
    """stdio MCP Server — JSON-RPC protocol."""

    def __init__(self):
        self.config = Config.from_file()
        self.store = MemoryStore(self.config)
        self.vidx = VectorIndex(self.config)
        self.engine = EmbeddingEngine(self.config)
        self.retriever = None
        self.conflict_detector = None
        self.forgetting = None
        self.consolidator = None
        # 初始化门闩：run() 里由后台线程完成重初始化后置位，
        # tools/call 等待它，握手（initialize/tools/list）不等
        self._init_done = threading.Event()
        self._init_error: Exception | None = None

    def initialize(self):
        """Initialize all components."""
        self.store.initialize()
        self.vidx.initialize(dim=self.config.embedding_dim)

        # Try loading the embedding model (FTS5 search works without it)
        try:
            self.engine.initialize()
        except Exception as e:
            # 宽捕获：模型加载的任何瞬时失败（缺文件/缺依赖/内存不足）都降级为
            # 仅 FTS 搜索，而不是让整个会话的 tools/call 被 _init_error 堵死
            self._log(f"Embedding engine not loaded: {e}")

        # Check USearch vs SQLite consistency (needs engine for rebuild)
        sqlite_count = len(self.store.all_ids())
        if not self.vidx.check_consistency(sqlite_count):
            self._rebuild_vector_index()

        self.retriever = Retriever(
            self.config, self.store, self.vidx, self.engine
        )
        self.conflict_detector = ConflictDetector(self.store)
        self.forgetting = ForgettingEngine(self.config, self.store)
        self.consolidator = Consolidator(
            self.config, self.store, self.vidx, self.engine
        )

    def shutdown(self):
        """Clean up resources."""
        # 等后台初始化结束，避免关闭与初始化并发操作同一资源
        self._init_done.wait()
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
        try:
            if self.store:
                self.store.close()
        except Exception:
            pass

    # ---- tool handlers ----

    def handle_tool_call(self, tool_name: str, args: dict) -> dict:
        """Route tool calls."""
        handlers = {
            "memory_search": self._memory_search,
            "memory_status": self._memory_status,
            "memory_add": self._memory_add,
            "memory_replace": self._memory_replace,
            "memory_remove": self._memory_remove,
            "memory_consolidate": self._memory_consolidate,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return {"error": f"Unknown tool: {tool_name}"}
        return handler(args)

    def _memory_search(self, args: dict) -> dict:
        query = args.get("query", "")
        top_k = int(args.get("top_k", 10))
        if not query:
            return {"error": "query parameter cannot be empty"}
        results = self.retriever.search(query, top_k=top_k)
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

    def _memory_status(self, args: dict) -> dict:
        total_active = self.store.count_active()
        total_all = len(self.store.all_ids())
        vector_count = self.vidx.count()
        return {
            "active_memories": total_active,
            "total_records": total_all,
            "vector_count": vector_count,
            "embedding_loaded": self.engine.is_loaded,
            "embedding_dim": self.config.embedding_dim,
            "data_dir": str(self.config.data_dir),
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
        extra = {}
        if importance is not None:
            extra["importance"] = importance
        if tier is not None:
            extra["tier"] = tier
        expires_at = args.get("expires_at")
        if expires_at is not None:
            extra["expires_at"] = expires_at

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
        elif decision.action == "replace":
            # Conflict detector determined replace: use replace() to mark old as superseded
            old_id = decision.existing_id
            new_id = self.store.replace(key=key, new_value=value, **extra)

            # Update vector index
            if self.engine.is_loaded:
                try:
                    vec = self.engine.encode_document(value)
                    import numpy as np
                    self.vidx.add(new_id, np.array(vec, dtype=np.float32))
                    self.vidx.save()
                except Exception as e:
                    self._log(f"Vector update failed (id={new_id}): {e}")

            return {"status": "replaced", "new_id": new_id, "old_id": old_id}
        else:
            # decision.action == "add": 同 key 无冲突 → 再做跨 key 语义合并
            # （tier == "reference" 的新值同样不参与合并：永不 supersede 别人）
            if self.engine.is_loaded and tier != "reference":
                match = find_semantic_match(
                    self.store, self.vidx, self.engine, value,
                    self.config.add_merge_threshold)
                if match:
                    new_id = self.store.replace(
                        key=match["key"], new_value=value, **extra)
                    if self.engine.is_loaded:
                        try:
                            vec = self.engine.encode_document(value)
                            import numpy as np
                            self.vidx.add(new_id, np.array(vec, dtype=np.float32))
                            self.vidx.save()
                        except Exception as e:
                            self._log(f"Vector update failed (id={new_id}): {e}")
                    return {"status": "merged", "merged_into": match["id"],
                            "key": match["key"],
                            "similarity": match["similarity"],
                            "new_id": new_id}
            # no existing key and no semantic match, insert directly
            mem_id = self.store.add(
                key=key, value=value, attribute=attribute, tags=tags, **extra
            )

        # Update vector index
        if self.engine.is_loaded:
            try:
                vec = self.engine.encode_document(value)
                import numpy as np
                self.vidx.add(mem_id, np.array(vec, dtype=np.float32))
                self.vidx.save()
            except Exception as e:
                self._log(f"Vector update failed (id={mem_id}): {e}")

        return {"status": "added", "id": mem_id}

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

        new_id = self.store.replace(key=key, new_value=new_value)

        # Update vector index
        if self.engine.is_loaded:
            try:
                vec = self.engine.encode_document(new_value)
                import numpy as np
                self.vidx.add(new_id, np.array(vec, dtype=np.float32))
                self.vidx.save()
            except Exception as e:
                self._log(f"Vector update failed (id={new_id}): {e}")

        return {"status": "replaced", "new_id": new_id}

    def _memory_remove(self, args: dict) -> dict:
        mem_id = int(args.get("id", 0))
        if not mem_id:
            return {"error": "id parameter cannot be empty"}
        self.store.remove(mem_id)
        # Keep the vector index in sync so counts stay consistent with SQLite
        if self.vidx is not None:
            try:
                self.vidx.remove(mem_id)
                self.vidx.save()
            except Exception as e:
                self._log(f"Vector removal failed (id={mem_id}): {e}")
        return {"status": "deleted", "id": mem_id}

    def _memory_consolidate(self, args: dict) -> dict:
        if not self.engine.is_loaded:
            return {"error": "embedding engine not loaded"}
        dry_run = bool(args.get("dry_run", True))
        threshold = args.get("threshold")
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

    # ---- internals ----

    def _rebuild_vector_index(self):
        """Rebuild USearch index from SQLite."""
        self._log("Vector index out of sync with SQLite, rebuilding...")
        all_ids = self.store.all_ids()
        if not all_ids:
            self._log("No records in SQLite, skipping rebuild")
            return
        if not self.engine.is_loaded:
            self._log("Embedding engine not loaded, cannot rebuild vector index")
            return

        records = self.store.get_by_ids(all_ids)
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
            self._log(f"Rebuild complete: {len(ids)} vectors")

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
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "evolvmem",
                        "version": "0.1.0",
                    },
                },
            }

        elif method == "ping":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {},
            }

        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "memory_search",
                            "description": "Hybrid memory search: FTS5/trigram exact match + HNSW vector semantic search. Supports Chinese substring matching and semantic similarity.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "Search query",
                                    },
                                    "top_k": {
                                        "type": "integer",
                                        "description": "Number of results to return, default 10",
                                        "default": 10,
                                    },
                                },
                                "required": ["query"],
                            },
                        },
                        {
                            "name": "memory_status",
                            "description": "View memory system status: active count, total records, vector index status.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {},
                            },
                        },
                        {
                            "name": "memory_add",
                            "description": "Manually add a memory. Performs automatic conflict detection.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "key": {
                                        "type": "string",
                                        "description": "Stable key, format: project:domain:type:topic",
                                    },
                                    "value": {
                                        "type": "string",
                                        "description": "Memory content (value 至少 10 字符，低信息过渡语会被拒收)",
                                    },
                                    "attribute": {
                                        "type": "string",
                                        "description": "Category: decision|preference|fact|constraint|user_profile",
                                        "default": "fact",
                                    },
                                    "tags": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "List of tags",
                                    },
                                    "importance": {
                                        "type": "number",
                                        "description": "Importance 1-10 (default 5). 9-10 hard constraints, 7-8 key decisions, 5-6 ordinary facts",
                                    },
                                    "tier": {
                                        "type": "string",
                                        "enum": ["pinned", "normal", "reference"],
                                        "description": "pinned = injected every session; normal = scored competition; reference = never injected, only searchable (for long documents)",
                                    },
                                    "expires_at": {
                                        "type": "string",
                                        "description": "Optional expiry, e.g. 2026-12-31; expired memories stop being injected and get archived",
                                    },
                                },
                                "required": ["key", "value"],
                            },
                        },
                        {
                            "name": "memory_replace",
                            "description": "Replace a memory. Old value marked as superseded, new value set to active. Full history preserved.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "key": {
                                        "type": "string",
                                        "description": "Stable key of the memory to replace",
                                    },
                                    "value": {
                                        "type": "string",
                                        "description": "New memory content (value 至少 10 字符，低信息过渡语会被拒收)",
                                    },
                                },
                                "required": ["key", "value"],
                            },
                        },
                        {
                            "name": "memory_remove",
                            "description": "Soft-delete a memory (status marked as deleted, data retained).",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "integer",
                                        "description": "Memory ID",
                                    },
                                },
                                "required": ["id"],
                            },
                        },
                        {
                            "name": "memory_consolidate",
                            "description": "Find and merge near-duplicate memories (vector similarity). dry_run=true (default) only reports candidates.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "dry_run": {"type": "boolean", "default": True},
                                    "threshold": {"type": "number",
                                                  "description": "similarity threshold, default from config (0.92)"},
                                },
                            },
                        },
                    ]
                },
            }

        elif method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            known_tools = {
                "memory_search", "memory_status", "memory_add",
                "memory_replace", "memory_remove", "memory_consolidate",
            }
            if tool_name not in known_tools:
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
