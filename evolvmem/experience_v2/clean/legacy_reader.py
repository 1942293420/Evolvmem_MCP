"""Read-only SQLite legacy experience reader for Stage 4 (context_items, capability_memories, learning_memories)."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Literal

from evolvmem.experience_v2.gate.rules import CATEGORIES, L0_LIMITS

_WS_RE = re.compile(r'\s+')
_TITLE_SUFFIX_RE = re.compile(r'[（(][^()（）]{0,40}[)）]\s*$')

_CATEGORY_MAP: dict[str, tuple[str, str]] = {
    'frontend': ('前端经验', 'procedure'),
    '前端经验': ('前端经验', 'procedure'),
    'backend': ('系统环境经验', 'procedure'),
    'database': ('系统环境经验', 'procedure'),
    'architecture': ('系统环境经验', 'procedure'),
    'quality': ('系统环境经验', 'pitfall'),
    'environment': ('系统环境经验', 'env_fact'),
    '系统环境经验': ('系统环境经验', 'procedure'),
    'habit': ('业务经验', 'habit'),
    'preference': ('业务经验', 'preference'),
    'decision': ('业务经验', 'decision'),
    'delivery': ('业务经验', 'procedure'),
    'business_logic': ('业务经验', 'procedure'),
    'experience': ('业务经验', 'procedure'),
    'project_convention': ('业务经验', 'procedure'),
    'task_requirement': ('业务经验', 'procedure'),
    'reference': ('业务经验', 'procedure'),
    '业务经验': ('业务经验', 'procedure'),
}


@dataclass(frozen=True)
class LegacyExperienceRecord:
    """Normalized legacy experience row extracted read-only from SQLite."""

    legacy_id: str
    source_table: Literal['context_items', 'capability_memories', 'learning_memories']
    raw_pk: int
    legacy_status: str
    legacy_category: str
    linked_content_type: str
    project: str
    source_ref: str
    conv_prefix: str
    title_stem: str
    problem: str
    solution: str
    reusable_experience: str
    misbelief: str
    mapped_category: str
    mapped_kind: str
    transferable: bool | None
    updated_at: str
    raw_preview: str


def open_sqlite_readonly(sqlite_path: Path | str) -> sqlite3.Connection:
    """Open SQLite in strict read-only URI mode with PRAGMA query_only=ON."""
    path = Path(sqlite_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f'legacy_sqlite_not_found:{path}')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only = ON')
    return conn


def _single_line_trim(text: Any, max_len: int) -> str:
    clean = _WS_RE.sub(' ', str(text or '')).strip()
    if len(clean) <= max_len:
        return clean
    if max_len <= 1:
        return clean[:max_len]
    return clean[: max_len - 1].rstrip() + '…'


def _normalize_stem(title: str) -> str:
    stem = _TITLE_SUFFIX_RE.sub('', _WS_RE.sub(' ', title).strip()).strip()
    return _WS_RE.sub(' ', stem).lower()


def _conv_prefix(source_ref: str) -> str:
    if source_ref.startswith('conversation:'):
        return source_ref.split('#', 1)[0]
    return ''


def _map_category_and_kind(raw_cat: str) -> tuple[str, str]:
    mapped = _CATEGORY_MAP.get(str(raw_cat or '').strip().lower())
    if mapped:
        return mapped
    if raw_cat in CATEGORIES:
        return raw_cat, 'procedure'
    return '业务经验', 'procedure'


def _parse_json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except ValueError:
        return {}


def _parse_bool_or_none(val: Any) -> bool | None:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low == 'true':
            return True
        if low == 'false':
            return False
    return None


def _layer_map(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    if not item_ids:
        return out
    chunk = 500
    for i in range(0, len(item_ids), chunk):
        ids = item_ids[i : i + chunk]
        marks = ','.join('?' * len(ids))
        rows = conn.execute(
            f"SELECT item_id, lower(layer) AS layer, content FROM context_layers WHERE item_id IN ({marks})",
            ids,
        ).fetchall()
        for r in rows:
            out.setdefault(int(r['item_id']), {})[str(r['layer'])] = str(r['content'] or '')
    return out


def _read_context_experience_items(conn: sqlite3.Connection) -> list[LegacyExperienceRecord]:
    rows = conn.execute(
        """
        SELECT id, project, status, updated_at, experience_payload
        FROM context_items
        WHERE content_type = 'experience'
        ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END, id DESC
        """
    ).fetchall()
    layers_by_id = _layer_map(conn, [int(r['id']) for r in rows])
    records: list[LegacyExperienceRecord] = []
    for r in rows:
        pk = int(r['id'])
        ep = _parse_json_obj(r['experience_payload'])
        layers = layers_by_id.get(pk, {})
        raw_prob = str(ep.get('problem') or layers.get('l0') or '')
        steps = ep.get('steps')
        steps_txt = ' '.join(str(x) for x in steps) if isinstance(steps, list) else str(steps or '')
        sol_joined = ' '.join(x for x in (str(ep.get('rationale') or ''), steps_txt, str(ep.get('result') or '')) if x)
        raw_sol = sol_joined or layers.get('l1') or layers.get('l2') or raw_prob
        raw_reus = str(ep.get('rationale') or raw_prob)
        proj = str(ep.get('project') or r['project'] or '')
        mcat, mkind = _map_category_and_kind(str(ep.get('category') or 'experience'))
        raw_src = json.dumps(ep, ensure_ascii=False) if ep else (layers.get('l2') or layers.get('l1') or raw_prob)
        records.append(
            LegacyExperienceRecord(
                legacy_id=f'context_items:{pk}',
                source_table='context_items',
                raw_pk=pk,
                legacy_status=str(r['status'] or ''),
                legacy_category='experience',
                linked_content_type='experience',
                project=proj,
                source_ref='',
                conv_prefix='',
                title_stem=_normalize_stem(raw_prob),
                problem=_single_line_trim(raw_prob, L0_LIMITS['problem']),
                solution=_single_line_trim(raw_sol, L0_LIMITS['solution']),
                reusable_experience=_single_line_trim(raw_reus, L0_LIMITS['reusable_experience']),
                misbelief='',
                mapped_category=mcat,
                mapped_kind=mkind,
                transferable=_parse_bool_or_none(ep.get('transferable')),
                updated_at=str(r['updated_at'] or ''),
                raw_preview=_single_line_trim(raw_src, 260),
            )
        )
    return records


def _read_learning_memories(conn: sqlite3.Connection) -> list[LegacyExperienceRecord]:
    rows = conn.execute(
        """
        SELECT lm.item_id, lm.category, lm.payload,
               COALESCE(ci.status, 'unknown') AS ci_status,
               COALESCE(ci.content_type, '') AS ci_content_type,
               COALESCE(ci.project, '') AS ci_project,
               COALESCE(ci.updated_at, '') AS ci_updated_at
        FROM learning_memories lm
        LEFT JOIN context_items ci ON ci.id = lm.item_id
        ORDER BY CASE ci.status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END, lm.item_id DESC
        """
    ).fetchall()
    layers_by_id = _layer_map(conn, [int(r['item_id']) for r in rows])
    records: list[LegacyExperienceRecord] = []
    for r in rows:
        pk = int(r['item_id'])
        p = _parse_json_obj(r['payload'])
        layers = layers_by_id.get(pk, {})
        binding = p.get('binding') if isinstance(p.get('binding'), dict) else {}
        ans = p.get('answer_support') if isinstance(p.get('answer_support'), dict) else {}
        raw_prob = str(layers.get('l0') or binding.get('trigger') or p.get('rationale') or '')
        raw_sol = str(layers.get('l1') or layers.get('l2') or p.get('rationale') or ans.get('asked') or raw_prob)
        raw_reus = str(p.get('rationale') or layers.get('l0') or raw_prob)
        mcat, mkind = _map_category_and_kind(str(r['category'] or ''))
        raw_src = f"{layers.get('l0', '')} | {layers.get('l1', '')} | {r['payload'] or ''}"
        records.append(
            LegacyExperienceRecord(
                legacy_id=f'learning_memories:{pk}',
                source_table='learning_memories',
                raw_pk=pk,
                legacy_status=str(r['ci_status'] or ''),
                legacy_category=str(r['category'] or ''),
                linked_content_type=str(r['ci_content_type'] or ''),
                project=str(r['ci_project'] or ''),
                source_ref=str(p.get('source_session') or ''),
                conv_prefix='',
                title_stem=_normalize_stem(raw_prob),
                problem=_single_line_trim(raw_prob, L0_LIMITS['problem']),
                solution=_single_line_trim(raw_sol, L0_LIMITS['solution']),
                reusable_experience=_single_line_trim(raw_reus, L0_LIMITS['reusable_experience']),
                misbelief='',
                mapped_category=mcat,
                mapped_kind=mkind,
                transferable=None,
                updated_at=str(r['ci_updated_at'] or ''),
                raw_preview=_single_line_trim(raw_src, 260),
            )
        )
    return records


def _read_capability_memories(conn: sqlite3.Connection) -> list[LegacyExperienceRecord]:
    rows = conn.execute(
        """
        SELECT memory_id, category, title, content, source_project, source_ref, status, updated_at
        FROM capability_memories
        ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, memory_id DESC
        """
    ).fetchall()
    records: list[LegacyExperienceRecord] = []
    for r in rows:
        pk = int(r['memory_id'])
        title = str(r['title'] or '')
        content = str(r['content'] or '')
        sref = str(r['source_ref'] or '')
        mcat, mkind = _map_category_and_kind(str(r['category'] or ''))
        records.append(
            LegacyExperienceRecord(
                legacy_id=f'capability_memories:{pk}',
                source_table='capability_memories',
                raw_pk=pk,
                legacy_status=str(r['status'] or ''),
                legacy_category=str(r['category'] or ''),
                linked_content_type='capability',
                project=str(r['source_project'] or ''),
                source_ref=sref,
                conv_prefix=_conv_prefix(sref),
                title_stem=_normalize_stem(title),
                problem=_single_line_trim(title, L0_LIMITS['problem']),
                solution=_single_line_trim(content, L0_LIMITS['solution']),
                reusable_experience=_single_line_trim(title, L0_LIMITS['reusable_experience']),
                misbelief='',
                mapped_category=mcat,
                mapped_kind=mkind,
                transferable=None,
                updated_at=str(r['updated_at'] or ''),
                raw_preview=_single_line_trim(f'{title} :: {content}', 260),
            )
        )
    return records


def read_legacy_experience_candidates(sqlite_path: Path | str) -> list[LegacyExperienceRecord]:
    """Read-only load of legacy experience candidates (never touches history tables or manual notes)."""
    conn = open_sqlite_readonly(sqlite_path)
    try:
        ci = _read_context_experience_items(conn)
        lm = _read_learning_memories(conn)
        cm = _read_capability_memories(conn)
        all_rows = ci + lm + cm
        table_rank = {'context_items': 0, 'learning_memories': 1, 'capability_memories': 2}
        status_rank = {'active': 0, 'candidate': 1}
        all_rows.sort(
            key=lambda x: (
                table_rank.get(x.source_table, 9),
                status_rank.get(x.legacy_status, 2),
                -x.raw_pk,
            )
        )
        return all_rows
    finally:
        conn.close()
