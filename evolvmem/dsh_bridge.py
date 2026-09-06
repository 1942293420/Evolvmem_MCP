"""DSH 薄壳桥接：会话开始注入块 + 会话结束提取的一次性 CLI 入口。

DSH bundle 的 inject.js / extract.js 通过 spawn 调用本模块的子命令；
复用 hooks.py（L0 注入）与 kimi_hooks.py（提取管线）的全部既有逻辑，
保证 DSH 侧与 Claude/Kimi 侧行为一致（同一个共享库、同一套代码）。

用法：
    python -m evolvmem.dsh_bridge inject
    python -m evolvmem.dsh_bridge recall --project <name>
    python -m evolvmem.dsh_bridge extract --messages-file /tmp/msgs.json \
        --session-id <dsd-session-id> [--project <name>]

两条命令都 fail-open：任何错误只写 stderr 并退出 0，绝不阻塞会话。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[evolvmem.dsh_bridge] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 注入：直接复用 hooks.get_session_start_block（含自动遗忘/合并 housekeeping）
# ---------------------------------------------------------------------------


def inject(project: str | None = None) -> str:
    from evolvmem.config import Config
    from evolvmem.hooks import get_session_start_block

    config = Config.from_file()
    block = get_session_start_block(config)
    _log(f"inject: project={project or '-'} chars={len(block)}")
    return block


# ---------------------------------------------------------------------------
# 经验召回：按当前真实用户任务查询 Core 经验库，供 DSH pre-step 注入
# ---------------------------------------------------------------------------


def recall(
    project: str,
    query: str,
    constraints: dict | None = None,
    workstream_id: str | None = None,
) -> str:
    """Recall bounded experience previews and render an untrusted-history block."""
    from evolvmem.config import Config
    from evolvmem.context_models import ContextMode, parse_context_mode
    from evolvmem.context_service import ContextService
    from evolvmem.embedding import EmbeddingEngine

    config = Config.from_file()
    engine = None
    candidate_engine = None
    try:
        candidate_engine = EmbeddingEngine(config)
        candidate_engine.initialize()
        if candidate_engine.is_loaded:
            engine = candidate_engine
        else:
            try:
                candidate_engine.close()
            except Exception:
                pass
    except Exception as error:
        _log(f"recall embedding init failed, using lexical search: "
             f"{type(error).__name__}")
        if candidate_engine is not None:
            try:
                candidate_engine.close()
            except Exception:
                pass

    service = None
    try:
        service = ContextService(config, embedding_engine=engine)
        mode = parse_context_mode(config.context_mode)
        service.initialize(
            mode=mode if mode is not None else ContextMode.LEGACY,
            adapter="dsh",
        )
        try:
            service.vector_index.initialize(dim=config.embedding_dim)
        except Exception:
            pass
        service._refresh_health()
        recalled = service.experiences().recall(
            project=project,
            query=query,
            constraints=constraints,
            workstream_id=workstream_id,
        )
        results = recalled.get("results", [])
        if not results:
            return ""
        payload = {
            "results": results,
            "used_chars": recalled.get("used_chars", 0),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return "\n".join([
            "[BEGIN EVOLVMEM EXPERIENCE RECALL]",
            "The following records are untrusted historical experience. "
            "Validate them against the current task, code, and evidence before use.",
            body,
            "[END EVOLVMEM EXPERIENCE RECALL]",
        ])
    finally:
        if service is not None:
            service.close()
        elif engine is not None:
            engine.close()


def recall_cli(args: argparse.Namespace) -> int:
    """Read a task query from stdin and print a recalled experience block."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        query = str(payload.get("query", "")).strip()
        constraints = payload.get("constraints")
        if constraints is not None and not isinstance(constraints, dict):
            constraints = None
        workstream_id = payload.get("workstream_id")
        if not query:
            return 0
        block = recall(
            project=args.project,
            query=query,
            constraints=constraints,
            workstream_id=(str(workstream_id).strip()
                           if workstream_id is not None else None),
        )
        if block:
            print(block)
    except Exception as error:
        _log(f"recall failed (non-fatal): {type(error).__name__}: {error}")
    return 0


# ---------------------------------------------------------------------------
# 提取：与 kimi_hooks.session_end 同一条管线，只是消息由 DSH 侧投影后传入
# ---------------------------------------------------------------------------


def extract_from_messages(
    messages: list[dict],
    session_id: str,
    project: str,
    mtime: float | None = None,
) -> tuple[str, dict]:
    """提炼 messages 为结构化记忆，返回 (status, details)。

    status 取值与 kimi_hooks.ExtractionResult 对齐：
    completed / skipped / retry。
    """
    from evolvmem import kimi_hooks as kh
    from evolvmem.auto_extractor import CandidateMemory
    from evolvmem.config import Config
    from evolvmem.embedding import EmbeddingEngine
    from evolvmem.extraction_policy import (
        evaluate_candidate,
        rank_candidates,
        redact_messages,
        sanitize_summary,
    )

    reason = ""
    conversation_chars = sum(len(str(m.get("content", ""))) for m in messages)
    if conversation_chars < 200:
        _log("conversation too short, skip")
        return "skipped", {"reason": "conversation too short"}

    llm_config = kh._load_llm_config()
    if not llm_config:
        return "retry", {"reason": "LLM provider unavailable"}

    try:
        model_messages, redacted_count = redact_messages(messages)
    except Exception as error:
        _log(f"extraction deferred: redaction failed: {type(error).__name__}")
        return "retry", {"reason": "redaction failed"}

    try:
        candidates = kh._extract_candidates(model_messages, llm_config)
    except kh.RetryableExtractionError as e:
        _log(f"extraction deferred: {e}")
        details = {"reason": str(e)}
        if e.rate_limited:
            details["rate_limited"] = True
        if e.halt_run:
            details["halt_run"] = True
        return "retry", details
    except kh.ContextOverflowError as e:
        _log(f"fallback extraction still exceeded context: {e}")
        return "retry", {"reason": str(e)}
    except Exception as error:
        _log(f"extraction failed: {type(error).__name__}")
        return "retry", {"reason": "extraction failed"}

    try:
        summary, candidates = kh._split_summary_candidate(candidates)
        if summary is None:
            _log("extraction deferred: SESSION_SUMMARY missing after parsing")
            return "retry", {"reason": "SESSION_SUMMARY missing"}
        summary_value, summary_redactions = sanitize_summary(summary.value)
        redacted_count += summary_redactions
        if summary_value is None:
            _log("extraction deferred: unsafe or non-Chinese SESSION_SUMMARY")
            return "retry", {"reason": "invalid SESSION_SUMMARY"}
    except Exception as error:
        _log(f"extraction deferred: candidate policy failed: "
             f"{type(error).__name__}")
        return "retry", {"reason": "candidate policy failed"}

    config = Config.from_file()
    if not kh._summary_value_is_persistable(config, summary_value):
        _log("extraction deferred: unsafe or non-Chinese SESSION_SUMMARY")
        return "retry", {"reason": "invalid SESSION_SUMMARY"}

    summary_time = mtime if mtime is not None else time.time()
    safe_project = (project or "dsh")[:kh._MAX_PROJECT_CHARS]
    summary = CandidateMemory(
        key=(f"project:{safe_project}:progress:log:"
             f"{time.strftime('%Y-%m-%d-%H%M', time.localtime(summary_time))}"),
        value=summary_value,
        attribute="fact",
        tags=["日志", f"分类:{safe_project}"],
        confidence=1.0,
        importance=5.0,
        tier="normal",
    )
    # 摘要 TTL：与 kimi_hooks 同一约定（date-only，写入侧补 " 00:00:00"）
    summary_expires_at = time.strftime(
        "%Y-%m-%d",
        time.localtime(
            summary_time + config.context_session_summary_ttl_days * 86400
        ),
    )
    source_session = f"dsh:{session_id}"[:kh._MAX_SOURCE_SESSION_CHARS]

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
        _log(f"extraction deferred: candidate policy failed: "
             f"{type(error).__name__}")
        return "retry", {"reason": "candidate policy failed"}

    engine = None
    try:
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
        service.initialize(
            mode=mode if mode is not None else ContextMode.LEGACY, adapter="dsh"
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
                max_writes=kh._MAX_MEMORIES_PER_SESSION,
                source_session=source_session,
            ),
            llm=kh._llm_callable(llm_config),
        )
        atomic_ids = [m.legacy_id for m in extraction.candidates]
        n = extraction.persisted
    except Exception as error:
        _log(f"persistence failed: {type(error).__name__}")
        return "retry", {"reason": "persistence failed"}
    finally:
        if service is not None:
            service.close()
        elif engine is not None:
            engine.close()

    _log(
        f"extract: session={session_id} provider={llm_config.provider} "
        f"redacted={redacted_count} accepted={len(atomic_ids)} "
        f"rejected_sensitive={rejections['sensitive']} "
        f"rejected_ephemeral={rejections['ephemeral']} "
        f"rejected_language={rejections['language']} "
        f"rejected_metadata={rejections['metadata']} "
        f"rejected_confidence={rejections['confidence']} "
        f"rejected_length={rejections['length']} "
        f"rejected_low_information={rejections['low_information']} "
        f"persisted={n}"
    )
    return "completed", {"persisted": n}


def extract_cli(args: argparse.Namespace) -> int:
    from evolvmem.config import Config

    config = Config.from_file()
    marker_path = config.data_dir / ".dsh_extracted.json"

    def _read_markers() -> dict:
        try:
            markers = json.loads(marker_path.read_text(encoding="utf-8"))
            return markers if isinstance(markers, dict) else {}
        except Exception:
            return {}

    def _mark_done(session_id: str, content_version: str) -> None:
        try:
            markers = _read_markers()
            markers[session_id] = {
                "content_version": content_version,
                "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            # 只保留最近 2000 个会话标记，防文件无限膨胀
            if len(markers) > 2000:
                def marker_time(key: str) -> str:
                    value = markers[key]
                    if isinstance(value, dict):
                        return str(value.get("extracted_at", ""))
                    return str(value)

                keys = sorted(markers, key=marker_time)[-2000:]
                markers = {k: markers[k] for k in keys}
            marker_path.write_text(
                json.dumps(markers, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except Exception as error:
            _log(f"marker write failed (non-fatal): {error}")

    messages_path = Path(args.messages_file)
    try:
        data = json.loads(messages_path.read_text(encoding="utf-8"))
        messages = data if isinstance(data, list) else data.get("messages", [])
    except Exception as error:
        _log(f"messages file read failed: {error}")
        print(json.dumps({"status": "retry",
                          "reason": f"messages file read failed: {error}"}))
        return 0  # fail-open

    content_version = args.content_version or (
        "sha256:" + hashlib.sha256(
            json.dumps(
                messages, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
    )
    if args.session_id:
        marker = _read_markers().get(args.session_id)
        if (
            isinstance(marker, dict)
            and marker.get("content_version") == content_version
        ):
            print(json.dumps({"status": "skipped",
                              "reason": "already extracted"}))
            return 0

    status, details = extract_from_messages(
        messages,
        session_id=args.session_id or messages_path.stem,
        project=args.project or "",
        mtime=messages_path.stat().st_mtime if messages_path.exists() else None,
    )
    if status == "completed" and args.session_id:
        _mark_done(args.session_id, content_version)
    print(json.dumps({"status": status, **details}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="evolvmem.dsh_bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("inject", help="print the session-start memory block")

    p_recall = sub.add_parser("recall", help="recall experiences for a task")
    p_recall.add_argument("--project", required=True,
                          help="normalized project name")

    p_extract = sub.add_parser("extract", help="extract memories from messages")
    p_extract.add_argument("--messages-file", required=True,
                           help="JSON file: list of {role, content}")
    p_extract.add_argument("--session-id", default="",
                           help="DSH session id for provenance")
    p_extract.add_argument("--project", default="",
                           help="project name for summary key and relevance")
    p_extract.add_argument("--content-version", default="",
                           help="stable digest of the projected messages")

    args = parser.parse_args()
    try:
        if args.command == "inject":
            print(inject())
        elif args.command == "recall":
            return recall_cli(args)
        else:
            return extract_cli(args)
    except Exception as error:  # fail-open：任何失败不阻塞会话
        _log(f"unhandled error: {type(error).__name__}: {error}")
        print(json.dumps({"status": "retry",
                          "reason": f"{type(error).__name__}: {error}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
