#!/usr/bin/env python3
"""Claude Code 记忆插件 — stdio MCP Server。

工具:
  memory_search   — FTS5/trigram + HNSW 混合检索
  memory_status   — 统计信息
  memory_add      — 手动写入记忆
  memory_replace  — 替换记忆（标记旧值为 superseded）
  memory_remove   — 软删除记忆
"""

import json
import sys
import os
import traceback
from hermes_memory.config import Config
from hermes_memory.memory_store import MemoryStore
from hermes_memory.vector_index import VectorIndex
from hermes_memory.embedding import EmbeddingEngine
from hermes_memory.retriever import Retriever
from hermes_memory.conflict_detector import ConflictDetector
from hermes_memory.forgetting import ForgettingEngine


class MemoryMCPServer:
    """stdio MCP Server — JSON-RPC 协议。"""

    def __init__(self):
        self.config = Config.from_file()
        self.store = MemoryStore(self.config)
        self.vidx = VectorIndex(self.config)
        self.engine = EmbeddingEngine(self.config)
        self.retriever = None
        self.conflict_detector = None
        self.forgetting = None

    def initialize(self):
        """初始化所有组件。"""
        self.store.initialize()
        self.vidx.initialize(dim=self.config.embedding_dim)

        # 检查 USearch 与 SQLite 一致性
        sqlite_count = len(self.store.all_ids())
        if not self.vidx.check_consistency(sqlite_count):
            self._rebuild_vector_index()
        else:
            self._log(
                "USearch 与 SQLite 条目数一致，但未进行 ID 级验证。"
                "如果语义搜索返回错误结果，请删除向量文件（vectors.usearch）强制重建。"
            )

        # 尝试加载 embedding 模型（失败不影响 FTS5 检索）
        try:
            self.engine.initialize()
        except (FileNotFoundError, ImportError) as e:
            self._log(f"Embedding 引擎未加载: {e}")

        self.retriever = Retriever(
            self.config, self.store, self.vidx, self.engine
        )
        self.conflict_detector = ConflictDetector(self.store)
        self.forgetting = ForgettingEngine(self.config, self.store)

    def shutdown(self):
        """清理资源。"""
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

    # ---- 工具处理 ----

    def handle_tool_call(self, tool_name: str, args: dict) -> dict:
        """路由工具调用。"""
        handlers = {
            "memory_search": self._memory_search,
            "memory_status": self._memory_status,
            "memory_add": self._memory_add,
            "memory_replace": self._memory_replace,
            "memory_remove": self._memory_remove,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return {"error": f"Unknown tool: {tool_name}"}
        return handler(args)

    def _memory_search(self, args: dict) -> dict:
        query = args.get("query", "")
        top_k = int(args.get("top_k", 10))
        if not query:
            return {"error": "query 参数不能为空"}
        results = self.retriever.search(query, top_k=top_k)
        return {
            "results": [
                {
                    "id": r["id"],
                    "key": r["key"],
                    "value": r["value"],
                    "status": r["status"],
                    "category": r["category"],
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
        category = args.get("category", "fact")
        tags = args.get("tags", [])

        if not key or not value:
            return {"error": "key 和 value 参数不能为空"}

        # 冲突检测
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
            # 冲突检测判定为替换：使用 replace() 标记旧值为 superseded
            old_id = decision.existing_id
            new_id = self.store.replace(key=key, new_value=value)

            # 更新向量索引
            if self.engine.is_loaded:
                try:
                    vec = self.engine.encode(value)
                    import numpy as np
                    self.vidx.add(new_id, np.array(vec, dtype=np.float32))
                    self.vidx.save()
                except Exception as e:
                    self._log(f"向量更新失败 (id={new_id}): {e}")

            return {"status": "replaced", "new_id": new_id, "old_id": old_id}
        else:
            # decision.action == "add"：无现有 key，直接新增
            mem_id = self.store.add(
                key=key, value=value, category=category, tags=tags
            )

        # 更新向量索引
        if self.engine.is_loaded:
            try:
                vec = self.engine.encode(value)
                import numpy as np
                self.vidx.add(mem_id, np.array(vec, dtype=np.float32))
                self.vidx.save()
            except Exception as e:
                self._log(f"向量更新失败 (id={mem_id}): {e}")

        return {"status": "added", "id": mem_id}

    def _memory_replace(self, args: dict) -> dict:
        key = args.get("key", "")
        new_value = args.get("value", "")

        if not key or not new_value:
            return {"error": "key 和 value 参数不能为空"}

        new_id = self.store.replace(key=key, new_value=new_value)

        # 更新向量索引
        if self.engine.is_loaded:
            try:
                vec = self.engine.encode(new_value)
                import numpy as np
                self.vidx.add(new_id, np.array(vec, dtype=np.float32))
                self.vidx.save()
            except Exception as e:
                self._log(f"向量更新失败 (id={new_id}): {e}")

        return {"status": "replaced", "new_id": new_id}

    def _memory_remove(self, args: dict) -> dict:
        mem_id = int(args.get("id", 0))
        if not mem_id:
            return {"error": "id 参数不能为空"}
        self.store.remove(mem_id)
        return {"status": "deleted", "id": mem_id}

    # ---- 内部 ----

    def _rebuild_vector_index(self):
        """从 SQLite 重建 USearch 索引。"""
        self._log("检测到 USearch 索引与 SQLite 不一致，正在重建...")
        all_ids = self.store.all_ids()
        if not all_ids:
            self._log("SQLite 中无记录，跳过重建")
            return
        if not self.engine.is_loaded:
            self._log("Embedding 引擎未加载，无法重建向量索引")
            return

        records = self.store.get_by_ids(all_ids)
        ids = []
        embeddings = []
        for r in records:
            try:
                vec = self.engine.encode(r["value"])
                import numpy as np
                ids.append(r["id"])
                embeddings.append(np.array(vec, dtype=np.float32))
            except Exception as e:
                self._log(f"编码失败 (id={r['id']}): {e}")

        if ids:
            self.vidx.rebuild(ids, embeddings)
            self._log(f"重建完成: {len(ids)} 条向量")

    @staticmethod
    def _log(msg: str):
        print(f"[hermes-memory] {msg}", file=sys.stderr, flush=True)

    # ---- MCP 协议 ----

    def run(self):
        """stdio MCP 主循环。"""
        self._log("MCP Server 启动")
        try:
            self.initialize()
        except Exception as e:
            self._log(f"初始化失败: {e}")
            traceback.print_exc(file=sys.stderr)
            sys.exit(1)

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            request = None
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                self._log(f"无效 JSON: {line[:100]}")
                continue
            try:
                response = self._handle_request(request)
                if response is not None:
                    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                    sys.stdout.flush()
            except Exception:
                self._log("处理请求异常")
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
                    self._log("无法写入错误响应")

        self._log("MCP Server 退出")
        self.shutdown()

    def _handle_request(self, request: dict) -> dict | None:
        method = request.get("method", "")
        req_id = request.get("id")

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "hermes-memory",
                        "version": "0.1.0",
                    },
                },
            }

        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "memory_search",
                            "description": "混合检索记忆：FTS5/trigram 精确匹配 + HNSW 向量语义搜索。支持中文子串匹配和语义相似度。",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "搜索查询",
                                    },
                                    "top_k": {
                                        "type": "integer",
                                        "description": "返回结果数，默认 10",
                                        "default": 10,
                                    },
                                },
                                "required": ["query"],
                            },
                        },
                        {
                            "name": "memory_status",
                            "description": "查看记忆系统状态：活跃记忆数、总记录数、向量索引状态。",
                            "inputSchema": {
                                "type": "object",
                                "properties": {},
                            },
                        },
                        {
                            "name": "memory_add",
                            "description": "手动添加一条记忆。自动检测冲突。",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "key": {
                                        "type": "string",
                                        "description": "稳定 key，格式: project:domain:type:topic",
                                    },
                                    "value": {
                                        "type": "string",
                                        "description": "记忆内容",
                                    },
                                    "category": {
                                        "type": "string",
                                        "description": "分类: decision|preference|fact|constraint|user_profile",
                                        "default": "fact",
                                    },
                                    "tags": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "标签列表",
                                    },
                                },
                                "required": ["key", "value"],
                            },
                        },
                        {
                            "name": "memory_replace",
                            "description": "替换一条记忆。旧值标记为 superseded，新值设为 active。保留完整历史。",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "key": {
                                        "type": "string",
                                        "description": "要替换的记忆的稳定 key",
                                    },
                                    "value": {
                                        "type": "string",
                                        "description": "新的记忆内容",
                                    },
                                },
                                "required": ["key", "value"],
                            },
                        },
                        {
                            "name": "memory_remove",
                            "description": "软删除一条记忆（状态标记为 deleted，数据保留）。",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "integer",
                                        "description": "记忆 ID",
                                    },
                                },
                                "required": ["id"],
                            },
                        },
                    ]
                },
            }

        elif method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            result = self.handle_tool_call(tool_name, tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, ensure_ascii=False),
                        }
                    ]
                },
            }

        elif method == "notifications/initialized":
            return None  # 无需回复

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
