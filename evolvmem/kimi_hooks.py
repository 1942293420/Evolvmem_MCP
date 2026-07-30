"""Kimi Code CLI hooks — SessionStart injection + SessionEnd auto-extraction.

Configured in ~/.kimi-code/config.toml as [[hooks]] entries. Both subcommands
are fail-open: any error only prints to stderr and exits 0, so a hook failure
can never block a session.

Usage:
    PYTHONPATH=. python -m evolvmem.kimi_hooks session-start
    PYTHONPATH=. python -m evolvmem.kimi_hooks session-end   # payload on stdin
"""

import glob
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

_KIMI_API = "https://api.kimi.com/coding/v1/chat/completions"
_CRED_PATH = Path.home() / ".kimi-code" / "credentials" / "kimi-code.json"
_SESSIONS_DIR = Path.home() / ".kimi-code" / "sessions"
_MODEL = "kimi-for-coding"
_MAX_CONVERSATION_CHARS = 12000
_MAX_MEMORIES_PER_SESSION = 8
_TOKEN_GRACE_S = 60


def _log(msg: str) -> None:
    print(f"[evolvmem] {msg}", file=sys.stderr, flush=True)


# ---- session-start ----

def session_start() -> None:
    """Print the three-layer injection block to stdout (CLI appends it to context)."""
    from evolvmem.hooks import get_session_start_block
    sys.stdout.write(get_session_start_block())


# ---- session-end ----

def _load_token() -> str | None:
    try:
        data = json.loads(_CRED_PATH.read_text(encoding="utf-8"))
        expires_at = data.get("expires_at") or 0
        if time.time() > expires_at - _TOKEN_GRACE_S:
            _log("access token expired or expiring soon, skip extraction")
            return None
        return data.get("access_token")
    except Exception as e:
        _log(f"credential read failed: {e}")
        return None


def _find_wire(session_id: str) -> str | None:
    """Locate main wire.jsonl for a session id (with or without session_ prefix)."""
    candidates = [session_id]
    if not session_id.startswith("session_"):
        candidates.append(f"session_{session_id}")
    for sid in candidates:
        matches = glob.glob(
            str(_SESSIONS_DIR / "*" / sid / "agents" / "main" / "wire.jsonl"))
        if matches:
            return matches[0]
    return None


def _read_conversation(wire_path: str) -> str:
    texts: list[str] = []
    with open(wire_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "turn.prompt":
                for part in ev.get("input", []):
                    if isinstance(part, dict) and part.get("type") == "text":
                        texts.append(f"[user]: {part['text']}")
            elif ev.get("type") == "context.append_loop_event":
                e = ev.get("event", {})
                part = e.get("part", {}) if isinstance(e, dict) else {}
                if e.get("type") == "content.part" and part.get("type") == "text":
                    texts.append(f"[assistant]: {part['text']}")
    return "\n".join(texts)[-_MAX_CONVERSATION_CHARS:]


def _call_llm(prompt: str, token: str) -> str:
    body = json.dumps({
        "model": _MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
    }).encode("utf-8")
    req = urllib.request.Request(
        _KIMI_API, data=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _persist_candidates(config, store, vidx, engine, candidates, session_id: str) -> int:
    """Persist extraction candidates: gate checks → conflict detection → write.

    The add branch (no same-key conflict) also checks for a semantically
    identical active memory: a hit supersedes the old record instead of
    coexisting as a fragmented duplicate. vidx/engine may be None (embedding
    unavailable) — semantic merge and vector sync are then skipped.
    Returns the number of memories persisted.
    """
    from evolvmem.auto_extractor import AutoExtractor
    from evolvmem.conflict_detector import ConflictDetector
    from evolvmem.mcp_server import _is_low_info
    from evolvmem.semantic_merge import find_semantic_match

    extractor = AutoExtractor()
    detector = ConflictDetector(store)
    added_ids: list[int] = []
    for c in candidates:
        if len(added_ids) >= _MAX_MEMORIES_PER_SESSION:
            break
        if not extractor.should_persist(c):
            continue
        value = c.value.strip()
        if not (config.value_min_chars <= len(value) <= config.value_max_chars):
            continue
        if _is_low_info(value):
            continue
        decision = detector.check(c.key, value)
        if decision.action == "skip":
            continue
        if decision.action == "replace":
            new_id = store.replace(key=c.key, new_value=value,
                                   importance=c.importance, tier=c.tier)
        else:
            # decision.action == "add": 同 key 无冲突 → 再做跨 key 语义合并
            if engine is not None and getattr(engine, "is_loaded", False):
                match = find_semantic_match(store, vidx, engine, value,
                                            config.add_merge_threshold)
                if match:
                    added_ids.append(store.replace(
                        key=match["key"], new_value=value,
                        importance=c.importance, tier=c.tier))
                    continue
            new_id = store.add(key=c.key, value=value, attribute=c.attribute,
                               tags=c.tags, importance=c.importance, tier=c.tier,
                               source_session=session_id)
        added_ids.append(new_id)
    # 向量索引同步（失败不阻塞，MCP 启动时一致性校验会兜底）
    if added_ids and vidx is not None and engine is not None \
            and getattr(engine, "is_loaded", False):
        try:
            import numpy as np
            for mid in added_ids:
                rec = store.get_by_id(mid)
                if rec:
                    vidx.add(mid, np.array(
                        engine.encode_document(rec["value"]),
                        dtype=np.float32))
            vidx.save()
        except Exception as e:
            _log(f"vector sync skipped: {e}")
    return len(added_ids)


def session_end(payload: dict) -> None:
    """Distill the closed session into memories via the extractor + live gate."""
    from evolvmem.auto_extractor import AutoExtractor
    from evolvmem.config import Config
    from evolvmem.memory_store import MemoryStore

    session_id = payload.get("session_id", "")
    wire = _find_wire(session_id)
    if not wire:
        _log(f"wire.jsonl not found for {session_id}, skip")
        return
    conversation = _read_conversation(wire)
    if len(conversation) < 200:
        _log("conversation too short, skip")
        return

    token = _load_token()
    if not token:
        return

    extractor = AutoExtractor()
    prompt = extractor.build_extraction_prompt(
        [{"role": "user", "content": conversation}])
    raw = _call_llm(prompt, token)
    candidates = extractor.parse_response(raw)
    if not candidates:
        _log("nothing worth persisting")
        return

    config = Config.from_file()
    # embedding/向量索引先就绪：persist 循环内要做跨 key 语义合并和向量同步；
    # 加载失败则传 None，退化为纯 SQLite 写入（不阻塞持久化）
    engine = None
    vidx = None
    try:
        from evolvmem.embedding import EmbeddingEngine
        from evolvmem.vector_index import VectorIndex
        eng = EmbeddingEngine(config)
        eng.initialize()
        if eng.is_loaded:
            engine = eng
            vidx = VectorIndex(config)
            vidx.initialize(dim=config.embedding_dim)
    except Exception as e:
        _log(f"embedding init failed, semantic merge/vector sync skipped: {e}")
        engine, vidx = None, None

    with MemoryStore(config) as store:
        n = _persist_candidates(config, store, vidx, engine, candidates, session_id)
    if vidx is not None:
        vidx.close()
    if engine is not None:
        engine.close()
    _log(f"session-end extraction: {n} memories persisted from {session_id}")


# ---- entry ----

def main() -> None:
    sub = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if sub == "session-start":
            session_start()
        elif sub == "session-end":
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
            session_end(payload)
        else:
            _log(f"unknown subcommand: {sub!r}")
    except Exception as e:  # fail-open: hook errors must never block a session
        _log(f"hook error ({sub}): {e}")
    sys.exit(0)


if __name__ == "__main__":
    main()
