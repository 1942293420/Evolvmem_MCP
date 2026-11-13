"""Task 2.7: L1 local embedding cosine deduplication and gated evidence merge."""
from __future__ import annotations

import json
import math
from typing import Any
import uuid

from evolvmem.experience_v2.writer import ExperienceWriter, Op, TicketIssuer

DEFAULT_DEDUPE_THRESHOLD = 0.88
EMBEDDING_DIM = 768


def normalize_vec768(raw: list[float] | tuple[float, ...]) -> list[float]:
    if not raw or len(raw) > EMBEDDING_DIM:
        raise ValueError(f'invalid_embedding_dim:{len(raw) if raw else 0}')
    vec = [float(x) for x in raw]
    if len(vec) < EMBEDDING_DIM:
        vec.extend([0.0] * (EMBEDDING_DIM - len(vec)))
    norm = math.sqrt(sum(v * v for v in vec))
    if not math.isfinite(norm) or norm <= 0.0:
        raise ValueError('zero_or_nonfinite_embedding')
    return [v / norm for v in vec]


def to_unit_vec768_literal(raw: list[float] | tuple[float, ...]) -> str:
    unit = normalize_vec768(raw)
    return '[' + ','.join(f'{v:.8g}' for v in unit) + ']'


def _encode_text(embedder: Any, text: str) -> list[float]:
    if hasattr(embedder, 'initialize'):
        embedder.initialize()
    if hasattr(embedder, 'encode_document'):
        return list(embedder.encode_document(text))
    if hasattr(embedder, 'encode'):
        return list(embedder.encode(text, 'document'))
    if hasattr(embedder, 'embed_texts'):
        return list(embedder.embed_texts([text])[0])
    raise TypeError('unsupported_embedder_interface')


def _resolve_gate_mode(gate_mode: str | dict[str, str], category: str) -> str:
    if isinstance(gate_mode, dict):
        return gate_mode.get(category, 'review')
    return gate_mode or 'review'


def run_l1_dedupe(
    conn,
    proposal_uid: str,
    *,
    embedder: Any,
    ticket_issuer: TicketIssuer,
    writer: ExperienceWriter,
    threshold: float = DEFAULT_DEDUPE_THRESHOLD,
    gate_mode: str | dict[str, str] = 'review',
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT owner_id, category, problem, solution, reusable_experience,
               evidence_json, actor_id, agent_client, model, source_kind
        FROM experience_proposals
        WHERE uid = %s
        FOR UPDATE
        """,
        (proposal_uid,),
    ).fetchone()
    if row is None:
        raise ValueError(f'proposal_not_found: {proposal_uid}')

    (owner_id, category, problem, solution, reusable,
     evidence_raw, actor_id, agent_client, model, source_kind) = row
    evidence = evidence_raw if not isinstance(evidence_raw, str) else json.loads(evidence_raw)
    doc_text = f'{problem}\n{solution}\n{reusable}'
    vec_literal = to_unit_vec768_literal(_encode_text(embedder, doc_text))

    match = conn.execute(
        """
        SELECT id, (1.0 - (embedding <=> %s::vector)) AS cosine_sim
        FROM experience_notes
        WHERE owner_id = %s
          AND category = %s
          AND status = 'active'
          AND deleted_at IS NULL
          AND embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector ASC
        LIMIT 1
        """,
        (vec_literal, owner_id, category, vec_literal),
    ).fetchone()

    best_id = match[0] if match else None
    best_sim = float(match[1]) if (match and match[1] is not None) else 0.0
    decision_id = 'dec-l1-' + uuid.uuid4().hex[:20]

    with conn.transaction():
        conn.execute(
            'UPDATE experience_proposals SET embedding = %s::vector, updated_at = now() WHERE uid = %s',
            (vec_literal, proposal_uid),
        )
        if best_id is not None and best_sim >= float(threshold):
            detail = f'与同类已有经验 {best_id} 余弦相似度 {best_sim:.3f} >= {threshold:.2f}，合并证据'
            conn.execute(
                """
                INSERT INTO proposal_decisions(
                    decision_id, proposal_uid, layer, verdict, reason_code,
                    confidence, judge_model, detail, target_note_id
                ) VALUES (%s, %s, 'L1', 'merge', 'duplicate', %s, 'local-embedder', %s, %s)
                """,
                (decision_id, proposal_uid, best_sim, detail[:200], best_id),
            )
            ticket = ticket_issuer.issue(
                proposal_uid=proposal_uid,
                decision_id=decision_id,
                lane='gated',
            )
            merged_resp = writer.apply(
                conn,
                ticket,
                Op(
                    action='merge_evidence',
                    owner_id=owner_id,
                    actor_id=actor_id,
                    request_id=f'l1-merge:{proposal_uid}',
                    note_id=best_id,
                    evidence=evidence if isinstance(evidence, list) else [],
                    origin='pipeline' if source_kind == 'pipeline' else 'agent',
                    agent_client=agent_client,
                    model=model,
                ),
            )
            conn.execute(
                """
                UPDATE experience_proposals
                SET state = 'merged', merged_into_uid = %s, updated_at = now()
                WHERE uid = %s
                """,
                (best_id, proposal_uid),
            )
            conn.execute(
                """
                INSERT INTO experience_relations(
                    owner_id, from_uid, to_uid, relation, reason, evidence_ref, proposal_uid
                ) VALUES (%s, %s, %s, 'duplicate', %s, %s::jsonb, %s)
                """,
                (
                    owner_id, proposal_uid, best_id, detail[:200],
                    json.dumps(evidence, ensure_ascii=False), proposal_uid,
                ),
            )
            return {
                'decision_id': decision_id,
                'layer': 'L1',
                'verdict': 'merge',
                'reason_code': 'duplicate',
                'confidence': best_sim,
                'target_note_id': best_id,
                'detail': detail[:200],
                'occurrence_count': merged_resp['experience']['occurrence_count'],
            }

        detail = f'L1 未命中同类别高相似经验 (max_sim={best_sim:.3f})'
        conn.execute(
            """
            INSERT INTO proposal_decisions(
                decision_id, proposal_uid, layer, verdict, reason_code,
                confidence, judge_model, detail
            ) VALUES (%s, %s, 'L1', 'pass', NULL, %s, 'local-embedder', %s)
            """,
            (decision_id, proposal_uid, 1.0 - best_sim, detail[:200]),
        )
        mode = _resolve_gate_mode(gate_mode, category)
        if mode in ('review', 'partial', 'auto'):
            conn.execute(
                "UPDATE experience_proposals SET state = 'review', updated_at = now() WHERE uid = %s",
                (proposal_uid,),
            )
        return {
            'decision_id': decision_id,
            'layer': 'L1',
            'verdict': 'pass',
            'reason_code': None,
            'confidence': 1.0 - best_sim,
            'target_note_id': None,
            'detail': detail[:200],
        }


def assess_l1_shadow(
    conn,
    *,
    owner_id: str,
    category: str,
    problem: str,
    solution: str,
    reusable_experience: str,
    embedder: Any,
    threshold: float = DEFAULT_DEDUPE_THRESHOLD,
) -> dict[str, Any]:
    """Read-only L1 local vector similarity check for shadow runs (0 LLM tokens)."""
    doc_text = f'{problem}\n{solution}\n{reusable_experience}'
    vec_literal = to_unit_vec768_literal(_encode_text(embedder, doc_text))
    match = conn.execute(
        """
        SELECT id, (1.0 - (embedding <=> %s::vector)) AS cosine_sim
        FROM experience_notes
        WHERE owner_id = %s
          AND category = %s
          AND status = 'active'
          AND deleted_at IS NULL
          AND embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector ASC
        LIMIT 1
        """,
        (vec_literal, owner_id, category, vec_literal),
    ).fetchone()
    best_id = match[0] if match else None
    best_sim = float(match[1]) if (match and match[1] is not None) else 0.0
    if best_id is not None and best_sim >= float(threshold):
        return {
            'verdict': 'merge',
            'reason_code': 'duplicate',
            'target_note_id': best_id,
            'similarity': best_sim,
            'embedding': vec_literal,
        }
    return {
        'verdict': 'pass',
        'reason_code': None,
        'target_note_id': None,
        'similarity': best_sim,
        'embedding': vec_literal,
    }
