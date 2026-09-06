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
import random
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

from evolvmem.config import Config
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
# Resolve once per hook process; keep individual paths replaceable by callers.
_DATA_DIR = Config().data_dir
_LLM_CONFIG_PATH = _DATA_DIR / "llm_credentials.json"
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
_HOOKS_LOG_PATH = _DATA_DIR / "hooks.log"
_HOOKS_LOG_MAX_BYTES = 1024 * 1024
_LIVE_DIR = _DATA_DIR / "live"


class ContextOverflowError(RuntimeError):
    """The extraction request exceeded the model context window."""


class RetryableExtractionError(RuntimeError):
    """Extraction did not complete and must remain pending for a later run."""

    def __init__(self, message: str, *, rate_limited: bool = False,
                 halt_run: bool = False):
        super().__init__(message)
        self.rate_limited = rate_limited
        # 认证/欠费等提供商硬故障：同批其余会话不必再试，整轮停止
        self.halt_run = halt_run


@dataclass(frozen=True)
class ExtractionResult:
    """Outcome consumed by both the live hook and stale-session worker."""

    status: str
    persisted: int = 0
    reason: str = ""
    rate_limited: bool = False
    halt_run: bool = False


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
    try:  # 落盘失败不影响 hook 本身
        if (_HOOKS_LOG_PATH.exists()
                and _HOOKS_LOG_PATH.stat().st_size > _HOOKS_LOG_MAX_BYTES):
            _HOOKS_LOG_PATH.replace(
                _HOOKS_LOG_PATH.with_name("hooks.log.1"))
        with _HOOKS_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} [evolvmem] {msg}\n")
    except Exception:
        pass


# ---- session-start ----

def session_start(payload: dict | None = None) -> None:
    """Print the three-layer injection block to stdout (CLI appends it to context)."""
    from evolvmem.hooks import get_session_start_block
    payload = payload or {}
    workspace_path = str(
        payload.get("cwd") or payload.get("workspace_path") or ""
    ).strip()
    sys.stdout.write(get_session_start_block(workspace_path=workspace_path))


# ---- session-end ----

def _load_llm_config(*, log_errors: bool = True) -> LLMConfig | None:
    try:
        data = json.loads(_LLM_CONFIG_PATH.read_text(encoding="utf-8"))
        provider = str(data.get("provider", "deepseek")).strip().casefold()
        if provider not in _PROVIDER_DEFAULTS:
            if log_errors:
                _log(f"unsupported extraction provider: {provider!r}")
            return None
        api_key = str(data.get("api_key", "")).strip()
        if not api_key:
            if log_errors:
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
        if log_errors:
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
            if error.code in (401, 402, 403):
                raise RetryableExtractionError(
                    f"{llm_config.provider} HTTP {error.code} "
                    "credentials/quota unavailable",
                    halt_run=True,
                ) from error
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
            # 指数退避 + 抖动；优先服从 provider 的 Retry-After
            backoff = min(2.0 ** (attempt + 1), 30.0) + random.uniform(0.0, 1.0)
            retry_after = error.headers.get("Retry-After") if error.headers else None
            try:
                delay = float(retry_after) if retry_after is not None else backoff
            except ValueError:
                delay = backoff
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


def _llm_callable(llm_config: LLMConfig):
    """Adapt the existing provider call to ``callable(prompt) -> str | None``."""
    def llm_call(prompt: str) -> str | None:
        try:
            return _call_llm_with_retry(prompt, llm_config)
        except Exception:
            return None

    return llm_call


def _load_llm_callable(*, log_errors: bool = True):
    """Load configured provider credentials and return the narrow adapter."""
    llm_config = (
        _load_llm_config()
        if log_errors
        else _load_llm_config(log_errors=False)
    )
    return _llm_callable(llm_config) if llm_config is not None else None


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
        summaries = []
        for chunk in _chunk_messages(messages, fallback_chunk_chars):
            summary, atomic = _split_summary_candidate(extract(chunk))
            candidates.extend(atomic)
            summaries.append(summary)
        if len(summaries) == 1:
            return candidates + summaries
        synthesis_messages = [
            {
                "role": "user",
                "content": f"分块摘要 {index}：{summary.value}",
            }
            for index, summary in enumerate(summaries, start=1)
        ]
        combined, _ = _split_summary_candidate(extract(synthesis_messages))
        return candidates + [combined]


def _summary_value_is_persistable(config, value: str) -> bool:
    """Apply the summary-specific deterministic value bounds."""
    from evolvmem.mcp_server import _is_low_info

    stripped = value.strip()
    return (
        config.value_min_chars <= len(stripped) <= config.value_max_chars
        and not _is_low_info(stripped)
    )


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


def _archive_raw_session(config, service, project: str,
                         source_session: str,
                         messages: list[dict[str, str]]) -> int | None:
    """Encrypt the raw messages into the local session archive; None on failure.

    The archive is local encrypted evidence and never leaves the machine; it
    is not a precondition for extraction — without an encryption backend or
    on any write failure the hook logs content-free and persists unlinked.
    Re-archiving the same session replaces the payload instead of adding rows.
    """
    try:
        from evolvmem.session_archive import SessionArchiver

        payload = json.dumps({"messages": messages}, ensure_ascii=False)
        record = SessionArchiver(config, service.store).archive_session(
            project, "kimi", source_session, payload
        )
    except Exception:
        _log("session archive failed; extraction continues without source links")
        return None
    return record.id if record is not None else None


def _run_consolidation_best_effort(service, mode, llm_config,
                                   engine) -> None:
    """One promotion + playbook pass after a successful persist; never fails.

    Only shadow/primary modes serve the lifecycle APIs. The existing LLM
    chat capability is wrapped as the narrow ``callable(prompt) -> str | None``
    the playbook generator expects; provider failures degrade to None, and
    any consolidation failure is logged content-free and skipped.
    """
    from evolvmem.context_models import ContextMode

    if mode not in (ContextMode.SHADOW, ContextMode.PRIMARY):
        return
    if llm_config is None:
        return

    try:
        service.run_consolidation(
            llm=_llm_callable(llm_config), embedding_engine=engine
        )
    except Exception as error:
        _log(f"consolidation skipped: {type(error).__name__}")


def session_end(payload: dict) -> ExtractionResult:
    """Distill the closed session into memories via the extractor + live gate."""
    from evolvmem.config import Config

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
            "retry", reason=str(e), rate_limited=e.rate_limited,
            halt_run=e.halt_run,
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
    # 摘要 TTL：到期后仅当被滚动摘要覆盖才归档（覆盖门控见 summary_retention）；
    # 日期精度沿用 legacy_projection 的 date-only 填充约定（写入侧补 " 00:00:00"）
    summary_expires_at = time.strftime(
        "%Y-%m-%d",
        time.localtime(
            summary_time + config.context_session_summary_ttl_days * 86400
        ),
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
                experience_case=candidate.experience_case,
            )
            for candidate in ranked
        ]
    except Exception as error:
        _log(
            "extraction deferred: candidate policy failed: "
            f"{type(error).__name__}"
        )
        return ExtractionResult("retry", reason="candidate policy failed")

    # embedding 引擎先就绪：服务在事务内用共享 legacy 索引做跨 key 语义合并；
    # 加载失败则传 None，退化为纯 SQLite 写入（不阻塞持久化）
    engine = None
    try:
        from evolvmem.embedding import EmbeddingEngine
        eng = EmbeddingEngine(config)
        eng.initialize()
        if eng.is_loaded:
            engine = eng
    except Exception as e:
        _log(f"embedding init failed, semantic merge/vector sync skipped: {e}")
        engine = None

    from evolvmem.context_models import ContextMode, parse_context_mode
    from evolvmem.context_service import ContextService
    from evolvmem.legacy_models import (
        LegacyExtractionItem,
        LegacyExtractionRequest,
    )

    service = None
    try:
        service = ContextService(config, embedding_engine=engine)
        # 未知 context_mode 按 legacy 落库：提炼正常持久化，Context 功能 fail-closed
        mode = parse_context_mode(config.context_mode)
        resolved_mode = mode if mode is not None else ContextMode.LEGACY
        service.initialize(mode=resolved_mode, adapter="kimi")
        # 原始消息先落加密归档（本地证据，绝不外发）；归档不是提炼的前置，
        # 失败只记无正文日志，提炼以无来源链接的旧行为继续
        archive_id = _archive_raw_session(
            config, service, project, source_session, messages
        )
        extraction = service.persist_legacy_extraction(
            LegacyExtractionRequest(
                summary=LegacyExtractionItem(
                    key=summary.key,
                    value=summary.value,
                    attribute=summary.attribute,
                    tags=tuple(summary.tags),
                    importance=summary.importance,
                    tier=summary.tier,
                    confidence=summary.confidence,
                    expires_at=summary_expires_at,
                ),
                candidates=tuple(
                    LegacyExtractionItem(
                        key=candidate.key,
                        value=candidate.value,
                        attribute=candidate.attribute,
                        tags=tuple(candidate.tags),
                        importance=candidate.importance,
                        tier=candidate.tier,
                        confidence=candidate.confidence,
                        experience_case=candidate.experience_case,
                    )
                    for candidate in ranked
                ),
                max_writes=_MAX_MEMORIES_PER_SESSION,
                source_session=source_session,
            ),
            source_archive_id=archive_id,
            llm=_llm_callable(llm_config),
        )
        atomic_ids = [m.legacy_id for m in extraction.candidates]
        n = extraction.persisted
        # 提炼落库成功后用既有 LLM chat 能力跑一次晋升+playbook 评估；
        # 仅 shadow/primary 提供服务面，任何失败都不改变提炼结果
        _run_consolidation_best_effort(
            service, resolved_mode, llm_config, engine
        )
    except Exception as error:
        _log(f"persistence failed: {type(error).__name__}")
        return ExtractionResult("retry", reason="persistence failed")
    finally:
        if service is not None:
            service.close()
        elif engine is not None:
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


# ---- heartbeat ----

def _touch_heartbeat(session_id: str) -> None:
    """Mark a session as alive; the stale-session worker skips live sessions."""
    safe = re.sub(r"[^\w-]", "_", str(session_id)).strip("_")
    if not safe:
        return
    try:
        _LIVE_DIR.mkdir(parents=True, exist_ok=True)
        (_LIVE_DIR / safe).touch()
    except Exception:
        pass  # fail-open：心跳失败不影响会话


def _payload_session_id(raw: str) -> str:
    try:
        return str(json.loads(raw).get("session_id", ""))
    except Exception:
        return ""


# ---- entry ----

def main() -> None:
    sub = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if sub == "session-start":
            raw = sys.stdin.read()
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except Exception:
                payload = {}
            _touch_heartbeat(str(payload.get("session_id", "")))
            session_start(payload)
        elif sub == "session-end":
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
            session_end(payload)
        elif sub == "heartbeat":
            _touch_heartbeat(_payload_session_id(sys.stdin.read()))
        else:
            _log(f"unknown subcommand: {sub!r}")
    except Exception as e:  # fail-open: hook errors must never block a session
        _log(f"hook error ({sub}): {e}")
    sys.exit(0)


if __name__ == "__main__":
    main()
