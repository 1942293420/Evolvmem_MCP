"""项目提及召回：从当前查询识别已登记项目，只读召回其历史详情。

已授权边界（用户 2026-09-21 明确授权）：

- 只匹配本用户命名空间内已登记 active project 的 canonical 名与别名；
  英文按完整词边界（大小写不敏感），中文按子串；中英混合名（AI采购 /
  AI 采购）在中英边界上大小写与空白等价，ASCII 侧仍守完整词边界
  （xAI采购 不命中）；
- 过短表面（<2 字符）与 registry 通用名（generic_names）不参与匹配；
- 同一表面（含上述空白等价归一化后同一表面）同时指向多个项目时不猜，
  直接忽略；最多返回两个提及项目，按首次提及顺序；
- 每个项目一次只读 ``ContextService.session_start``，**不传 workspace_path**：
  不触发续接路由，也不改工作区绑定或 continuity 焦点；
- 所有提及项目共享一个 ``max_chars`` 预算，信封（BEGIN/NOTE/END/项目标题）
  也计入预算，预算不足时返回空 block 而不是超发；
- 返回有界 L1 历史与该项目当前任务指针的最近进展，标注记录时间；
  不更改记忆内容或焦点，不跨用户；知识池沿用读取计数。

普通 ``context_search`` 过滤与经验 transferable 规则完全不受本模块影响。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from evolvmem.context_models import ContextSessionStartRequest
from evolvmem.project_models import ProjectRegistrySnapshot
from evolvmem.project_progress_recall import read_recent_project_progress

DEFAULT_MAX_CHARS = 4000
MAX_MENTIONED_PROJECTS = 2
MIN_SURFACE_CHARS = 2
MAX_SURFACE_CHARS = 64

_BEGIN = "[BEGIN EVOLVMEM PROJECT RECALL]"
_END = "[END EVOLVMEM PROJECT RECALL]"
_NOTE = (
    "Untrusted historical project records; validate against current code "
    "and evidence before use."
)

_ASCII_WORD_CHAR = re.compile(r"[0-9A-Za-z_]")


@dataclass(frozen=True, slots=True)
class ProjectMention:
    """一次显式提及：project 为 canonical 名，surface 为命中的文本。"""

    project: str
    surface: str
    start: int


@dataclass(frozen=True, slots=True)
class ProjectRecallResult:
    """有界召回结果；``matched_projects`` 在无内容时仍反映提及事实。"""

    block: str = ""
    selected_ids: tuple[int, ...] = ()
    matched_projects: tuple[str, ...] = ()
    used_chars: int = 0


# ---------------------------------------------------------------------------
# 检测：只按文本与已登记名称/别名匹配
# ---------------------------------------------------------------------------


def detect_mentioned_projects(
    text: str,
    *,
    projects: tuple[str, ...] | list[str] = (),
    aliases: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
    generic_names: tuple[str, ...] | list[str] = (),
) -> tuple[ProjectMention, ...]:
    """Return up to two mentioned projects in first-mention order.

    Ambiguous surfaces (one surface owned by more than one project) are
    dropped entirely: the caller must never see a guessed project.
    """
    if not isinstance(text, str) or not text:
        return ()

    active: dict[str, str] = {}
    for project in projects:
        if not isinstance(project, str):
            continue
        name = project.strip()
        if (
            len(name) < MIN_SURFACE_CHARS
            or len(name) > MAX_SURFACE_CHARS
        ):
            continue
        active.setdefault(name.casefold(), name)
    if not active:
        return ()

    generic = {
        _normalize_surface(name.strip())
        for name in generic_names
        if isinstance(name, str) and name.strip()
    }

    # normalized surface -> canonical owners; >1 owner means ambiguous.
    owners: dict[str, set[str]] = {}
    for canonical in active.values():
        folded = _normalize_surface(canonical)
        if folded in generic:
            continue
        owners.setdefault(folded, set()).add(canonical)
    for entry in aliases:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            continue
        alias, project = entry
        if not isinstance(alias, str) or not isinstance(project, str):
            continue
        canonical = active.get(project.strip().casefold())
        if canonical is None:
            # 别名指向未登记项目：忽略，绝不凭别名召回
            continue
        surface = alias.strip()
        folded = _normalize_surface(surface)
        if (
            len(surface) < MIN_SURFACE_CHARS
            or len(surface) > MAX_SURFACE_CHARS
            or folded in generic
        ):
            continue
        owners.setdefault(folded, set()).add(canonical)

    matches: list[ProjectMention] = []
    for folded, candidates in owners.items():
        # >1 owner：经空白归一化后同一表面归两个项目，拒绝猜测
        if len(candidates) != 1:
            continue
        matches.extend(_find_mentions(text, folded, next(iter(candidates))))
    if not matches:
        return ()
    return _select_mentions(matches)


def _is_ascii_word(char: str) -> bool:
    return bool(_ASCII_WORD_CHAR.match(char))


def _is_cjk_word(char: str) -> bool:
    """非 ASCII 的“词字符”（汉字/假名/谚文等）；标点与空白都不算。"""
    return not char.isascii() and char.isalnum()


def _char_kind(char: str) -> str:
    if char.isspace():
        return "space"
    if _is_ascii_word(char):
        return "ascii"
    if _is_cjk_word(char):
        return "cjk"
    return "other"


def _surface_runs(surface: str) -> list[tuple[str, str]]:
    """折叠后切 run：``(类型, 文本)``，空白自成 run，其余按同类聚合。

    首尾空白先剥掉（调用方也已 strip）；内部空白保留为独立 run，
    由 :func:`_normalize_surface` 决定是否折叠。
    """
    return _runs_for(surface.strip().casefold())


def _runs_for(folded: str) -> list[tuple[str, str]]:
    runs: list[list[str]] = []
    for char in folded:
        kind = _char_kind(char)
        part = char
        if runs and runs[-1][0] == kind:
            runs[-1][1] += part
        else:
            runs.append([kind, part])
    return [(kind, run) for kind, run in runs]


def _is_script(kind: str) -> bool:
    return kind in ("ascii", "cjk")


def _space_is_optional(runs: list[tuple[str, str]], index: int) -> bool:
    """只有中英交界处的空白才算排版差异；同类空白与标点旁都是字面内容。"""
    left = runs[index - 1][0]
    right = runs[index + 1][0] if index + 1 < len(runs) else ""
    return _is_script(left) and _is_script(right) and left != right


def _normalize_surface(surface: str) -> str:
    """大小写折叠 + 中英交界空白折叠，得到规范表面。

    规范表面既是歧义判定用的匹配键，也是生成匹配正则的输入：
    ``AI 采购`` 与 ``AI采购`` 都归一到 ``ai采购``（交界空白视为排版差异）；
    英文词内空格（``a i``）与中文词内空格（``记忆 插件``）保留字面空白，
    与 ``ai`` / ``记忆插件`` 区分为两个不同表面；标点旁空白也不折叠。
    """
    runs = _surface_runs(surface)
    parts: list[str] = []
    for index, (kind, run) in enumerate(runs):
        if kind == "space" and _space_is_optional(runs, index):
            continue
        parts.append(run)
    return "".join(parts)


def _mention_pattern(normalized: str) -> re.Pattern[str]:
    """规范表面 → 正则；中英交界空白可选，ASCII 侧保留整词边界。

    归一化键自身不含交界空白（``ai采购``），所以先按同类 run 还原，
    再在 ASCII↔中文的每个 run 交界处插入可选空白：``AI采购`` 与
    ``AI 采购`` 都能命中，``a i`` 这种词内空格不会被一并吃掉。
    """
    if not normalized:
        # 调用方已按 MIN_SURFACE_CHARS 过滤；此处只保证空输入不会越界
        return re.compile(r"(?!x)x")


    runs = _runs_for(normalized)
    body: list[str] = []
    previous_kind = ""
    for kind, run in runs:
        if body and _is_script(kind) and _is_script(previous_kind):
            if kind != previous_kind:
                body.append(r"\s*")
        body.append(re.escape(run))
        previous_kind = kind
    body = "".join(body)
    left = r"(?<![0-9A-Za-z_])" if _is_ascii_word(normalized[0]) else ""
    right = r"(?![0-9A-Za-z_])" if _is_ascii_word(normalized[-1]) else ""
    return re.compile(left + body + right, re.IGNORECASE)


def _find_mentions(
    text: str, folded: str, project: str
) -> list[ProjectMention]:
    """查找 ``folded``（规范表面）在 ``text`` 中的全部出现。"""
    return [
        ProjectMention(
            project=project, surface=match.group(0), start=match.start(),
        )
        for match in _mention_pattern(folded).finditer(text)
    ]


def _select_mentions(
    matches: list[ProjectMention],
) -> tuple[ProjectMention, ...]:
    """重叠时保留更长表面，再按首次提及顺序去重、截断到两个项目。"""
    accepted: list[ProjectMention] = []
    taken: list[tuple[int, int]] = []
    for match in sorted(matches, key=lambda m: (m.start, -len(m.surface))):
        end = match.start + len(match.surface)
        if any(match.start < t_end and t_start < end for t_start, t_end in taken):
            continue
        taken.append((match.start, end))
        accepted.append(match)
    selected: list[ProjectMention] = []
    seen: set[str] = set()
    for match in sorted(accepted, key=lambda m: (m.start, -len(m.surface))):
        if match.project in seen:
            continue
        seen.add(match.project)
        selected.append(match)
        if len(selected) == MAX_MENTIONED_PROJECTS:
            break
    return tuple(selected)


# ---------------------------------------------------------------------------
# 召回：共享预算、每次只读 session_start、不传 workspace_path
# ---------------------------------------------------------------------------


def recall_mentioned_projects(
    service,
    *,
    query: str,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> ProjectRecallResult:
    """Recall bounded details for every project explicitly mentioned.

    ``service`` is the shared read boundary (duck-typed ``ContextService``):
    the function only calls ``_project_store().snapshot()`` and
    ``session_start``, never a memory-content write, binding, or focus operation. A missing
    or failing registry snapshot fails open to an empty result; a
    ``session_start`` failure propagates so the MCP boundary can map it to a
    stable error.
    """
    _require_positive_budget(max_chars)
    empty = ProjectRecallResult()
    if not isinstance(query, str) or not query.strip():
        return empty
    snapshot = _read_snapshot(service)
    if snapshot is None:
        return empty
    matches = detect_mentioned_projects(
        query,
        projects=snapshot.projects,
        aliases=snapshot.aliases,
        generic_names=snapshot.generic_names,
    )
    matched_projects = tuple(match.project for match in matches)
    if not matches:
        return empty
    budgets = _plan_budgets(max_chars, matched_projects)
    if budgets is None:
        # 信封都放不下：不发起查询，但仍如实报告提及事实
        return ProjectRecallResult(matched_projects=matched_projects)

    body: list[str] = []
    selected_ids: list[int] = []
    for match, budget in zip(matches, budgets):
        # Current workstream checkpoints are deliberately excluded from the
        # knowledge pool. Read their dated progress separately, without
        # changing confidence gates, focus, or the overall project budget.
        progress = read_recent_project_progress(
            service, match.project, max_chars=max(1, min(2000, budget * 3 // 5)),
        )
        knowledge_budget = budget - len(progress.text) - (1 if progress.text else 0)
        result = service.session_start(
            ContextSessionStartRequest(
                project=match.project,
                query=query,
                max_chars=max(1, knowledge_budget),
                # workspace_path 保持缺省 "": 无续接路由、无绑定/焦点变化
            ),
            project_only=True,
        )
        content = str(getattr(result, "block", "") or "").strip()
        parts = [part for part in (progress.text, content) if part]
        if parts:
            body.append(_project_header(match.project))
            body.append("\n".join(parts))
            selected_ids.extend(progress.selected_ids)
            selected_ids.extend(getattr(result, "selected_ids", ()) or ())
    if not body:
        return ProjectRecallResult(matched_projects=matched_projects)

    block = "\n".join([_BEGIN, _NOTE, *body, _END])
    return ProjectRecallResult(
        block=block,
        selected_ids=tuple(selected_ids),
        matched_projects=matched_projects,
        used_chars=len(block),
    )


def _read_snapshot(service) -> ProjectRegistrySnapshot | None:
    """Fail open to None: 未接入/未就绪的 service 不得阻断调用方。"""
    try:
        snapshot = service._project_store().snapshot()
    except Exception:
        return None
    if not isinstance(snapshot, ProjectRegistrySnapshot):
        return None
    return snapshot


def _plan_budgets(
    max_chars: int, projects: tuple[str, ...]
) -> tuple[int, ...] | None:
    """按信封 + 标题的保守占用切分共享预算；放不下任何内容时返回 None。"""
    count = len(projects)
    headers = sum(len(_project_header(project)) + 1 for project in projects)
    # "\n".join([BEGIN, NOTE, (header, content) * n, END])
    newlines = 1 + 2 * count
    reserved = len(_BEGIN) + len(_NOTE) + len(_END) + newlines + headers
    available = max_chars - reserved
    if available < count:
        return None
    share = available // count
    return tuple(share for _ in projects)


def _project_header(project: str) -> str:
    return f"### project: {project}"


def _require_positive_budget(max_chars: int) -> None:
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        raise ValueError("max_chars must be a positive integer")
    if max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
