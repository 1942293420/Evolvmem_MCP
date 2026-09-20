"""项目提及召回：从当前查询识别已登记项目，只读召回其历史详情。

已授权边界（用户 2026-09-21 明确授权）：

- 只匹配本用户命名空间内已登记 active project 的 canonical 名与别名；
  英文按完整词边界（大小写不敏感），中文按子串；
- 过短表面（<2 字符）与 registry 通用名（generic_names）不参与匹配；
- 同一表面同时指向多个项目时不猜，直接忽略；最多返回两个提及项目，
  按首次提及顺序；
- 每个项目一次只读 ``ContextService.session_start``，**不传 workspace_path**：
  不触发续接路由，也不改工作区绑定或 continuity 焦点；
- 所有提及项目共享一个 ``max_chars`` 预算，信封（BEGIN/NOTE/END/项目标题）
  也计入预算，预算不足时返回空 block 而不是超发；
- 仅返回有界 L1 历史，不更改记忆内容或焦点，不跨用户；沿用读取计数。

普通 ``context_search`` 过滤与经验 transferable 规则完全不受本模块影响。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from evolvmem.context_models import ContextSessionStartRequest
from evolvmem.project_models import ProjectRegistrySnapshot

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
        name.strip().casefold()
        for name in generic_names
        if isinstance(name, str) and name.strip()
    }

    # folded surface -> canonical owners; >1 owner means ambiguous.
    owners: dict[str, set[str]] = {}
    for canonical in active.values():
        if canonical.casefold() in generic:
            continue
        owners.setdefault(canonical.casefold(), set()).add(canonical)
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
        folded = surface.casefold()
        if (
            len(surface) < MIN_SURFACE_CHARS
            or len(surface) > MAX_SURFACE_CHARS
            or folded in generic
        ):
            continue
        owners.setdefault(folded, set()).add(canonical)

    matches: list[ProjectMention] = []
    for folded, candidates in owners.items():
        if len(candidates) != 1:
            continue
        matches.extend(_find_mentions(text, folded, next(iter(candidates))))
    if not matches:
        return ()
    return _select_mentions(matches)


def _find_mentions(
    text: str, folded: str, project: str
) -> list[ProjectMention]:
    """查找 ``folded`` 在 ``text`` 中的全部出现（英文整词，中文子串）。"""
    if folded.isascii():
        pattern = _ascii_word_pattern(folded)
        return [
            ProjectMention(project=project, surface=match.group(0),
                           start=match.start())
            for match in pattern.finditer(text)
        ]
    found: list[ProjectMention] = []
    start = text.find(folded)
    while start != -1:
        found.append(
            ProjectMention(project=project, surface=folded, start=start)
        )
        start = text.find(folded, start + len(folded))
    return found


def _ascii_word_pattern(folded: str) -> re.Pattern[str]:
    """整词匹配：边界外侧不能再是 ASCII 词字符（beta 不匹配 betamax/beta2）。"""
    left = r"(?<![0-9A-Za-z_])" if _ASCII_WORD_CHAR.match(folded[0]) else ""
    right = r"(?![0-9A-Za-z_])" if _ASCII_WORD_CHAR.match(folded[-1]) else ""
    return re.compile(left + re.escape(folded) + right, re.IGNORECASE)


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
        result = service.session_start(
            ContextSessionStartRequest(
                project=match.project,
                query=query,
                max_chars=budget,
                # workspace_path 保持缺省 "": 无续接路由、无绑定/焦点变化
            ),
            project_only=True,
        )
        content = str(getattr(result, "block", "") or "").strip()
        if content:
            body.append(_project_header(match.project))
            body.append(content)
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
