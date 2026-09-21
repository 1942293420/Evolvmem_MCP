"""项目进展召回：只读本用户 store 中 workstream 当前断点（L1）。

背景（用户 2026-09-21 指出的缺口）：``context_project_recall`` 目前只带
``session_start`` 知识池，而默认检索按全局隔离契约排除
``workstream_checkpoint``；用户较新的进展恰好落在
``continuity_workstreams.current_context_id`` 指向的当前断点里。本模块提供一个
最小只读辅助函数，供协调者接入 ``project_mention_recall``；它不修改那三样：
全局隔离契约、0.5 置信度门槛、每日摘要来源。

已授权边界：

- 只读调用方注入的 ``service.store``（本用户自己的库），不跨用户、不跨库；
- JOIN ``continuity_workstreams.current_context_id = context_items.id``；
- 项目在 workstream 与 item 两侧严格一致，且 item scope 固定为 project；
- 只取 item.status=active、content_type=workstream_checkpoint、layer=l1：
  因此绝不带出 superseded 旧断点，也不用候选/归档 item 冒充当前进展；
- 至多最近 3 条，按当前断点 created_at DESC, id DESC；
- ``max_chars`` 是硬总预算：单条可有界截断，但完整保留日期/id/状态头部、
  目标与已完成内容，并明确标注这是历史任务记录、记录时间取自数据库 UTC，
  不代表 Git 提交或线上即时状态；预算不足时返回空而不是超发；
- 全程只读：不改焦点、工作区绑定、访问计数或任何记忆内容。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

DEFAULT_MAX_CHARS = 1200
MAX_CHECKPOINTS = 3

# 一条记录至少要有头部 + 一段正文才值得占用预算；不足则整块返回空。
_MIN_BODY_CHARS = 60
_MIN_LINE_CHARS = 24

_TRUNCATION_MARKER = "…〔截断〕"
_OMISSION_MARKER = "…〔其余内容因字符预算省略〕"

_NOTE = (
    "以下为用户历史任务记录（记录时间取自数据库 UTC）；"
    "不代表 Git 提交或线上即时状态。"
)

# 只经由 continuity_workstreams 当前指针取当前断点；superseded 旧断点不会
# 出现在 current_context_id 上，item.status=active 再兜一层。
_SELECT_SQL = """
SELECT i.id AS context_id,
       i.created_at AS created_at,
       w.status AS workstream_status,
       l.content AS l1
FROM continuity_workstreams AS w
JOIN context_items AS i ON i.id = w.current_context_id
JOIN context_layers AS l ON l.item_id = i.id AND l.layer = 'l1'
WHERE w.project = ?
  AND i.project = ?
  AND i.scope = 'project'
  AND i.status = 'active'
  AND i.content_type = 'workstream_checkpoint'
ORDER BY i.created_at DESC, i.id DESC
LIMIT ?
"""


@dataclass(frozen=True, slots=True)
class ProjectProgressResult:
    """有界进展文本；``selected_ids`` 只列被完整包含的断点 id。"""

    text: str = ""
    selected_ids: tuple[int, ...] = ()
    used_chars: int = 0


def read_recent_project_progress(
    service, project: str, max_chars: int = DEFAULT_MAX_CHARS
) -> ProjectProgressResult:
    """Read the project's newest workstream checkpoints as bounded plain text.

    ``service`` is the caller's own read boundary (duck-typed, e.g.
    ``ContextService``): only ``service.store`` is queried, with one SELECT
    join. The function never writes, never routes continuation, and never
    touches the project registry, workspace binding, or continuity focus.
    An unavailable store or a database without the continuity schema fails
    open to an empty result so auxiliary recall never blocks the caller.
    """
    _require_positive_budget(max_chars)
    if not isinstance(project, str) or not project.strip():
        return ProjectProgressResult()
    connection = _store_connection(service)
    if connection is None:
        return ProjectProgressResult()
    name = project.strip()
    try:
        rows = connection.execute(
            _SELECT_SQL, (name, name, MAX_CHECKPOINTS)
        ).fetchall()
    except sqlite3.DatabaseError:
        # 旧库没有 continuity schema，或库不可读：辅助读返回空，不阻断召回
        return ProjectProgressResult()
    return _render(rows, max_chars)


def _store_connection(service):
    """Fail open to None：service 未接入 / store 未就绪时不打断调用方。"""
    try:
        store = service.store
        return store._connection()
    except (AttributeError, TypeError, RuntimeError):
        return None


def _render(rows, max_chars: int) -> ProjectProgressResult:
    """把候选行渲染成总额不超过 ``max_chars`` 的文本。"""
    # text = "\n".join([note, *entries])：note 之后每条 entry 都带一个换行
    remaining = max_chars - len(_NOTE)
    entries: list[str] = []
    selected: list[int] = []
    for row in rows:
        header = _header(row)
        body_budget = remaining - 1 - len(header) - 1
        if body_budget < _MIN_BODY_CHARS:
            break
        body, complete = _render_body(str(row["l1"] or ""), body_budget)
        if not body:
            if not complete:
                break
            continue  # L1 为空：没有内容可展示，继续看更旧的一条
        entry = header + "\n" + body
        if len(entry) + 1 > remaining:
            break
        entries.append(entry)
        remaining -= len(entry) + 1
        if complete:
            selected.append(int(row["context_id"]))
        else:
            break  # 至多截断一条：不再继续更旧的断点
    if not entries:
        return ProjectProgressResult()
    text = "\n".join([_NOTE, *entries])
    return ProjectProgressResult(
        text=text, selected_ids=tuple(selected), used_chars=len(text)
    )


def _header(row) -> str:
    """完整头部：日期（数据库 UTC）、断点 id、workstream 原状态。"""
    return (
        f"### 历史任务记录 id={int(row['context_id'])}"
        f" status={row['workstream_status']}"
        f" created_at(UTC)={row['created_at']}"
    )


def _render_body(l1: str, budget: int) -> tuple[str, bool]:
    """按行分配预算渲染 L1；返回 (正文, 是否完整包含全部非空行)。

    目标行与已完成行优先保留配额；任何被截断或被省略的行都有明确标识，
    因此调用方既能看到足够的目标/已完成内容，也不会误以为内容完整。
    """
    lines = [line.strip() for line in l1.splitlines() if line.strip()]
    if not lines:
        return "", True
    # 预留一个省略标记与行间换行，保证所有分支都不超预算
    allowance = budget - len(_OMISSION_MARKER) - (len(lines) - 1)
    if allowance <= 0:
        return "", False
    caps = _line_caps(lines, allowance)
    pieces: list[str] = []
    omitted = False
    truncated = False
    for index, line in enumerate(lines):
        cap = caps[index]
        if cap <= 0:
            omitted = True
            continue
        if cap >= len(line):
            pieces.append(line)
            continue
        room = cap - len(_TRUNCATION_MARKER)
        if room <= 0:
            omitted = True
            continue
        pieces.append(line[:room] + _TRUNCATION_MARKER)
        truncated = True
    if not pieces:
        return "", False
    if omitted:
        pieces.append(_OMISSION_MARKER)
    return "\n".join(pieces), not (omitted or truncated)


def _line_caps(lines: list[str], allowance: int) -> list[int]:
    """给每行分配字符配额：目标/已完成优先，剩余按文档顺序补齐。"""
    caps = [0] * len(lines)
    must = [0]
    completed = next(
        (index for index, line in enumerate(lines) if line.startswith("已完成")),
        None,
    )
    if completed is not None and completed != 0:
        must.append(completed)
    remaining = allowance
    per_must = max(_MIN_LINE_CHARS, allowance // (2 * len(must)))
    for index in must:
        caps[index] = min(len(lines[index]), per_must, remaining)
        remaining -= caps[index]
    others = [index for index in range(len(lines)) if index not in set(must)]
    if others and remaining > 0:
        share = remaining // len(others)
        if share >= _MIN_LINE_CHARS:
            for index in others:
                caps[index] = min(len(lines[index]), share)
                remaining -= caps[index]
    if remaining > 0:
        # 还有余量（must 行较短、或只有单行）时按文档顺序补齐整行
        for index, line in enumerate(lines):
            if remaining <= 0:
                break
            room = len(line) - caps[index]
            if room <= 0:
                continue
            top = min(room, remaining)
            caps[index] += top
            remaining -= top
    return caps


def _require_positive_budget(max_chars: int) -> None:
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        raise ValueError("max_chars must be a positive integer")
    if max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
