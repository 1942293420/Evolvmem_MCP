"""Stage 4 Tasks 4.4 & 4.5: Migrate kept legacy experiences to PG via AdmissionTicket & register manifest."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Sequence

from evolvmem.experience_v2.clean.legacy_reader import LegacyExperienceRecord, _single_line_trim
from evolvmem.experience_v2.gate.dedupe import _encode_text, normalize_vec768
from evolvmem.experience_v2.gate.rules import CATEGORIES, L0_LIMITS
from evolvmem.experience_v2.writer import ExperienceWriter, Op, TicketIssuer

_VALID_KINDS = frozenset({'env_fact', 'pitfall', 'procedure', 'preference', 'decision', 'habit'})
_RESCUE_16405_RULE = '迁移或重构产品费用计算逻辑时，未显式变更的兜底字段应沿用原脚本参考字段口径。'


def legacy_pg_note_id(owner_id: str, legacy_id: str) -> str:
    """Deterministic PG experience_notes id matching exp-[0-9a-z]{26}."""
    digest = hashlib.sha256(f'stage4-legacy:{owner_id}:{legacy_id}'.encode('utf-8')).hexdigest()
    return f'exp-{digest[:26]}'


def _det_id(prefix: str, owner_id: str, legacy_id: str) -> str:
    digest = hashlib.sha256(f'{prefix}:{owner_id}:{legacy_id}'.encode('utf-8')).hexdigest()
    return f'{prefix}-{digest[:26]}'


def _parse_iso_or_none(raw: str | None) -> datetime | None:
    text = str(raw or '').strip()
    if not text:
        return None
    norm = text[:-1] + '+00:00' if text.endswith(('Z', 'z')) else text
    try:
        dt = datetime.fromisoformat(norm)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def prepare_legacy_keep_units(
    raw_records_by_id: dict[str, LegacyExperienceRecord],
    keep_manifest: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve kept legacy items into distinct PG units, merging fk-inherit child provenance into parent."""
    keep_ids = {str(x['legacy_id']) for x in keep_manifest}
    children_by_parent: dict[str, list[LegacyExperienceRecord]] = {}
    primary_entries: list[dict[str, Any]] = []

    for entry in keep_manifest:
        lid = str(entry['legacy_id'])
        rec = raw_records_by_id[lid]
        parent_lid = f'context_items:{rec.raw_pk}'
        if rec.source_table == 'learning_memories' and rec.linked_content_type == 'experience' and parent_lid in keep_ids:
            children_by_parent.setdefault(parent_lid, []).append(rec)
            continue
        primary_entries.append(dict(entry))

    units: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in primary_entries:
        lid = str(entry['legacy_id'])
        if lid in seen:
            continue
        seen.add(lid)
        rec = raw_records_by_id[lid]
        cat = rec.mapped_category if rec.mapped_category in CATEGORIES else '业务经验'
        kind = rec.mapped_kind if rec.mapped_kind in _VALID_KINDS else 'procedure'
        problem = _single_line_trim(rec.problem, L0_LIMITS['problem']) or _single_line_trim(rec.solution, 120)
        solution = _single_line_trim(rec.solution, L0_LIMITS['solution']) or problem
        reusable = _single_line_trim(rec.reusable_experience, L0_LIMITS['reusable_experience'])
        source_ref = rec.source_ref

        if lid == 'learning_memories:16405' and '未经用户确认' in reusable:
            reusable = _RESCUE_16405_RULE

        ev_list: list[dict[str, Any]] = [
            {
                'kind': 'legacy_migration',
                'legacy_id': rec.legacy_id,
                'source_table': rec.source_table,
                'source_ref': source_ref,
                'updated_at': rec.updated_at,
                'gate_layer': str(entry.get('gate_layer') or ''),
                'reason_code': str(entry.get('reason_code') or ''),
            }
        ]

        for child in children_by_parent.get(lid, ()):
            c_reus = _single_line_trim(child.reusable_experience, L0_LIMITS['reusable_experience'])
            if c_reus and (
                not reusable
                or reusable.startswith('experience:')
                or reusable.endswith(('…', '.'))
            ):
                reusable = c_reus
            if not source_ref and child.source_ref:
                source_ref = child.source_ref
            ev_list.append(
                {
                    'kind': 'legacy_migration_merged_child',
                    'merged_child_legacy_id': child.legacy_id,
                    'source_table': child.source_table,
                    'source_ref': child.source_ref,
                    'updated_at': child.updated_at,
                }
            )

        if not reusable:
            reusable = _single_line_trim(solution, L0_LIMITS['reusable_experience'])

        scope = {
            'origin_stage': 'stage4_legacy_clean',
            'legacy_id': rec.legacy_id,
            'source_table': rec.source_table,
        }
        if rec.project:
            scope['project'] = rec.project
        if source_ref:
            scope['source_ref'] = source_ref

        units.append(
            {
                'legacy_id': rec.legacy_id,
                'source_table': rec.source_table,
                'category': cat,
                'kind': kind,
                'problem': problem,
                'solution': solution,
                'reusable_experience': reusable,
                'misbelief': _single_line_trim(rec.misbelief, 200),
                'scope': scope,
                'evidence': ev_list,
                'updated_at': rec.updated_at,
            }
        )
    return units


def _encode_units(
    units: Sequence[dict[str, Any]],
    *,
    embedder: Any,
    batch_encode_fn: Callable[[list[str]], list[list[float]]] | None,
) -> list[list[float]]:
    if not units:
        return []
    texts = [f"{u['problem']}\n{u['solution']}\n{u['reusable_experience']}" for u in units]
    if batch_encode_fn is not None:
        vecs = batch_encode_fn(texts)
    elif embedder is not None:
        vecs = [_encode_text(embedder, t) for t in texts]
    else:
        raise ValueError('embedder_or_batch_encode_fn_required')
    if len(vecs) != len(units):
        raise ValueError(f'embedding_count_mismatch:{len(vecs)}!={len(units)}')
    return [normalize_vec768(v) for v in vecs]


def migrate_kept_legacy_to_pg(
    conn,
    *,
    units: Sequence[dict[str, Any]],
    owner_id: str,
    ticket_secret: bytes | str,
    embedder: Any = None,
    batch_encode_fn: Callable[[list[str]], list[list[float]]] | None = None,
) -> dict[str, Any]:
    """Write distinct legacy keep units to PG experience_notes via gated AdmissionTicket."""
    existing_ids = {
        str(r[0])
        for r in conn.execute(
            "SELECT id FROM experience_notes WHERE owner_id = %s AND origin = 'legacy'",
            (owner_id,),
        ).fetchall()
    }
    to_insert: list[tuple[str, dict[str, Any]]] = []
    already_present = 0
    for u in units:
        nid = legacy_pg_note_id(owner_id, str(u['legacy_id']))
        if nid in existing_ids:
            already_present += 1
        else:
            to_insert.append((nid, u))

    vecs = _encode_units([u for _, u in to_insert], embedder=embedder, batch_encode_fn=batch_encode_fn)
    issuer = TicketIssuer(ticket_secret, gate_id='stage4-clean-gate')
    writer = ExperienceWriter(ticket_secret)
    migrated = 0

    for (nid, u), vec in zip(to_insert, vecs):
        lid = str(u['legacy_id'])
        prop_uid = _det_id('prop', owner_id, lid)
        dec_id = _det_id('dec', owner_id, lid)
        vec_lit = '[' + ','.join(f'{x:.8g}' for x in vec) + ']'
        payload_obj = {
            'category': u['category'],
            'kind': u['kind'],
            'problem': u['problem'],
            'solution': u['solution'],
            'reusable_experience': u['reusable_experience'],
            'misbelief': u['misbelief'],
            'scope': u['scope'],
        }
        ts = _parse_iso_or_none(u.get('updated_at'))
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO experience_proposals(
                    uid, owner_id, op, category, kind, problem, solution, reusable_experience,
                    misbelief, scope, payload_json, evidence_json, source_kind, source_track,
                    agent_client, model, actor_id, request_id, state, embedding
                ) VALUES (
                    %s, %s, 'add', %s, %s, %s, %s, %s,
                    %s, %s::jsonb, %s::jsonb, %s::jsonb, 'legacy', 'backfill',
                    'stage4-cleaner', 'stage4-clean-v1', %s, %s, 'accepted', %s::vector
                )
                ON CONFLICT (owner_id, request_id) DO UPDATE SET
                    state = 'accepted',
                    updated_at = now()
                """,
                (
                    prop_uid, owner_id, u['category'], u['kind'], u['problem'], u['solution'], u['reusable_experience'],
                    u['misbelief'], json.dumps(u['scope'], ensure_ascii=False),
                    json.dumps(payload_obj, ensure_ascii=False), json.dumps(u['evidence'], ensure_ascii=False),
                    owner_id, f'stage4-legacy-prop:{lid}', vec_lit,
                ),
            )
            conn.execute(
                """
                INSERT INTO proposal_decisions(
                    decision_id, proposal_uid, layer, verdict, reason_code, confidence,
                    judge_model, detail, target_note_id
                ) VALUES (%s, %s, 'L4', 'pass', 'stage4_user_approved', 1.0, 'user-gate', %s, %s)
                ON CONFLICT (decision_id) DO NOTHING
                """,
                (dec_id, prop_uid, 'Approved via Stage 4 delete-sample-50 gate', nid),
            )
            ticket = issuer.issue(proposal_uid=prop_uid, decision_id=dec_id, lane='gated')
            writer.apply(
                conn,
                ticket,
                Op(
                    action='create',
                    owner_id=owner_id,
                    actor_id=owner_id,
                    request_id=f'stage4-migrate-legacy:{lid}',
                    note_id=nid,
                    category=u['category'],
                    problem=u['problem'],
                    solution=u['solution'],
                    reusable_experience=u['reusable_experience'],
                    origin='legacy',
                    kind=u['kind'],
                    misbelief=u['misbelief'],
                    scope=u['scope'],
                    evidence=u['evidence'],
                    importance=5,
                    pinned=False,
                    status='active',
                    embedding=vec,
                    agent_client='stage4-cleaner',
                    model='stage4-clean-v1',
                    created_at=ts,
                    updated_at=ts,
                ),
            )
        migrated += 1

    active_total = int(
        conn.execute(
            "SELECT COUNT(*) FROM experience_notes WHERE owner_id = %s AND origin = 'legacy' AND deleted_at IS NULL AND status = 'active'",
            (owner_id,),
        ).fetchone()[0]
    )
    return {
        'input_units': len(units),
        'migrated': migrated,
        'already_present': already_present,
        'pg_legacy_active_total': active_total,
    }


def register_legacy_cleanup_manifest(
    conn,
    *,
    cleanup_id: str,
    owner_id: str,
    approved_by: str,
    approved_at: datetime,
    backup_receipt: dict[str, Any],
    delete_summary: dict[str, Any],
    migrate_summary: dict[str, Any],
    rescued_ids: Sequence[str],
    scrubbed_backup_label: str,
    manifest_out_path: Path | str | None = None,
) -> dict[str, Any]:
    """Task 4.5: Record user authorization, 30-day backup path, and table dispositions in PG & manifest JSON."""
    auth_note = f'用户授权删除，备份位置 {scrubbed_backup_label}'
    dispositions = {
        'context_items_experience': 'cleaned_and_kept_migrated_to_pg_legacy',
        'capability_memories': 'cleaned_and_kept_migrated_to_pg_legacy',
        'learning_memories': 'cleaned_and_kept_migrated_to_pg_legacy',
        'memories': '停用但保留',
        'history_tables': '未触碰 (D7)',
        'vectors_usearch': '未触碰 (vectors_usearch_untouched)',
    }
    manifest_obj = {
        'cleanup_id': cleanup_id,
        'owner_id': owner_id,
        'approved_by': approved_by,
        'approved_at': approved_at.astimezone(timezone.utc).isoformat(),
        'authorization_note': auth_note,
        'backup_archive_label': scrubbed_backup_label,
        'backup_sha256': backup_receipt['sha256'],
        'backup_bytes': int(backup_receipt['bytes']),
        'retain_until': backup_receipt['retain_until'],
        'deleted': delete_summary['deleted'],
        'deleted_total': int(delete_summary['deleted_total']),
        'migrated_keep_total': int(migrate_summary['pg_legacy_active_total']),
        'rescued_ids': list(rescued_ids),
        'table_dispositions': dispositions,
    }
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO legacy_cleanup_registry(
                cleanup_id, owner_id, approved_by, approved_at, authorization_note,
                backup_archive_path, backup_sha256, backup_bytes, retain_until,
                deleted_total, migrated_keep_total, rescued_ids_json,
                table_dispositions_json, manifest_json
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s::jsonb,
                %s::jsonb, %s::jsonb
            )
            ON CONFLICT (cleanup_id) DO UPDATE SET
                authorization_note = EXCLUDED.authorization_note,
                backup_archive_path = EXCLUDED.backup_archive_path,
                backup_sha256 = EXCLUDED.backup_sha256,
                backup_bytes = EXCLUDED.backup_bytes,
                retain_until = EXCLUDED.retain_until,
                deleted_total = EXCLUDED.deleted_total,
                migrated_keep_total = EXCLUDED.migrated_keep_total,
                rescued_ids_json = EXCLUDED.rescued_ids_json,
                table_dispositions_json = EXCLUDED.table_dispositions_json,
                manifest_json = EXCLUDED.manifest_json
            """,
            (
                cleanup_id, owner_id, approved_by, approved_at, auth_note,
                scrubbed_backup_label, backup_receipt['sha256'], int(backup_receipt['bytes']),
                backup_receipt['retain_until'], int(delete_summary['deleted_total']),
                int(migrate_summary['pg_legacy_active_total']), json.dumps(list(rescued_ids)),
                json.dumps(dispositions, ensure_ascii=False), json.dumps(manifest_obj, ensure_ascii=False),
            ),
        )
    if manifest_out_path is not None:
        out_p = Path(manifest_out_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(manifest_obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest_obj
