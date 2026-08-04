"""Kimi Code CLI hooks — SessionStart injection + SessionEnd auto-extraction.

Configured in ~/.kimi-code/config.toml as [[hooks]] entries. Both subcommands
are fail-open: any error only prints to stderr and exits 0, so a hook failure
can never block a session.

Usage:
    PYTHONPATH=. python -m evolvmem.kimi_hooks session-start
    PYTHONPATH=. python -m evolvmem.kimi_hooks session-end   # payload on stdin
"""

import glob
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from pathlib import Path

from evolvmem.extraction_policy import (
    contains_sensitive_text,
    evaluate_candidate,
    rank_candidates,
    redact_messages,
    sanitize_summary,
)

_KIMI_API = "https://api.kimi.com/coding/v1/chat/completions"
_SESSIONS_DIR = Path.home() / ".kimi-code" / "sessions"
_MODEL = "kimi-for-coding"
_LLM_CONFIG_PATH = (Path.home() / ".claude" / "evolvmem"
                    / "llm_credentials.json")
_PROVIDER_DEFAULTS = {
    "deepseek": (
        "https://api.deepseek.com/chat/completions",
        "deepseek-v4-flash",
    ),
    "kimi": (_KIMI_API, _MODEL),
}
_FALLBACK_CHUNK_CHARS = 120000
_EXTRACTION_BUDGET_S = 240
_REQUEST_TIMEOUT_S = 120.0
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_ERROR_BODY_BYTES = 64 * 1024
_MAX_MEMORIES_PER_SESSION = 8
_MAX_PROJECT_CHARS = 48
_MAX_SOURCE_SESSION_CHARS = 128
_SESSION_SUMMARY_KEY = "SESSION_SUMMARY"
_WD_DIR_RE = re.compile(r"^wd_(.+)_[0-9a-f]{8,}$")


class ContextOverflowError(RuntimeError):
    """The extraction request exceeded the model context window."""


class RetryableExtractionError(RuntimeError):
    """Extraction did not complete and must remain pending for a later run."""

    def __init__(self, message: str, *, rate_limited: bool = False):
        super().__init__(message)
        self.rate_limited = rate_limited


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome consumed by both the live hook and stale-session worker."""

    status: str
    persisted: int = 0
    reason: str = ""
    rate_limited: bool = False


@dataclass(frozen=True)
class LLMConfig:
    """Credentials and endpoint for the session extraction provider."""

    provider: str
    api_key: str
    base_url: str
    model: str


def _url_origin(url: str) -> tuple[str, str, int] | None:
    """Return a normalized network origin, or ``None`` for an unsafe URL."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").casefold()
        if scheme not in {"http", "https"} or not host:
            return None
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow only same-origin redirects so bearer auth cannot escape."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        source_origin = _url_origin(req.full_url)
        target_origin = _url_origin(newurl)
        if (
            source_origin is None
            or target_origin is None
            or source_origin != target_origin
            or (source_origin[0] == "https" and target_origin[0] != "https")
        ):
            raise HTTPError(
                req.full_url,
                code,
                "unsafe redirect blocked",
                headers,
                fp,
            )
        redirected = super().redirect_request(
            req, fp, code, msg, headers, newurl
        )
        authorization = req.get_header("Authorization")
        if authorization and redirected is not None:
            redirected.add_header("Authorization", authorization)
        return redirected


def _log(msg: str) -> None:
    print(f"[evolvmem] {msg}", file=sys.stderr, flush=True)


# ---- session-start ----

def session_start() -> None:
    """Print the three-layer injection block to stdout (CLI appends it to context)."""
    from evolvmem.hooks import get_session_start_block
    sys.stdout.write(get_session_start_block())


# ---- session-end ----

def _load_llm_config() -> LLMConfig | None:
    try:
        data = json.loads(_LLM_CONFIG_PATH.read_text(encoding="utf-8"))
        provider = str(data.get("provider", "deepseek")).strip().casefold()
        if provider not in _PROVIDER_DEFAULTS:
            _log(f"unsupported extraction provider: {provider!r}")
            return None
        api_key = str(data.get("api_key", "")).strip()
        if not api_key:
            _log(f"{_LLM_CONFIG_PATH} has no api_key, skip extraction")
            return None
        default_url, default_model = _PROVIDER_DEFAULTS[provider]
        return LLMConfig(
            provider=provider,
            api_key=api_key,
            base_url=str(data.get("base_url") or default_url).strip(),
            model=str(data.get("model") or default_model).strip(),
        )
    except Exception as e:
        _log(f"LLM credential read failed: {e}")
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


def _project_from_wire(wire_path: str, aliases: dict) -> str:
    """Infer the project key segment from the session working-directory name.

    The sessions directory entry looks like 'wd_<cwd-basename>_<hex>';
    the basename is mapped through inject_project_aliases (directory name
    → key segment) and sanitized. Falls back to 'general'.
    """
    dirname = ""
    for part in Path(wire_path).parts:
        m = _WD_DIR_RE.match(part)
        if m:
            dirname = m.group(1)
            break
    segment = aliases.get(dirname, dirname).lower() if dirname else ""
    if contains_sensitive_text(segment):
        return "general"
    segment = re.sub(r"_+", "_", re.sub(r"[^\w一-鿿-]", "_", segment)).strip("_")
    segment = segment[:_MAX_PROJECT_CHARS]
    return segment or "general"


def _canonical_session_id(session_id: str, wire_path: str) -> str:
    """Return a bounded ``session_*`` source identifier without raw metadata."""
    wire_session = ""
    try:
        candidate = Path(wire_path).parents[2].name
        if candidate.startswith("session_"):
            wire_session = candidate
    except IndexError:
        pass
    raw = wire_session or str(session_id)
    if contains_sensitive_text(raw):
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"session_{digest}"
    if not raw.startswith("session_"):
        raw = f"session_{raw}"
    if any(ord(character) < 32 for character in raw):
        raw = ""
    raw = re.sub(r"[^\w-]", "_", raw)
    raw = re.sub(r"_+", "_", raw).strip("_")
    if not raw.startswith("session_"):
        digest = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16]
        raw = f"session_{digest}"
    return raw[:_MAX_SOURCE_SESSION_CHARS]


def _split_summary_candidate(candidates: list) -> tuple:
    """Pull out the SESSION_SUMMARY entry (at most one) from LLM candidates."""
    for i, c in enumerate(candidates):
        if c.key.strip().upper() == _SESSION_SUMMARY_KEY:
            return c, candidates[:i] + candidates[i + 1:]
    return None, candidates


def _read_messages(wire_path: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    with open(wire_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "turn.prompt":
                for part in ev.get("input", []):
                    if isinstance(part, dict) and part.get("type") == "text":
                        messages.append({
                            "role": "user", "content": part["text"],
                        })
            elif ev.get("type") == "context.append_loop_event":
                e = ev.get("event", {})
                part = e.get("part", {}) if isinstance(e, dict) else {}
                if e.get("type") == "content.part" and part.get("type") == "text":
                    messages.append({
                        "role": "assistant", "content": part["text"],
                    })
    return messages


def _read_conversation(wire_path: str) -> str:
    """Backward-compatible rendered conversation for diagnostics."""
    return "\n".join(
        f"[{message['role']}]: {message['content']}"
        for message in _read_messages(wire_path)
    )


def _call_llm(
    prompt: str,
    llm_config: LLMConfig,
    *,
    deadline: float | None = None,
) -> str:
    request_body = {
        "model": llm_config.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
    }
    if llm_config.provider == "deepseek":
        request_body.update({
            "temperature": 0.2,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        })
    body = json.dumps(request_body).encode("utf-8")
    req = urllib.request.Request(
        llm_config.base_url, data=body,
        headers={"Authorization": f"Bearer {llm_config.api_key}",
                 "Content-Type": "application/json"})
    if deadline is None:
        deadline = time.monotonic() + _REQUEST_TIMEOUT_S
        timeout = _REQUEST_TIMEOUT_S
    else:
        timeout = min(_REQUEST_TIMEOUT_S, deadline - time.monotonic())
        if timeout <= 0:
            raise RetryableExtractionError(
                "extraction retry budget exhausted"
            )
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read(_MAX_RESPONSE_BYTES + 1)
        if time.monotonic() >= deadline:
            raise RetryableExtractionError(
                "extraction retry budget exhausted"
            )
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise RetryableExtractionError(
                "provider response body exceeded limit"
            )
        data = json.loads(raw.decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _http_error_body(
    error: HTTPError,
    *,
    deadline: float | None = None,
) -> str:
    try:
        if deadline is not None and time.monotonic() >= deadline:
            raise RetryableExtractionError(
                "extraction retry budget exhausted"
            )
        raw = error.read(_MAX_ERROR_BODY_BYTES)
        if deadline is not None and time.monotonic() >= deadline:
            raise RetryableExtractionError(
                "extraction retry budget exhausted"
            )
        return raw.decode("utf-8", errors="replace").casefold()
    except RetryableExtractionError:
        raise
    except Exception:
        return ""


def _call_llm_with_retry(prompt: str, llm_config: LLMConfig,
                         deadline: float | None = None) -> str:
    """Retry transient provider failures within one extraction budget."""
    max_attempts = 3
    if deadline is None:
        deadline = time.monotonic() + _EXTRACTION_BUDGET_S
    for attempt in range(max_attempts):
        if time.monotonic() >= deadline:
            raise RetryableExtractionError("extraction retry budget exhausted")
        try:
            content = _call_llm(prompt, llm_config, deadline=deadline)
            if time.monotonic() >= deadline:
                raise RetryableExtractionError(
                    "extraction retry budget exhausted"
                )
            return content
        except HTTPError as error:
            body = _http_error_body(error, deadline=deadline)
            if error.code in (400, 422) and any(marker in body for marker in (
                "context_length_exceeded", "maximum context length",
                "context too long", "context window",
            )):
                raise ContextOverflowError(
                    f"{llm_config.provider} context window exceeded"
                ) from error
            rate_limited = error.code == 429
            if error.code not in (408, 429, 500, 502, 503, 504):
                raise RetryableExtractionError(
                    f"{llm_config.provider} HTTP {error.code}; "
                    "retry after credentials/service recover",
                    rate_limited=rate_limited,
                ) from error
            if attempt == max_attempts - 1:
                raise RetryableExtractionError(
                    f"{llm_config.provider} HTTP {error.code} retries exhausted",
                    rate_limited=rate_limited,
                ) from error
            retry_after = error.headers.get("Retry-After") if error.headers else None
            try:
                delay = float(retry_after) if retry_after is not None else (2, 5)[attempt]
            except ValueError:
                delay = (2, 5)[attempt]
        except (TimeoutError, socket.timeout, URLError) as error:
            # A second blocking read timeout can already consume the hook's
            # 240-second budget, so timeout-like failures get one retry only.
            if attempt >= 1:
                raise RetryableExtractionError(
                    f"{llm_config.provider} network retries exhausted"
                ) from error
            delay = 2.0

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RetryableExtractionError("extraction retry budget exhausted")
        time.sleep(max(0.0, min(delay, 30.0, remaining)))
    raise AssertionError("unreachable")


def _chunk_messages(messages: list[dict[str, str]],
                    max_chars: int) -> list[list[dict[str, str]]]:
    """Pack whole messages into fallback chunks without splitting content."""
    chunks: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    current_chars = 0
    for message in messages:
        message_chars = (len(message.get("role", ""))
                         + len(message.get("content", "")) + 4)
        if current and current_chars + message_chars > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(message)
        current_chars += message_chars
    if current:
        chunks.append(current)
    return chunks


def _keep_latest_summary(candidates: list) -> list:
    """Keep atomic candidates plus only the latest chunk's session summary."""
    summaries = [
        c for c in candidates
        if c.key.strip().upper() == _SESSION_SUMMARY_KEY
    ]
    atomic = [
        c for c in candidates
        if c.key.strip().upper() != _SESSION_SUMMARY_KEY
    ]
    return atomic + summaries[-1:]


def _extract_candidates(messages: list[dict[str, str]],
                        llm_config: LLMConfig,
                        fallback_chunk_chars: int = _FALLBACK_CHUNK_CHARS
                        ) -> list:
    """Extract the full conversation once; chunk only on context overflow."""
    from evolvmem.auto_extractor import AutoExtractor

    extractor = AutoExtractor()
    deadline = time.monotonic() + _EXTRACTION_BUDGET_S

    def extract(batch: list[dict[str, str]]) -> list:
        prompt = extractor.build_extraction_prompt(batch)
        candidates = extractor.parse_response(
            _call_llm_with_retry(prompt, llm_config, deadline=deadline)
        )
        if not any(
            c.key.strip().upper() == _SESSION_SUMMARY_KEY
            for c in candidates
        ):
            raise RetryableExtractionError(
                f"{llm_config.provider} extraction response omitted "
                "SESSION_SUMMARY"
            )
        return candidates

    try:
        return _keep_latest_summary(extract(messages))
    except ContextOverflowError:
        candidates = []
        for chunk in _chunk_messages(messages, fallback_chunk_chars):
            candidates.extend(extract(chunk))
        return _keep_latest_summary(candidates)


def _persist_candidates(
        config, store, vidx, engine, candidates, session_id: str,
        *, max_writes: int | None = None,
) -> list[int]:
    """Persist extraction candidates: gate checks → conflict detection → write.

    The add branch (no same-key conflict) also checks for a semantically
    identical active memory: a hit supersedes the old record instead of
    coexisting as a fragmented duplicate. vidx/engine may be None (embedding
    unavailable) — semantic merge is then skipped. Returns the IDs written to
    SQLite; vector synchronization is deliberately handled after commit.
    """
    from evolvmem.conflict_detector import ConflictDetector
    from evolvmem.semantic_merge import find_semantic_match

    detector = ConflictDetector(store)
    added_ids: list[int] = []
    for c in candidates:
        if max_writes is not None and len(added_ids) >= max_writes:
            break
        value = c.value.strip()
        decision = detector.check(c.key, value)
        if decision.action == "skip":
            continue
        if decision.action == "replace":
            new_id = store.replace(key=c.key, new_value=value,
                                   importance=c.importance, tier=c.tier,
                                   source_session=session_id)
        else:
            # 同 key 无冲突或 conflict → 再做跨 key 语义合并
            # （tier == "reference" 的候选不参与合并：永不 supersede 别人，
            #   与 mcp_server._memory_add 的守卫一致）
            if (engine is not None and getattr(engine, "is_loaded", False)
                    and c.tier != "reference"):
                match = find_semantic_match(store, vidx, engine, value,
                                            config.add_merge_threshold)
                if match:
                    # 合并目标是 pinned 记忆时保留 pinned tier，避免被候选的
                    # 默认 "normal" 静默降级、掉出每会话必注入层
                    merged_tier = "pinned" if match.get("tier") == "pinned" else c.tier
                    added_ids.append(store.replace(
                        key=match["key"], new_value=value,
                        importance=c.importance, tier=merged_tier,
                        source_session=session_id))
                    continue
            new_id = store.add(key=c.key, value=value, attribute=c.attribute,
                               tags=c.tags, importance=c.importance, tier=c.tier,
                               source_session=session_id)
        added_ids.append(new_id)
    return added_ids


def _summary_value_is_persistable(config, value: str) -> bool:
    """Apply the summary-specific deterministic value bounds."""
    from evolvmem.mcp_server import _is_low_info

    stripped = value.strip()
    return (
        config.value_min_chars <= len(stripped) <= config.value_max_chars
        and not _is_low_info(stripped)
    )


def _persist_summary(store, summary, session_id: str) -> tuple[list[int], bool]:
    """Write a locally constructed summary or accept an existing equivalent."""
    active = next(
        (
            record
            for record in store.get_by_key(summary.key)
            if record["status"] == "active"
        ),
        None,
    )
    if active is not None:
        metadata_equivalent = (
            active["attribute"] == summary.attribute
            and active["tags"] == ",".join(summary.tags)
            and active["importance"] == summary.importance
            and active["tier"] == summary.tier
        )
        if (
            active["value"].strip() == summary.value.strip()
            and metadata_equivalent
        ):
            return [], True
    metadata = {
        "attribute": summary.attribute,
        "tags": summary.tags,
        "importance": summary.importance,
        "tier": summary.tier,
        "source_session": session_id,
    }
    if active is None:
        memory_id = store.add(summary.key, summary.value.strip(), **metadata)
    else:
        memory_id = store.replace(
            summary.key,
            summary.value.strip(),
            **metadata,
        )
    return [memory_id], True


def _sync_candidate_vectors(store, vidx, engine,
                            memory_ids: list[int]) -> None:
    """Best-effort vector sync for an already committed SQLite batch."""
    if not memory_ids or vidx is None or engine is None:
        return
    if not getattr(engine, "is_loaded", False):
        return
    try:
        import numpy as np
        mark_dirty = getattr(vidx, "mark_dirty", None)
        if callable(mark_dirty):
            mark_dirty()
        for memory_id in memory_ids:
            record = store.get_by_id(memory_id)
            if record:
                embedding = engine.encode_document(record["value"])
                vidx.add(
                    memory_id,
                    np.array(embedding, dtype=np.float32),
                )
        vidx.save()
    except Exception as error:
        preserve_dirty = getattr(vidx, "preserve_dirty", None)
        if callable(preserve_dirty):
            preserve_dirty()
        _log(f"vector sync skipped: {type(error).__name__}")


def session_end(payload: dict) -> ExtractionResult:
    """Distill the closed session into memories via the extractor + live gate."""
    from evolvmem.config import Config
    from evolvmem.memory_store import MemoryStore

    session_id = payload.get("session_id", "")
    wire = _find_wire(session_id)
    if not wire:
        _log(f"wire.jsonl not found for {session_id}, skip")
        return ExtractionResult("retry", reason="wire.jsonl not found")
    try:
        messages = _read_messages(wire)
    except Exception as e:
        _log(f"conversation read failed: {e}")
        return ExtractionResult("retry", reason=f"conversation read failed: {e}")
    conversation_chars = sum(len(m.get("content", "")) for m in messages)
    if conversation_chars < 200:
        _log("conversation too short, skip")
        return ExtractionResult("skipped", reason="conversation too short")

    llm_config = _load_llm_config()
    if not llm_config:
        return ExtractionResult("retry", reason="LLM provider unavailable")

    try:
        model_messages, redacted_count = redact_messages(messages)
    except Exception as error:
        _log(f"extraction deferred: redaction failed: {type(error).__name__}")
        return ExtractionResult("retry", reason="redaction failed")

    try:
        candidates = _extract_candidates(model_messages, llm_config)
    except RetryableExtractionError as e:
        _log(f"extraction deferred: {e}")
        return ExtractionResult(
            "retry", reason=str(e), rate_limited=e.rate_limited
        )
    except ContextOverflowError as e:
        _log(f"fallback extraction still exceeded context: {e}")
        return ExtractionResult("retry", reason=str(e))
    except Exception as error:
        _log(f"extraction failed: {type(error).__name__}")
        return ExtractionResult("retry", reason="extraction failed")

    # 会话摘要单独拆出：key 规范为 project:{项目}:progress:log:{日期-时分}，
    # 持久化时单独放行，不占 _MAX_MEMORIES_PER_SESSION 配额
    try:
        summary, candidates = _split_summary_candidate(candidates)
        if summary is None:
            _log("extraction deferred: SESSION_SUMMARY missing after parsing")
            return ExtractionResult("retry", reason="SESSION_SUMMARY missing")
        summary_value, summary_redactions = sanitize_summary(summary.value)
        redacted_count += summary_redactions
        if summary_value is None:
            _log("extraction deferred: unsafe or non-Chinese SESSION_SUMMARY")
            return ExtractionResult("retry", reason="invalid SESSION_SUMMARY")
    except Exception as error:
        _log(
            "extraction deferred: candidate policy failed: "
            f"{type(error).__name__}"
        )
        return ExtractionResult("retry", reason="candidate policy failed")

    config = Config.from_file()
    if not _summary_value_is_persistable(config, summary_value):
        _log("extraction deferred: unsafe or non-Chinese SESSION_SUMMARY")
        return ExtractionResult("retry", reason="invalid SESSION_SUMMARY")
    project = _project_from_wire(wire, config.inject_project_aliases)
    try:
        summary_time = os.path.getmtime(wire)
    except OSError:
        summary_time = time.time()
    from evolvmem.auto_extractor import CandidateMemory

    summary = CandidateMemory(
        key=(f"project:{project}:progress:log:"
             f"{time.strftime('%Y-%m-%d-%H%M', time.localtime(summary_time))}"),
        value=summary_value,
        attribute="fact",
        tags=["日志", f"分类:{project}"],
        confidence=1.0,
        importance=5.0,
        tier="normal",
    )
    source_session = _canonical_session_id(session_id, wire)

    try:
        rejections: Counter[str] = Counter()
        eligible = []
        for candidate in candidates:
            decision = evaluate_candidate(
                candidate,
                value_min_chars=config.value_min_chars,
                value_max_chars=config.value_max_chars,
            )
            if decision.accepted:
                eligible.append(candidate)
            else:
                rejections[decision.reason] += 1
        ranked = rank_candidates(eligible, limit=None)
        ranked = [
            CandidateMemory(
                key=candidate.key.casefold(),
                value=candidate.value.strip(),
                attribute=candidate.attribute,
                tags=list(candidate.tags),
                confidence=candidate.confidence,
                importance=candidate.importance,
                tier=candidate.tier,
            )
            for candidate in ranked
        ]
    except Exception as error:
        _log(
            "extraction deferred: candidate policy failed: "
            f"{type(error).__name__}"
        )
        return ExtractionResult("retry", reason="candidate policy failed")

    # embedding/向量索引先就绪：事务内只读索引做跨 key 语义合并；
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

    try:
        with MemoryStore(config) as store:
            with store.transaction():
                summary_ids, summary_satisfied = _persist_summary(
                    store, summary, source_session,
                )
                if not summary_satisfied:
                    raise RuntimeError("SESSION_SUMMARY was not persisted")
                atomic_ids = _persist_candidates(
                    config,
                    store,
                    vidx,
                    engine,
                    ranked,
                    source_session,
                    max_writes=_MAX_MEMORIES_PER_SESSION,
                )
                memory_ids = [*summary_ids, *atomic_ids]
            _sync_candidate_vectors(store, vidx, engine, memory_ids)
            n = len(memory_ids)
    except Exception as error:
        _log(f"persistence failed: {type(error).__name__}")
        return ExtractionResult("retry", reason="persistence failed")
    finally:
        if vidx is not None:
            vidx.close()
        if engine is not None:
            engine.close()
    _log(
        f"provider={llm_config.provider} redacted={redacted_count} "
        f"accepted={len(atomic_ids)} "
        f"rejected_sensitive={rejections['sensitive']} "
        f"rejected_ephemeral={rejections['ephemeral']} "
        f"rejected_language={rejections['language']} "
        f"rejected_metadata={rejections['metadata']} "
        f"rejected_confidence={rejections['confidence']} "
        f"rejected_length={rejections['length']} "
        f"rejected_low_information={rejections['low_information']} "
        f"persisted={n}"
    )
    return ExtractionResult("completed", persisted=n)


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
