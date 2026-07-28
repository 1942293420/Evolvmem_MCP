#!/usr/bin/env python3
"""EvolvMem memory plugin — stdio MCP Server.

Tools:
  memory_search   — FTS5/trigram + HNSW hybrid search
  memory_status   — statistics
  memory_add      — manually add a memory
  memory_replace  — replace a memory (mark old as superseded)
  memory_remove   — soft-delete a memory
"""

import json
import math
import sys
import os
import traceback
from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex
from evolvmem.embedding import EmbeddingEngine
from evolvmem.retriever import Retriever
from evolvmem.conflict_detector import ConflictDetector
from evolvmem.forgetting import ForgettingEngine


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

    def initialize(self):
        """Initialize all components."""
        self.store.initialize()
        self.vidx.initialize(dim=self.config.embedding_dim)

        # Check USearch vs SQLite consistency
        sqlite_count = len(self.store.all_ids())
        if not self.vidx.check_consistency(sqlite_count):
            self._rebuild_vector_index()

        # Try loading the embedding model (FTS5 search works without it)
        try:
            self.engine.initialize()
        except (FileNotFoundError, ImportError) as e:
            self._log(f"Embedding engine not loaded: {e}")

        self.retriever = Retriever(
            self.config, self.store, self.vidx, self.engine
        )
        self.conflict_detector = ConflictDetector(self.store)
        self.forgetting = ForgettingEngine(self.config, self.store)

    def shutdown(self):
        """Clean up resources."""
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
            return {"error": "key and value parameters cannot be empty"}

        if len(value) > self.config.value_max_chars:
            return {"error": f"value too long ({len(value)} > {self.config.value_max_chars} chars); "
                             "split or summarize before adding"}

        importance = args.get("importance")
        if importance is not None:
            importance = float(importance)
            if math.isnan(importance):  # min(10.0, nan) 返回 10.0，置 None 走默认路径
                importance = None
        if importance is not None:
            importance = max(1.0, min(10.0, importance))
        tier = args.get("tier")
        if tier not in ("pinned", "normal"):
            tier = None
        extra = {}
        if importance is not None:
            extra["importance"] = importance
        if tier is not None:
            extra["tier"] = tier

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
            # decision.action == "add": no existing key, insert directly
            mem_id = self.store.add(
                key=key, value=value, category=category, tags=tags, **extra
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

    def run(self):
        """stdio MCP main loop."""
        self._log("MCP Server starting")
        try:
            self.initialize()
        except Exception as e:
            self._log(f"Initialization failed: {e}")
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
                self._log(f"Invalid JSON: {line[:100]}")
                continue
            try:
                response = self._handle_request(request)
                if response is not None:
                    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                    sys.stdout.flush()
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
                                        "description": "Memory content",
                                    },
                                    "category": {
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
                                        "enum": ["pinned", "normal"],
                                        "description": "pinned = injected every session; normal = scored competition",
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
                                        "description": "New memory content",
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
                    ]
                },
            }

        elif method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
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
