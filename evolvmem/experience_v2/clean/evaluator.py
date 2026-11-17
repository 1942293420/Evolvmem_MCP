"""Task 4.1: Read-only L0 -> L1 -> L2 legacy experience evaluator."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Sequence

from evolvmem.experience_v2.clean.legacy_reader import (
    LegacyExperienceRecord,
    read_legacy_experience_candidates,
)
from evolvmem.experience_v2.gate.dedupe import (
    DEFAULT_DEDUPE_THRESHOLD,
    _encode_text,
    normalize_vec768,
)
from evolvmem.experience_v2.gate.rules import _contains_secret, evaluate_l0_fields
from evolvmem.experience_v2.gate.value import assess_value_cascade
from evolvmem.experience_v2.llm_meter import BudgetConfig, BudgetDeferred, LlmTransportError
from evolvmem.experience_v2.pipeline.pilot import scrub_pilot_text

_EPHEMERAL_TASK_RE = re.compile(
    r'本轮|本次(?:会话|整理|改造|排查)|留到下一步|刚才|今天先|下一轮再|待后续处理'
)
_HOST_SPECIFIC_RE = re.compile(
    r'(?<![0-9.])192\.168\.\d{1,3}\.\d{1,3}(?![0-9.])|\bssh\s+-p\s+\d+|/workspace/|colleague@',
    re.IGNORECASE,
)
_DELIVERY_LOG_RE = re.compile(
    r'已上线|已交付|已向用户|已实施|部分实施|阶段性|共关闭\s*\d+|容器健康|已处理\s*\d+'
    r'|新增\s*\d+\s*条|测试通过\s*[:：]?\s*\d+|待验证|生产部署未开放|方案与实施记录'
    r'|方案与部分实施|已切换并验证|现状汇总|进度汇报|开发到什么地步|用户要求关闭|首屏主卡'
    r'|改为可搜索.*条|批次结果[:：]已入库'
)
L1_SURVIVOR_CAP = 1800


def _scrub(text: Any, extra_terms: Sequence[str]) -> str:
    return scrub_pilot_text(text, extra_terms=extra_terms)


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _evaluate_l0(r: LegacyExperienceRecord) -> tuple[bool, str | None, str]:
    if r.source_table == 'capability_memories' and r.legacy_status != 'active':
        return False, 'legacy_inactive_status', f'legacy status={r.legacy_status}'
    if r.legacy_status not in ('active', 'candidate'):
        return False, 'legacy_inactive_status', f'legacy status={r.legacy_status}'
    if r.source_table == 'context_items' and r.transferable is False:
        return False, 'legacy_non_transferable', 'experience_payload.transferable=false'
    if r.source_table == 'learning_memories' and r.legacy_category in ('reference', 'environment'):
        return False, 'legacy_static_fact_not_experience', f'learning_memories category={r.legacy_category}'
    if (
        r.source_table == 'learning_memories'
        and r.legacy_category == 'task_requirement'
        and _EPHEMERAL_TASK_RE.search(f'{r.problem} {r.solution}')
    ):
        return False, 'legacy_ephemeral_task_instruction', '单轮临时任务指令而非可复用经验'
    if (
        _contains_secret([r.problem, r.solution, r.reusable_experience, r.raw_preview])
        or _HOST_SPECIFIC_RE.search(f'{r.problem} {r.solution} {r.raw_preview}')
    ):
        return False, 'secret_or_host_specific_config', '检测到密钥或宿主机/内网专有路径配置'
    if (
        (r.source_table == 'capability_memories' and r.legacy_category == 'delivery')
        or _DELIVERY_LOG_RE.search(r.problem)
    ):
        return False, 'legacy_delivery_or_ops_log', '阶段交付或运维状态汇总而非可复用经验'
    if len(r.problem.strip()) < 4 or len(r.solution.strip()) < 8:
        return False, 'empty_or_too_short', 'problem 或 solution 过短'
    verdict, rc, det = evaluate_l0_fields(
        {
            'category': r.mapped_category,
            'kind': r.mapped_kind,
            'problem': r.problem,
            'solution': r.solution,
            'reusable_experience': r.reusable_experience,
            'misbelief': r.misbelief,
        },
        require_evidence=False,
    )
    if verdict != 'pass':
        return False, (rc or 'l0_invalid'), det
    return True, None, 'L0 通过'


def _encode_all(
    texts: list[str],
    *,
    embedder: Any,
    batch_encode_fn: Callable[[list[str]], list[list[float]]] | None,
) -> list[list[float]]:
    if not texts:
        return []
    if batch_encode_fn is not None:
        raw_vecs = batch_encode_fn(texts)
    elif embedder is not None:
        raw_vecs = [_encode_text(embedder, t) for t in texts]
    else:
        raise ValueError('embedder_or_batch_encode_fn_required')
    if len(raw_vecs) != len(texts):
        raise ValueError(f'embedding_count_mismatch:{len(raw_vecs)}!={len(texts)}')
    return [normalize_vec768(v) for v in raw_vecs]


def _load_active_manual_vecs(
    conn,
    *,
    owner_id: str,
    embedder: Any,
    batch_encode_fn: Callable[[list[str]], list[list[float]]] | None,
) -> list[tuple[str, list[float]]]:
    rows = conn.execute(
        """
        SELECT id, problem, solution, reusable_experience, embedding::text
        FROM experience_notes
        WHERE owner_id = %s AND status = 'active' AND origin = 'manual' AND deleted_at IS NULL
        """,
        (owner_id,),
    ).fetchall()
    out: list[tuple[str, list[float]]] = []
    missing_ids: list[str] = []
    missing_texts: list[str] = []
    for note_id, prob, sol, reus, emb_txt in rows:
        if emb_txt:
            out.append((str(note_id), normalize_vec768(json.loads(emb_txt))))
        else:
            missing_ids.append(str(note_id))
            missing_texts.append(f'{prob}\n{sol}\n{reus}')
    if missing_texts:
        vecs = _encode_all(missing_texts, embedder=embedder, batch_encode_fn=batch_encode_fn)
        out.extend(zip(missing_ids, vecs))
    return out


def _manifest_entry(
    r: LegacyExperienceRecord,
    *,
    gate_layer: str,
    reason_code: str | None,
    detail: str,
    l2_tier: str = '',
    confidence: float = 1.0,
    judge_model: str = '',
    extra_terms: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        'legacy_id': r.legacy_id,
        'source_table': r.source_table,
        'raw_pk': r.raw_pk,
        'legacy_status': r.legacy_status,
        'legacy_category': r.legacy_category,
        'linked_content_type': r.linked_content_type,
        'mapped_category': r.mapped_category,
        'mapped_kind': r.mapped_kind,
        'project': _scrub(r.project, extra_terms),
        'source_ref': _scrub(r.source_ref, extra_terms),
        'conv_prefix': r.conv_prefix,
        'updated_at': r.updated_at,
        'problem': _scrub(r.problem, extra_terms),
        'solution': _scrub(r.solution, extra_terms),
        'reusable_experience': _scrub(r.reusable_experience, extra_terms),
        'misbelief': _scrub(r.misbelief, extra_terms),
        'raw_preview': _scrub(r.raw_preview, extra_terms),
        'gate_layer': gate_layer,
        'l2_tier': l2_tier,
        'reason_code': reason_code or '',
        'confidence': float(confidence),
        'judge_model': judge_model,
        'detail': _scrub(detail, extra_terms)[:200],
    }


def evaluate_legacy_clean(
    *,
    sqlite_path: Path | str,
    conn,
    owner_id: str,
    flash_model: str,
    strong_model: str,
    flash_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]],
    strong_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]] | None = None,
    embedder: Any = None,
    batch_encode_fn: Callable[[list[str]], list[list[float]]] | None = None,
    dedupe_threshold: float = DEFAULT_DEDUPE_THRESHOLD,
    intra_conv_threshold: float = 0.82,
    max_workers: int = 1,
    conn_factory: Callable[[], Any] | None = None,
    budget: BudgetConfig | None = None,
    extra_scrub_terms: Sequence[str] = (),
    preflight_only: bool = False,
    l1_survivor_cap: int = L1_SURVIVOR_CAP,
) -> dict[str, Any]:
    """Run read-only L0 -> L1 -> L2 legacy cleanup evaluation without mutating SQLite or live notes."""
    notes_before = int(conn.execute('SELECT COUNT(*) FROM experience_notes').fetchone()[0])
    props_before = int(conn.execute('SELECT COUNT(*) FROM experience_proposals').fetchone()[0])
    max_call_id_before = int(conn.execute('SELECT COALESCE(MAX(id), 0) FROM llm_calls').fetchone()[0] or 0)

    records = read_legacy_experience_candidates(sqlite_path)
    deferred_children: list[LegacyExperienceRecord] = []
    l0_pass: list[LegacyExperienceRecord] = []
    delete_entries: list[dict[str, Any]] = []
    keep_entries: list[dict[str, Any]] = []

    for r in records:
        if r.source_table == 'learning_memories' and r.linked_content_type == 'experience':
            deferred_children.append(r)
            continue
        ok, rc, det = _evaluate_l0(r)
        if not ok:
            delete_entries.append(
                _manifest_entry(r, gate_layer='L0', reason_code=rc, detail=det, judge_model='rule-l0', extra_terms=extra_scrub_terms)
            )
        else:
            l0_pass.append(r)

    seen_stems: dict[tuple[str, str], str] = {}
    l1_candidates: list[LegacyExperienceRecord] = []
    for r in l0_pass:
        if r.conv_prefix and r.title_stem:
            key = (r.conv_prefix, r.title_stem)
            first_id = seen_stems.get(key)
            if first_id is not None:
                delete_entries.append(
                    _manifest_entry(
                        r,
                        gate_layer='L1',
                        reason_code='duplicate_conversation_snapshot',
                        detail=f'同会话快照重复 ({first_id})',
                        judge_model='rule-l1',
                        extra_terms=extra_scrub_terms,
                    )
                )
                continue
            seen_stems[key] = r.legacy_id
        l1_candidates.append(r)

    manual_vecs = _load_active_manual_vecs(conn, owner_id=owner_id, embedder=embedder, batch_encode_fn=batch_encode_fn)
    cand_texts = [f'{r.problem}\n{r.solution}\n{r.reusable_experience}' for r in l1_candidates]
    cand_vecs = _encode_all(cand_texts, embedder=embedder, batch_encode_fn=batch_encode_fn)

    l1_survivors: list[LegacyExperienceRecord] = []
    accepted_vecs: list[tuple[LegacyExperienceRecord, list[float]]] = []
    by_conv: dict[str, list[tuple[LegacyExperienceRecord, list[float]]]] = {}

    for r, vec in zip(l1_candidates, cand_vecs):
        best_m_id, best_m_sim = '', -1.0
        for nid, nvec in manual_vecs:
            sim = _dot(vec, nvec)
            if sim > best_m_sim:
                best_m_id, best_m_sim = nid, sim
        if best_m_id and best_m_sim >= float(dedupe_threshold):
            delete_entries.append(
                _manifest_entry(
                    r,
                    gate_layer='L1',
                    reason_code='duplicate_of_active_manual_note',
                    detail=f'与现役手工经验 {best_m_id} 余弦相似度 {best_m_sim:.3f}',
                    confidence=best_m_sim,
                    judge_model='local-embedder',
                    extra_terms=extra_scrub_terms,
                )
            )
            continue

        best_l_id, best_l_sim = '', -1.0
        for prev_r, prev_vec in accepted_vecs:
            sim = _dot(vec, prev_vec)
            if sim > best_l_sim:
                best_l_id, best_l_sim = prev_r.legacy_id, sim
        if best_l_id and best_l_sim >= float(dedupe_threshold):
            delete_entries.append(
                _manifest_entry(
                    r,
                    gate_layer='L1',
                    reason_code='duplicate_of_legacy_survivor',
                    detail=f'与保留候选 {best_l_id} 余弦相似度 {best_l_sim:.3f}',
                    confidence=best_l_sim,
                    judge_model='local-embedder',
                    extra_terms=extra_scrub_terms,
                )
            )
            continue

        if r.conv_prefix and r.conv_prefix in by_conv:
            best_c_id, best_c_sim = '', -1.0
            for prev_r, prev_vec in by_conv[r.conv_prefix]:
                sim = _dot(vec, prev_vec)
                if sim > best_c_sim:
                    best_c_id, best_c_sim = prev_r.legacy_id, sim
            if best_c_id and best_c_sim >= float(intra_conv_threshold):
                delete_entries.append(
                    _manifest_entry(
                        r,
                        gate_layer='L1',
                        reason_code='duplicate_conversation_revision',
                        detail=f'同会话渐进修订重复 {best_c_id} ({best_c_sim:.3f})',
                        confidence=best_c_sim,
                        judge_model='local-embedder',
                        extra_terms=extra_scrub_terms,
                    )
                )
                continue

        l1_survivors.append(r)
        accepted_vecs.append((r, vec))
        if r.conv_prefix:
            by_conv.setdefault(r.conv_prefix, []).append((r, vec))

    if preflight_only:
        reason_counts = dict(Counter(e['reason_code'] for e in delete_entries))
        return {
            'preflight_only': True,
            'total_candidates': len(records),
            'deferred_fk_children': len(deferred_children),
            'l0_l1_deleted_count': len(delete_entries),
            'l1_survivor_count': len(l1_survivors),
            'l1_survivor_by_table': dict(Counter(r.source_table for r in l1_survivors)),
            'l1_survivor_cap': int(l1_survivor_cap),
            'reason_counts': reason_counts,
        }

    if len(l1_survivors) > int(l1_survivor_cap):
        raise ValueError(f'l1_survivor_cap_exceeded:{len(l1_survivors)}>{int(l1_survivor_cap)}')

    active_budget = budget or BudgetConfig()
    stop_l2 = threading.Event()

    def _run_l2_one(r: LegacyExperienceRecord, use_conn) -> dict[str, Any]:
        if stop_l2.is_set():
            return {'record': r, 'deferred': True, 'detail': 'budget_deferred'}
        scope = {'project': r.project} if r.project else {}
        last_exc: Exception | None = None
        for attempt in range(3):
            if stop_l2.is_set():
                return {'record': r, 'deferred': True, 'detail': 'budget_deferred'}
            try:
                outcome = assess_value_cascade(
                    use_conn,
                    problem=r.problem,
                    solution=r.solution,
                    scope=scope,
                    flash_model=flash_model,
                    strong_model=strong_model,
                    flash_transport=flash_transport,
                    strong_transport=strong_transport,
                    track='backfill',
                    owner_id=owner_id,
                    proposal_uid=r.legacy_id,
                    is_shadow=True,
                    budget=active_budget,
                )
                return {'record': r, 'outcome': outcome}
            except BudgetDeferred as exc:
                stop_l2.set()
                return {'record': r, 'deferred': True, 'detail': f'{exc.scope}:{exc.detail}'}
            except LlmTransportError as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        raise RuntimeError(f'unreachable:{last_exc}')

    if max_workers > 1:
        if conn_factory is None:
            raise ValueError('conn_factory_required_for_multiworker_l2')

        def _worker(r: LegacyExperienceRecord) -> dict[str, Any]:
            wconn = conn_factory()
            try:
                res = _run_l2_one(r, wconn)
                if not getattr(wconn, 'autocommit', False):
                    wconn.commit()
                return res
            except Exception:
                if not getattr(wconn, 'autocommit', False):
                    wconn.rollback()
                raise
            finally:
                wconn.close()

        with ThreadPoolExecutor(max_workers=int(max_workers)) as pool:
            l2_results = list(pool.map(_worker, l1_survivors))
    else:
        l2_results = [_run_l2_one(r, conn) for r in l1_survivors]

    keep_by_id: dict[str, dict[str, Any]] = {}
    delete_by_id: dict[str, dict[str, Any]] = {}
    deferred_entries: list[dict[str, Any]] = []
    deferred_by_id: dict[str, dict[str, Any]] = {}
    for item in l2_results:
        r = item['record']
        if item.get('deferred'):
            entry = _manifest_entry(
                r,
                gate_layer='L2',
                reason_code='budget_deferred',
                detail=str(item.get('detail') or 'budget_deferred'),
                l2_tier='deferred',
                confidence=0.0,
                judge_model='',
                extra_terms=extra_scrub_terms,
            )
            deferred_entries.append(entry)
            deferred_by_id[r.legacy_id] = entry
            continue
        out = item['outcome']
        if out.verdict == 'pass':
            entry = _manifest_entry(
                r,
                gate_layer='L2',
                reason_code='',
                detail=out.detail,
                l2_tier=out.layer,
                confidence=out.confidence,
                judge_model=out.judge_model,
                extra_terms=extra_scrub_terms,
            )
            keep_entries.append(entry)
            keep_by_id[r.legacy_id] = entry
        else:
            entry = _manifest_entry(
                r,
                gate_layer='L2',
                reason_code=(out.reason_code or 'common_knowledge'),
                detail=out.detail,
                l2_tier=out.layer,
                confidence=out.confidence,
                judge_model=out.judge_model,
                extra_terms=extra_scrub_terms,
            )
            delete_entries.append(entry)
            delete_by_id[r.legacy_id] = entry

    for entry in delete_entries:
        delete_by_id.setdefault(entry['legacy_id'], entry)

    for child in deferred_children:
        parent_id = f'context_items:{child.raw_pk}'
        if parent_id in keep_by_id:
            keep_entries.append(
                _manifest_entry(
                    child,
                    gate_layer=keep_by_id[parent_id]['gate_layer'],
                    reason_code='',
                    detail=f'继承父经验保留判定 ({parent_id})',
                    l2_tier=keep_by_id[parent_id]['l2_tier'],
                    confidence=keep_by_id[parent_id]['confidence'],
                    judge_model='fk-inherit',
                    extra_terms=extra_scrub_terms,
                )
            )
        elif parent_id in deferred_by_id:
            deferred_entries.append(
                _manifest_entry(
                    child,
                    gate_layer='L2',
                    reason_code='budget_deferred',
                    detail=f'父经验受预算延后未评估 ({parent_id})',
                    l2_tier='deferred',
                    confidence=0.0,
                    judge_model='fk-inherit',
                    extra_terms=extra_scrub_terms,
                )
            )
        else:
            p_del = delete_by_id.get(parent_id)
            delete_entries.append(
                _manifest_entry(
                    child,
                    gate_layer=(p_del['gate_layer'] if p_del else 'L0'),
                    reason_code=(p_del['reason_code'] if p_del else 'orphan_learning_memory'),
                    detail=f'继承父经验删除判定 ({parent_id})',
                    l2_tier=(p_del['l2_tier'] if p_del else ''),
                    confidence=(p_del['confidence'] if p_del else 1.0),
                    judge_model='fk-inherit',
                    extra_terms=extra_scrub_terms,
                )
            )

    notes_after = int(conn.execute('SELECT COUNT(*) FROM experience_notes').fetchone()[0])
    props_after = int(conn.execute('SELECT COUNT(*) FROM experience_proposals').fetchone()[0])
    llm_row = conn.execute(
        """
        SELECT COUNT(*),
               COALESCE(SUM(input_tokens + output_tokens), 0),
               COALESCE(SUM(cost_estimate), 0.0)
        FROM llm_calls
        WHERE id > %s
        """,
        (max_call_id_before,),
    ).fetchone()

    layer_counts = dict(Counter(e['gate_layer'] for e in delete_entries))
    reason_counts = dict(Counter(e['reason_code'] for e in delete_entries))
    summary = {
        'total_candidates': len(records),
        'keep_count': len(keep_entries),
        'delete_count': len(delete_entries),
        'l0_pass_count': len(l0_pass),
        'l1_survivor_count': len(l1_survivors),
        'deferred_fk_children': len(deferred_children),
        'deferred_count': len(deferred_entries),
        'l1_survivor_cap': int(l1_survivor_cap),
        'delete_by_layer': layer_counts,
        'delete_by_reason': reason_counts,
        'keep_by_table': dict(Counter(e['source_table'] for e in keep_entries)),
        'delete_by_table': dict(Counter(e['source_table'] for e in delete_entries)),
    }
    return {
        'status': 'deferred' if deferred_entries else 'completed',
        'deferred_count': len(deferred_entries),
        'l1_survivor_cap': int(l1_survivor_cap),
        'keep_manifest': keep_entries,
        'delete_manifest': delete_entries,
        'deferred_manifest': deferred_entries,
        'summary': summary,
        'live_tables_mutated': (notes_after != notes_before or props_after != props_before),
        'llm_usage': {
            'calls': int(llm_row[0] or 0),
            'tokens': int(llm_row[1] or 0),
            'cost_estimate': float(llm_row[2] or 0.0),
        },
    }
