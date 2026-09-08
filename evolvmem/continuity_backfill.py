"""Codex 中断会话的本地有界增量补录（无需外部模型）。

扫描 Codex 持久 rollout JSONL（session_meta、user 指令、function_call 与
function_call_output 等实际结构），为意外中断且没有结构化 checkpoint 的
会话生成保守的未完成任务断点：

  - 助手自称"完成"不是执行证据：只有带回结果的工具调用计入
    completed_steps；没有结果的工具调用一律记为"待核实"（blockers）。
    静默会话不等于任务完成。
  - 逐来源版本幂等：每个会话的任务 id 由 session id 确定性派生，状态文件
    记录字节偏移与已写入的 checkpoint revision；重复扫描不产生重复任务，
    追加日志从偏移处增量解析，尾部半行留给下一轮。
  - 保守边界：不覆盖更新的人工 checkpoint（revision 高于已写入版本即
    人工证据）、不复活终态任务、不改变任何项目 focus；路径/凭据形内容
    复用现有内容边界（extraction_policy + checkpoint 内容策略） sanitise
    后才允许入库。
  - 归属：apply 需要显式 --project 与 --workspace-path；session_meta 的
    cwd 能确定且不在该工作区内的会话只作为候选报告，绝不猜写到项目。
  - dry-run 是默认；--apply 才写库。每轮最多处理 MAX_PER_RUN 个会话，
    mtime 新于 idle 阈值的活跃会话跳过。

用法（crontab，每小时一次，复用现有 cron 模式，不建常驻服务）：
  # 自动模式：按 session cwd 指纹匹配现有 active binding，唯一确定才补录
  41 * * * * /usr/bin/flock -n ~/.claude/evolvmem/.continuity_backfill.lock \
    /path/to/evolvmem-plugin/.venv/bin/python -m evolvmem.continuity_backfill \
    --auto --apply >> ~/.claude/evolvmem/continuity_backfill.log 2>&1
  # 限定模式：只补录显式声明的项目/工作区（如指定的历史会话）
  41 * * * * /usr/bin/flock -n ~/.claude/evolvmem/.continuity_backfill.lock \
    /path/to/evolvmem-plugin/.venv/bin/python -m evolvmem.continuity_backfill \
    --project "权限审批" --workspace-path /path/to/workspace --apply \
    >> ~/.claude/evolvmem/continuity_backfill.log 2>&1

CLI 输出每会话一行 JSON，只含 id/动作/计数/稳定原因码，绝不输出会话
正文、绝对路径或 traceback。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from evolvmem.config import Config
from evolvmem.context_store import ContextStore
from evolvmem.continuity_models import (
    ContinuityError,
    ContinuityImportRequest,
    _TERMINAL_STATUSES,
)
from evolvmem.continuity_service import ContinuityService, _normalize_objective
from evolvmem.experience_sources import _CODEX_FILENAME
from evolvmem.extraction_policy import contains_sensitive_text
from evolvmem.project_store import ProjectStore, ProjectStoreError
from evolvmem.workspace_identity import (
    WorkspaceIdentityError,
    WorkspaceIdentityProvider,
)

VERSION = "continuity-backfill.v1"

MAX_PER_RUN = 5          # 每轮最多补录几个会话（新的优先）
IDLE_MINUTES = 30        # mtime 新于该阈值视为活跃会话，本轮跳过
MAX_LINE_BYTES = 1024 * 1024
MAX_BYTES_PER_SCAN = 8 * 1024 * 1024  # 单文件单轮读取字节预算（真正有限读取）
MAX_EVENTS_PER_SCAN = 2000  # 单文件单轮最多消费的事件数（剩余下轮继续）
MAX_USERS = 50           # 状态内保留的用户消息条数（首条 + 最近若干）
MAX_CALLS = 500          # 状态内保留的工具调用数（先逐出最旧已答）
MAX_FIELD_CHARS = 400    # objective 等单字段 sanitise 后上限
MAX_STEP_CHARS = 120     # 单条步骤/待核实文本上限
MAX_STEPS = 8            # completed/pending 各保留的最近条数
_REDACTED = "（已按内容边界省略）"
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# 补录动作分类（稳定码，CLI 与测试共用一份词表）
ACTIONS = frozenset(
    {
        "create",
        "update",
        "unchanged",
        "existing",
        "no_new_events",
        "active",
        "candidate",
        "skipped",
        "terminal_kept",
        "human_checkpoint_kept",
    }
)


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """单会话补录结论；content-free，可安全打印。"""

    session_id: str
    action: str
    reason: str = ""
    workstream_id: str = ""
    completed: int = 0
    pending: int = 0


@dataclass(slots=True)
class _ParsedSession:
    session_id: str
    cwd: str
    users: list  # [(lineno, text)]，相邻镜像对已去重
    calls: dict  # call_id -> {"name", "lineno", "hint", "answered", "failed"}
    last_lineno: int
    excluded: str = ""  # subagent_session / mixed_workspace 等排除原因码
    confirmed_id: str = ""  # continuity_checkpoint 工具结果确认的 workstream id


def deterministic_workstream_id(session_id: str) -> str:
    """ws_ + sha256(session)[:16]：同一来源会话永远映射到同一任务。"""
    digest = hashlib.sha256(
        f"codex-backfill:{session_id}".encode("utf-8")
    ).hexdigest()
    return f"ws_{digest[:16]}"


def _sanitize(text: str, maximum: int) -> str:
    """去掉路径形 token、折叠空白、截断；命中敏感内容则整条省略。"""
    if contains_sensitive_text(text):
        return _REDACTED
    tokens = [
        token
        for token in text.split()
        if not token.startswith("/") and not token.startswith("~/")
    ]
    cleaned = " ".join(tokens).strip()
    if len(cleaned) > maximum:
        cleaned = cleaned[: maximum - 1].rstrip() + "…"
    return cleaned or _REDACTED


def _sanitize_tool_name(name: object) -> str:
    text = str(name or "").strip()
    return _TOOL_NAME_RE.sub("_", text)[:40] or "tool"


def _read_increment(path: Path, offset: int, lineno: int, in_oversized: bool):
    """从字节偏移增量读取，受 MAX_BYTES_PER_SCAN 字节预算约束。

    readline 带上限，绝不为超长行分配整行；超长行按块丢弃并跨扫描持久化
    in_oversized 状态，续扫不重读前缀。行号随扫描累积（含被丢弃的超长
    行），调用方把 lineno/in_oversized 与 offset 一起持久化。普通尾部半行
    不消费，留给追加后的下一轮。

    返回 (new_offset, lineno, in_oversized, [(lineno, event)])。
    """
    new_offset = offset
    events = []
    bytes_read = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            if bytes_read >= MAX_BYTES_PER_SCAN:
                break
            if len(events) >= MAX_EVENTS_PER_SCAN:
                break
            raw = handle.readline(MAX_LINE_BYTES + 2)
            if not raw:
                break  # EOF
            bytes_read += len(raw)
            if in_oversized:
                # 丢弃超长行的后续块；遇到行尾才完成该行
                new_offset = handle.tell()
                if raw.endswith(b"\n"):
                    in_oversized = False
                    lineno += 1
                continue
            if not raw.endswith(b"\n"):
                if len(raw) <= MAX_LINE_BYTES:
                    break  # 普通尾部半行：不消费
                # 超长行的首个块：进入跨块丢弃状态
                in_oversized = True
                new_offset = handle.tell()
                continue
            new_offset = handle.tell()
            lineno += 1
            if len(raw) > MAX_LINE_BYTES:
                continue  # 刚好被限长读全的超长行：跳过内容，推进行号
            try:
                event = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append((lineno, event))
    return new_offset, lineno, in_oversized, events


def _user_text(event: dict) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    if event.get("type") == "response_item" and (
        payload.get("type") == "message" and payload.get("role") == "user"
    ):
        parts = []
        for node in payload.get("content") or ():
            if isinstance(node, dict) and isinstance(node.get("text"), str):
                parts.append(node["text"])
            elif isinstance(node, str):
                parts.append(node)
        return "\n".join(parts) if parts else None
    if event.get("type") == "event_msg" and payload.get("type") == "user_message":
        message = payload.get("message")
        return message if isinstance(message, str) else None
    return None


def _is_injected_context(text: str) -> bool:
    """系统注入（environment_context / AGENTS.md）不是用户目标。"""
    if "<environment_context>" in text:
        return True
    return text.lstrip().startswith("# AGENTS.md")


def _output_failed(payload: dict) -> bool:
    """工具返回了结果不等于成功：error/exit_code 非零即返回失败。"""
    if payload.get("success") is False:
        return True
    output = payload.get("output")
    data = output
    if isinstance(output, str):
        try:
            data = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            data = None
    if not isinstance(data, dict):
        return False
    containers = [data]
    if isinstance(data.get("metadata"), dict):
        containers.append(data["metadata"])
    for container in containers:
        code = container.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool) and code != 0:
            return True
    return bool(data.get("error"))


def _arg_hint(payload: dict) -> str:
    """从工具参数提取可理解的短提示（command/首个字符串值），sanitise。"""
    raw = payload.get("arguments") or payload.get("input")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    value = None
    for key in ("command", "cmd"):
        if key in data:
            value = data[key]
            break
    if value is None:
        for candidate in data.values():
            if isinstance(candidate, str):
                value = candidate
                break
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    if not isinstance(value, str) or not value.strip():
        return ""
    hint = _sanitize(value, 40)
    return "" if hint == _REDACTED else hint


def _same_workspace(old: str, new: str) -> bool:
    try:
        old_path, new_path = Path(old).resolve(), Path(new).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return new_path == old_path or old_path in new_path.parents


_WS_ID_RE = re.compile(r"ws_[0-9a-f]{16}\Z")
_CHECKPOINT_CALL_MARK = "continuity_checkpoint"


def _extract_workstream_id(value, depth: int = 0) -> str:
    """从 continuity_checkpoint 工具结果的嵌套 JSON 中提取 workstream id。

    只接受实际工具返回结构里的 ws_  id（MCP content[].text 内嵌 JSON），
    绝不从助手自述或任意文本推断。
    """
    if depth > 5:
        return ""
    if isinstance(value, dict):
        candidate = value.get("workstream_id")
        if isinstance(candidate, str) and _WS_ID_RE.fullmatch(candidate):
            return candidate
        for child in value.values():
            found = _extract_workstream_id(child, depth + 1)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = _extract_workstream_id(child, depth + 1)
            if found:
                return found
    elif isinstance(value, str) and "workstream_id" in value:
        try:
            data = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return ""
        return _extract_workstream_id(data, depth + 1)
    return ""


def _is_checkpoint_call(payload: dict) -> bool:
    """该调用是否是 continuity_checkpoint（名称或 exec 输入均可识别）。"""
    name = payload.get("name")
    if isinstance(name, str) and _CHECKPOINT_CALL_MARK in name:
        return True
    for key in ("input", "arguments"):
        raw = payload.get(key)
        if isinstance(raw, str) and _CHECKPOINT_CALL_MARK in raw:
            return True
    return False


def _parse_events(path: Path, seed: _ParsedSession, events: list) -> None:
    """把增量事件并入会话状态（跨扫描累积，偏移持久化在状态文件）。"""
    for lineno, event in events:
        seed.last_lineno = max(seed.last_lineno, lineno)
        payload = event.get("payload")
        if event.get("type") == "session_meta" and isinstance(payload, dict):
            if isinstance(payload.get("id"), str) and payload["id"]:
                seed.session_id = payload["id"]
            if isinstance(payload.get("cwd"), str):
                seed.cwd = payload["cwd"]
            source = payload.get("source")
            if payload.get("parent_thread_id") or (
                isinstance(source, dict) and "subagent" in source
            ):
                # 审查器/子代理内部会话：不是新的用户任务
                seed.excluded = "subagent_session"
            continue
        if event.get("type") == "turn_context" and isinstance(payload, dict):
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                if seed.cwd and not _same_workspace(seed.cwd, cwd):
                    # 中途切换到别的工作区：混合来源，只能候选
                    seed.excluded = "mixed_workspace"
                elif not seed.cwd:
                    seed.cwd = cwd
            continue
        text = _user_text(event)
        if text is not None:
            if _is_injected_context(text):
                continue
            # 入库/落状态前 sanitise：原始用户文本（可能含凭据/路径）
            # 绝不进入状态文件或断点
            cleaned = _sanitize(text, MAX_FIELD_CHARS)
            # Codex response_item/event_msg 相邻镜像对只保留一条
            if not seed.users or seed.users[-1][1] != cleaned:
                seed.users.append((lineno, cleaned))
                if len(seed.users) > MAX_USERS:
                    seed.users = seed.users[:1] + seed.users[-(MAX_USERS - 1):]
            continue
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind in ("function_call", "custom_tool_call"):
            call_id = payload.get("call_id") or payload.get("id")
            if isinstance(call_id, str) and call_id:
                if len(seed.calls) >= MAX_CALLS and call_id not in seed.calls:
                    # 有界 state：先逐出最旧的已答调用，其次最旧调用
                    answered = [
                        cid for cid, c in seed.calls.items() if c["answered"]
                    ]
                    victim = min(
                        answered or list(seed.calls),
                        key=lambda cid: seed.calls[cid]["lineno"],
                    )
                    del seed.calls[victim]
                seed.calls[call_id] = {
                    "name": _sanitize_tool_name(payload.get("name")),
                    "lineno": lineno,
                    "hint": _arg_hint(payload),
                    "answered": False,
                    "failed": False,
                    "checkpoint": _is_checkpoint_call(payload),
                }
        elif kind in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id in seed.calls:
                seed.calls[call_id]["answered"] = True
                seed.calls[call_id]["failed"] = _output_failed(payload)
                if seed.calls[call_id].get("checkpoint"):
                    confirmed = _extract_workstream_id(payload.get("output"))
                    if confirmed:
                        seed.confirmed_id = confirmed


def _checkpoint_fields(parsed: _ParsedSession) -> dict:
    """保守断点内容：客观事件序列，绝不从助手自述推断完成。

    只有返回了成功结果的工具调用计入 completed；返回失败（error/
    exit_code 非零）与无结果的调用一律待核实。
    """
    objective = parsed.users[0][1] if parsed.users else ""
    ordered = sorted(parsed.calls.items(), key=lambda item: item[1]["lineno"])

    def hint_of(call: dict) -> str:
        return f"{call['hint']} " if call.get("hint") else ""

    completed = [
        f"{call['name']} {hint_of(call)}已返回结果 (L{call['lineno']})"
        for _call_id, call in ordered
        if call["answered"] and not call.get("failed")
    ][-MAX_STEPS:]
    pending = [
        f"{call['name']} {hint_of(call)}返回失败，待核实 (L{call['lineno']})"
        if call.get("failed")
        else f"{call['name']} {hint_of(call)}调用无结果，待核实 (L{call['lineno']})"
        for _call_id, call in ordered
        if not call["answered"] or call.get("failed")
    ][-MAX_STEPS:]
    return {
        "objective": objective,
        "completed_steps": tuple(completed),
        "current_step": (
            f"来源会话 {parsed.session_id} 中断前最后事件 "
            f"L{parsed.last_lineno}（自动补录，待人工核实）"
        ),
        "next_action": "人工核实待核实项后继续",
        "blockers": tuple(pending),
    }


def _load_state(state_path: Path) -> dict:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, state_path)


def _iter_rollouts(sessions_root: Path):
    try:
        paths = sorted(
            sessions_root.glob("**/rollout-*.jsonl"),
            key=lambda p: (p.stat().st_mtime, str(p)),
            reverse=True,
        )
    except OSError:
        return []
    return [p for p in paths if p.is_file() and _CODEX_FILENAME.fullmatch(p.name)]


def _attribution_reason(parsed: _ParsedSession, workspace: Path) -> str:
    """可确定的 cwd 不在声明工作区内（或缺失）→ 只能当候选。"""
    if not parsed.cwd:
        return "cwd_unknown"
    try:
        cwd = Path(parsed.cwd).resolve()
        root = workspace.resolve()
    except (OSError, RuntimeError, ValueError):
        return "cwd_unknown"
    if cwd == root or root in cwd.parents:
        return ""
    return "cwd_mismatch"


def _auto_attribution(
    parsed: _ParsedSession,
    service: ContinuityService,
    identity_provider: WorkspaceIdentityProvider,
) -> tuple[str, str, str]:
    """自动模式归属：session cwd 指纹唯一匹配现有 active binding 才返回
    (project, workspace, "")，否则返回 ("", "", 原因码) 只作候选。

    不匹配、多项目无唯一默认、通用 home、cwd 已不存在都绝不猜项目。
    """
    if not parsed.cwd:
        return "", "", "cwd_unknown"
    try:
        resolved = Path(parsed.cwd).resolve()
    except (OSError, RuntimeError, ValueError):
        return "", "", "cwd_unavailable"
    if resolved == Path.home():
        return "", "", "generic_workspace"
    try:
        fingerprint = identity_provider.resolve(parsed.cwd).fingerprint
    except WorkspaceIdentityError:
        return "", "", "cwd_unavailable"
    project = service._resolve_project(fingerprint, "")
    if project is None:
        return "", "", "no_unique_binding"
    return project, parsed.cwd, ""


def _objective_matched_workstream(
    service: ContinuityService,
    workspace_path: str,
    project: str,
    objective: str,
    *,
    exclude_id: str,
):
    """同项目/工作区内规范化目标一致的既有任务（人工结构化任务优先）。

    返回行或 None；仅用于“复用而非另造平行任务”，绝不写入。
    """
    if not objective or objective == _REDACTED:
        return None
    try:
        fingerprint = service._resolve_workspace(workspace_path).fingerprint
    except ContinuityError:
        return None
    wanted = _normalize_objective(objective)
    if not wanted:
        return None
    rows = service._store._connection().execute(
        "SELECT * FROM continuity_workstreams "
        "WHERE project=? AND workspace_fingerprint=? "
        "ORDER BY updated_at DESC, id LIMIT ?",
        (project, fingerprint, 50),
    ).fetchall()
    for row in rows:
        if row["id"] == exclude_id:
            continue
        previous = service._load_previous_content(int(row["current_context_id"]))
        if _normalize_objective(previous.get("objective", "")) == wanted:
            return row
    return None


def run_backfill(
    store: ContextStore,
    identity_provider: WorkspaceIdentityProvider,
    *,
    sessions_root,
    project: str,
    workspace_path: str,
    state_path,
    apply: bool = False,
    max_per_run: int = MAX_PER_RUN,
    idle_minutes: int = IDLE_MINUTES,
    now: float | None = None,
) -> list[BackfillReport]:
    """有界增量扫描一轮；dry-run 默认，apply 时经 ContinuityService 写入。

    store 必须已 initialize（schema 就绪）；identity_provider 对应
    data_dir 的 workspace.key。两种归属模式：

    - 限定模式（project + workspace_path 非空）：调用方显式声明归属，
      apply 前幂等登记并绑定（method=backfill）。
    - 自动模式（两者皆空）：按 session_meta.cwd 的指纹匹配现有 active
      binding；唯一确定项目且非通用 home 才 apply，不匹配/多项目/混合
      会话只候选，绝不登记新项目。
    """
    if not isinstance(store, ContextStore):
        raise TypeError("store must be a ContextStore instance")
    now = time.time() if now is None else now
    sessions_root = Path(sessions_root)
    state_path = Path(state_path)
    scoped = bool(project.strip() and workspace_path.strip())
    workspace = Path(workspace_path) if workspace_path.strip() else None
    state = _load_state(state_path)
    service = ContinuityService(store.config, store, identity_provider)

    if apply and scoped:
        fingerprint = identity_provider.resolve(str(workspace)).fingerprint
        with store.transaction():
            project_store = ProjectStore(
                store._connection(),
                store._require_transaction,
                generic_names=(),
            )
            row = store._connection().execute(
                "SELECT status FROM context_project_registry WHERE project=?",
                (project,),
            ).fetchone()
            if row is not None and row["status"] != "active":
                raise ContinuityError("project_archived")
            project_store.register_project(project)
            try:
                project_store.bind_workspace(
                    fingerprint, project, method="backfill", make_default=True
                )
            except ProjectStoreError as exc:
                if exc.code != "default_binding_conflict":
                    raise
                project_store.bind_workspace(
                    fingerprint, project, method="backfill", make_default=False
                )

    reports: list[BackfillReport] = []
    processed = 0
    for path in _iter_rollouts(sessions_root):
        if processed >= max_per_run:
            break
        try:
            stat = path.stat()
        except OSError:
            continue
        fallback_id = _CODEX_FILENAME.fullmatch(path.name).group(1)
        saved = state.get(fallback_id) or {}
        if saved and stat.st_size < int(saved.get("offset", 0)):
            saved = {}  # 日志被截断/轮换：从头重建该会话状态
        if now - stat.st_mtime < idle_minutes * 60:
            reports.append(BackfillReport(fallback_id, "active"))
            continue
        if (
            saved.get("applied_revision", 0) > 0
            and saved.get("offset", 0) >= stat.st_size
        ):
            reports.append(BackfillReport(fallback_id, "no_new_events"))
            continue
        parsed = _ParsedSession(
            fallback_id,
            str(saved.get("cwd", "")),
            [],
            dict(saved.get("calls", {})),
            int(saved.get("lineno", 0)),
        )
        # 跨扫描累积：session id、users、排除来源、确认关联、行号与超长行
        # 状态全部从状态文件恢复，增量绝不丢失来源身份
        parsed.session_id = str(saved.get("session_id", fallback_id))
        parsed.users = [tuple(item) for item in saved.get("users", [])]
        parsed.excluded = str(saved.get("excluded", ""))
        parsed.confirmed_id = str(saved.get("confirmed_id", ""))
        try:
            new_offset, new_lineno, in_oversized, events = _read_increment(
                path,
                int(saved.get("offset", 0)),
                parsed.last_lineno,
                bool(saved.get("in_oversized", False)),
            )
        except OSError:
            reports.append(BackfillReport(fallback_id, "skipped", "unreadable"))
            continue
        parsed.last_lineno = new_lineno
        _parse_events(path, parsed, events)
        if not events:
            if apply and new_offset != int(saved.get("offset", 0)):
                # 完整但被跳过的事件（如超长行）也推进偏移，避免每轮重读
                _persist_state(state_path, state, parsed.session_id,
                               fallback_id, saved, new_offset, stat, parsed,
                               in_oversized=in_oversized)
            if saved.get("applied_revision", 0) > 0:
                reports.append(BackfillReport(fallback_id, "no_new_events"))
            else:
                reports.append(BackfillReport(fallback_id, "skipped", "no_events"))
            continue
        session_id = parsed.session_id
        if parsed.excluded:
            if apply:
                _persist_state(state_path, state, session_id, fallback_id,
                               saved, new_offset, stat, parsed,
                               in_oversized=in_oversized)
            if parsed.excluded == "mixed_workspace":
                processed += 1
                reports.append(
                    BackfillReport(session_id, "candidate", parsed.excluded)
                )
            else:
                reports.append(BackfillReport(session_id, "skipped", parsed.excluded))
            continue
        if not parsed.users:
            if apply:
                _persist_state(state_path, state, session_id, fallback_id,
                               saved, new_offset, stat, parsed,
                               in_oversized=in_oversized)
            reports.append(BackfillReport(session_id, "skipped", "no_objective"))
            continue
        if scoped:
            session_project, session_workspace = project, str(workspace)
            reason = _attribution_reason(parsed, workspace)
        else:
            session_project, session_workspace, reason = _auto_attribution(
                parsed, service, identity_provider
            )
        completed = sum(
            1 for c in parsed.calls.values()
            if c["answered"] and not c.get("failed")
        )
        pending = sum(
            1 for c in parsed.calls.values()
            if not c["answered"] or c.get("failed")
        )
        if reason:
            processed += 1
            if apply:
                _persist_state(state_path, state, session_id, fallback_id,
                               saved, new_offset, stat, parsed,
                               in_oversized=in_oversized)
            reports.append(
                BackfillReport(session_id, "candidate", reason,
                               completed=completed, pending=pending)
            )
            continue
        fields = _checkpoint_fields(parsed)
        workstream_id = deterministic_workstream_id(session_id)
        if not apply:
            processed += 1
            reports.append(
                BackfillReport(
                    session_id,
                    "update" if saved.get("applied_revision", 0) > 0 else "create",
                    workstream_id=workstream_id,
                    completed=completed,
                    pending=pending,
                )
            )
            continue
        # continuity_checkpoint 工具结果确认的任务优先复用（核验同属本
        # 项目/工作区且非终态；不从助手自述推断，不覆盖较新人工进度）
        existing = None
        terminal_confirmed = False
        if parsed.confirmed_id:
            row = service._workstream_row(parsed.confirmed_id)
            try:
                confirmed_fp = service._resolve_workspace(
                    session_workspace
                ).fingerprint
            except ContinuityError:
                confirmed_fp = None
            if (
                row is not None
                and confirmed_fp is not None
                and row["project"] == session_project
                and row["workspace_fingerprint"] == confirmed_fp
            ):
                if row["status"] in _TERMINAL_STATUSES:
                    terminal_confirmed = True
                else:
                    existing = row
        if terminal_confirmed:
            processed += 1
            _persist_state(state_path, state, session_id, fallback_id, saved,
                           new_offset, stat, parsed,
                           in_oversized=in_oversized,
                           applied_revision=int(row["checkpoint_revision"]),
                           workstream_id=row["id"])
            reports.append(
                BackfillReport(session_id, "terminal_kept",
                               workstream_id=row["id"],
                               completed=completed, pending=pending)
            )
            continue
        if existing is None:
            # 已有同目标（人工结构化）任务优先复用：不另造每会话平行任务
            existing = _objective_matched_workstream(
                service,
                session_workspace,
                session_project,
                fields["objective"],
                exclude_id=workstream_id,
            )
        if existing is not None:
            processed += 1
            _persist_state(state_path, state, session_id, fallback_id, saved,
                           new_offset, stat, parsed,
                           applied_revision=int(existing["checkpoint_revision"]),
                           workstream_id=existing["id"],
                           in_oversized=in_oversized)
            reports.append(
                BackfillReport(session_id, "existing",
                               workstream_id=existing["id"],
                               completed=completed, pending=pending)
            )
            continue
        try:
            result = service.import_interrupted(
                ContinuityImportRequest(
                    workspace_path=session_workspace,
                    project_hint=session_project,
                    workstream_id=workstream_id,
                    objective=fields["objective"],
                    completed_steps=fields["completed_steps"],
                    current_step=fields["current_step"],
                    next_action=fields["next_action"],
                    blockers=fields["blockers"],
                    applied_revision=int(saved.get("applied_revision", 0)),
                )
            )
        except ContinuityError as exc:
            # 被拒来源不消耗扫描配额，但推进偏移（同一文件版本只试一次）
            if apply:
                _persist_state(state_path, state, session_id, fallback_id,
                               saved, new_offset, stat, parsed,
                               in_oversized=in_oversized)
            reports.append(BackfillReport(session_id, "skipped", exc.code))
            continue
        processed += 1
        action = {
            "created": "create",
            "updated": "update",
            "unchanged": "unchanged",
            "terminal_kept": "terminal_kept",
            "human_checkpoint_kept": "human_checkpoint_kept",
        }[result.code]
        if result.code in ("created", "updated"):
            _persist_state(state_path, state, session_id, fallback_id, saved,
                           new_offset, stat, parsed,
                           in_oversized=in_oversized,
                           applied_revision=result.checkpoint_revision,
                           workstream_id=result.workstream_id)
        else:
            _persist_state(state_path, state, session_id, fallback_id, saved,
                           new_offset, stat, parsed,
                           in_oversized=in_oversized,
                           workstream_id=saved.get("workstream_id", workstream_id))
        reports.append(
            BackfillReport(session_id, action,
                           workstream_id=result.workstream_id,
                           completed=completed, pending=pending)
        )
    return reports


def _persist_state(
    state_path: Path,
    state: dict,
    session_id: str,
    fallback_id: str,
    saved: dict,
    new_offset: int,
    stat,
    parsed: _ParsedSession,
    *,
    applied_revision: int | None = None,
    workstream_id: str = "",
    in_oversized: bool = False,
) -> None:
    """推进单会话偏移；applied_revision 只升不降。

    排除来源、确认的结构化任务关联、行号与超长行丢弃状态一并持久化，
    增量续扫不丢来源身份、不重读前缀。
    """
    entry = {
        "offset": new_offset,
        "mtime": stat.st_mtime,
        "session_id": session_id,
        "cwd": parsed.cwd,
        "users": [list(item) for item in parsed.users],
        "calls": parsed.calls,
        "excluded": parsed.excluded,
        "confirmed_id": parsed.confirmed_id or saved.get("confirmed_id", ""),
        "lineno": parsed.last_lineno,
        "in_oversized": bool(in_oversized),
        "workstream_id": workstream_id or saved.get("workstream_id", ""),
        "applied_revision": max(
            int(saved.get("applied_revision", 0)),
            int(applied_revision or 0),
        ),
        "via": VERSION,
    }
    state[session_id] = entry
    if session_id != fallback_id and fallback_id in state:
        del state[fallback_id]
    _save_state(state_path, state)


# ---- CLI ----


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evolvmem.continuity_backfill",
        description="Codex 中断会话的有界增量保守补录（dry-run 默认）",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Config().data_dir,
        help="evolvmem data directory (default: the configured one)",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path(
            os.environ.get("CODEX_HOME") or Path.home() / ".codex"
        ) / "sessions",
        help="Codex sessions root (default: $CODEX_HOME/sessions or ~/.codex/sessions)",
    )
    parser.add_argument("--project", default="", help="归属项目名（限定模式）")
    parser.add_argument(
        "--workspace-path", default="", help="归属工作区路径（限定模式）"
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="自动模式：按 session cwd 指纹匹配现有 active binding，"
        "唯一确定项目且非通用 home 才 apply（与 --project 互斥）",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=None,
        help="增量状态文件（默认 <data-dir>/.continuity_backfill.json）",
    )
    parser.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    parser.add_argument("--max-per-run", type=int, default=MAX_PER_RUN)
    parser.add_argument("--idle-minutes", type=int, default=IDLE_MINUTES)
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    scoped = bool(args.project.strip() and args.workspace_path.strip())
    if args.auto and (args.project.strip() or args.workspace_path.strip()):
        print(
            json.dumps({"error": "auto_conflicts_with_scoped_attribution"}),
            file=sys.stderr,
        )
        return 2
    if args.apply and not args.auto and not scoped:
        print(
            json.dumps({"error": "apply_requires_auto_or_project_and_workspace"}),
            file=sys.stderr,
        )
        return 2
    state_path = args.state_path or (args.data_dir / ".continuity_backfill.json")
    store = ContextStore(Config(data_dir=args.data_dir))
    try:
        store.initialize()
        provider = WorkspaceIdentityProvider(
            key_path=args.data_dir / "workspace.key"
        )
        try:
            reports = run_backfill(
                store,
                provider,
                sessions_root=args.sessions_root,
                project=args.project.strip(),
                workspace_path=args.workspace_path.strip(),
                state_path=state_path,
                apply=bool(args.apply),
                max_per_run=args.max_per_run,
                idle_minutes=args.idle_minutes,
            )
        except WorkspaceIdentityError as exc:
            print(json.dumps({"error": exc.code}), file=sys.stderr)
            return 2
        except ContinuityError as exc:
            print(json.dumps({"error": exc.code}), file=sys.stderr)
            return 2
        for report in reports:
            print(
                json.dumps(
                    {
                        "session": report.session_id,
                        "action": report.action,
                        "reason": report.reason,
                        "workstream_id": report.workstream_id,
                        "completed": report.completed,
                        "pending": report.pending,
                    },
                    ensure_ascii=False,
                )
            )
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
