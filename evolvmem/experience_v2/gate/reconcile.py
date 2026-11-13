"""Task 3.7: L3 6-relation reconciliation, challenger probation, and refuted misbelief."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any, Callable, Literal
import uuid

from evolvmem.experience_v2.control.agent_identity import normalize_model_name
from evolvmem.experience_v2.gate.rules import L0_LIMITS
from evolvmem.experience_v2.llm_meter import BudgetConfig, estimate_tokens, metered_llm
from evolvmem.experience_v2.writer import ExperienceWriter, Op, TicketIssuer

RELATIONS: tuple[str, ...] = ('duplicate', 'refines', 'scope_split', 'outdated', 'refuted', 'contested')
_FENCE_RE = re.compile(r'^\s*```(?:json)?\s*|\s*```\s*\Z', re.IGNORECASE)
_WS_RE = re.compile(r'\s+')

L3_SYSTEM_PROMPT = (
    '你是经验关系对账裁判。对比新提议与候选已有经验，给出唯一关系分类，严禁物理覆盖旧经验。\n'
    '可选 relation：duplicate（同一事实，合并证据）、refines（新条目细化旧条目触发条件）、'
    'scope_split（适用环境/系统分化，双方并存）、outdated（环境或版本演进导致旧条过时）、'
    'refuted（旧条目根本前提错误，须额外生成误区条目）、contested（存在真实未决分歧，保留双方）。\n'
    '仅输出严格单个 JSON 对象：{"relation":"...","target_note_id":"...",'
    '"confidence":0.0~1.0,"reason":"<=120字",'
    '"incumbent_scope":{},"challenger_scope":{},'
    '"misbelief_problem":"<=40字","misbelief_solution":"<=90字",'
    '"misbelief_reusable":"<=50字","misbelief_text":"<=80字"}。'
)


class SelfJudgingForbiddenError(PermissionError):
    """Raised when L3 judge_model matches the proposer model of the candidate."""


def _clip_single_line(text: Any, limit: int) -> str:
    return _WS_RE.sub(' ', str(text or '')).strip()[:limit]


def _parse_l3_json(raw_text: str) -> dict[str, Any]:
    cleaned = _FENCE_RE.sub('', raw_text or '').strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        s, e = cleaned.find('{'), cleaned.rfind('}')
        if s != -1 and e > s:
            try:
                parsed = json.loads(cleaned[s : e + 1])
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass
    return {}


def _fetch_candidates(conn, *, owner_id: str, category: str, embedding: Any, limit: int = 5) -> list[dict[str, Any]]:
    if embedding is not None:
        rows = conn.execute(
            """
            SELECT id, category, problem, solution, reusable_experience, scope, importance, pinned, status
            FROM experience_notes
            WHERE owner_id = %s AND status IN ('active', 'contested', 'challenger') AND deleted_at IS NULL
            ORDER BY (embedding IS NULL) ASC, embedding <=> %s::vector ASC, updated_at DESC
            LIMIT %s
            """,
            (owner_id, str(embedding), limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, category, problem, solution, reusable_experience, scope, importance, pinned, status
            FROM experience_notes
            WHERE owner_id = %s AND status IN ('active', 'contested', 'challenger') AND deleted_at IS NULL
            ORDER BY (category = %s) DESC, updated_at DESC
            LIMIT %s
            """,
            (owner_id, category, limit),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            'id': r[0],
            'category': r[1],
            'problem': r[2],
            'solution': r[3],
            'reusable_experience': r[4],
            'scope': r[5] if isinstance(r[5], dict) else json.loads(r[5] or '{}'),
            'importance': int(r[6]),
            'pinned': bool(r[7]),
            'status': r[8],
        })
    return out


def assess_l3_shadow(
    conn,
    *,
    owner_id: str,
    proposer_model: str,
    judge_model: str,
    judge_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]],
    problem: str,
    solution: str,
    reusable_experience: str,
    category: str,
    scope: dict[str, Any] | None = None,
    embedding: Any = None,
    track: Literal['live', 'backfill'] = 'live',
    session_id: str | None = None,
    proposal_uid: str | None = None,
    is_shadow: bool = True,
    budget: BudgetConfig | None = None,
) -> dict[str, Any]:
    """Read-only L3 classification against active notes, enforcing judge != proposer."""
    if normalize_model_name(judge_model) == normalize_model_name(proposer_model):
        raise SelfJudgingForbiddenError(f'judge_model {judge_model!r} cannot equal proposer_model {proposer_model!r}')
    if estimate_tokens(L3_SYSTEM_PROMPT) > 600:
        raise ValueError('l3_static_prompt_exceeds_600_tokens')

    candidates = _fetch_candidates(conn, owner_id=owner_id, category=category, embedding=embedding)
    if not candidates:
        return {
            'relation': None,
            'target_note_id': None,
            'confidence': 1.0,
            'reason': '库中暂无可比对经验，直接作为独立候选进入下一阶段',
            'candidates_checked': 0,
            'calls_used': 0,
            'tokens_used': 0,
        }

    cand_lines = [
        f"- id={c['id']} imp={c['importance']} pinned={c['pinned']} scope={json.dumps(c['scope'], ensure_ascii=False)} "
        f"P:{c['problem']} | S:{c['solution']}"
        for c in candidates
    ]
    user_prompt = (
        f"新提议({category}, scope={json.dumps(scope or {}, ensure_ascii=False)}):\n"
        f"P: {problem}\nS: {solution}\nR: {reusable_experience}\n\n"
        "候选旧经验:\n" + '\n'.join(cand_lines)
    )
    res = metered_llm(
        track=track,
        stage='l3_reconcile',
        model=judge_model,
        messages=[{'role': 'system', 'content': L3_SYSTEM_PROMPT}, {'role': 'user', 'content': user_prompt}],
        session_id=session_id,
        max_output_tokens=600,
        conn=conn,
        owner_id=owner_id,
        proposal_uid=proposal_uid,
        is_shadow=is_shadow,
        budget=budget,
        transport=judge_transport,
    )
    obj = _parse_l3_json(res.text)
    rel = str(obj.get('relation') or '').strip()
    if rel not in RELATIONS:
        rel = 'refines'
    valid_ids = {c['id']: c for c in candidates}
    target_id = str(obj.get('target_note_id') or '').strip()
    if target_id not in valid_ids:
        target_id = candidates[0]['id']
    try:
        conf = max(0.0, min(1.0, float(obj.get('confidence', 0.8))))
    except (TypeError, ValueError):
        conf = 0.8
    return {
        'relation': rel,
        'target_note_id': target_id,
        'incumbent': valid_ids[target_id],
        'confidence': conf,
        'reason': _clip_single_line(obj.get('reason') or f'L3 判定为 {rel}', 200),
        'incumbent_scope': obj.get('incumbent_scope') if isinstance(obj.get('incumbent_scope'), dict) else {},
        'challenger_scope': obj.get('challenger_scope') if isinstance(obj.get('challenger_scope'), dict) else (scope or {}),
        'misbelief_problem': _clip_single_line(obj.get('misbelief_problem'), L0_LIMITS['problem']),
        'misbelief_solution': _clip_single_line(obj.get('misbelief_solution'), L0_LIMITS['solution']),
        'misbelief_reusable': _clip_single_line(obj.get('misbelief_reusable'), L0_LIMITS['reusable_experience']),
        'misbelief_text': _clip_single_line(obj.get('misbelief_text'), L0_LIMITS['misbelief']),
        'candidates_checked': len(candidates),
        'calls_used': 1,
        'tokens_used': res.total_tokens,
    }


def _create_note_via_writer(
    conn,
    *,
    writer: ExperienceWriter,
    issuer: TicketIssuer,
    proposal_uid: str,
    decision_id: str,
    row_data: dict[str, Any],
    req_suffix: str,
    status: str = 'active',
    scope_override: dict[str, Any] | None = None,
    valid_from: datetime | None = None,
) -> str:
    ticket = issuer.issue(proposal_uid=proposal_uid, decision_id=decision_id, lane='gated')
    resp = writer.apply(
        conn,
        ticket,
        Op(
            action='create',
            owner_id=row_data['owner_id'],
            actor_id=row_data['actor_id'],
            request_id=f'l3:{proposal_uid}:{req_suffix}',
            category=row_data['category'],
            problem=row_data['problem'],
            solution=row_data['solution'],
            reusable_experience=row_data['reusable_experience'],
            origin='pipeline' if row_data['source_kind'] == 'pipeline' else 'agent',
            kind=row_data['kind'],
            misbelief=row_data['misbelief'],
            scope=scope_override if scope_override is not None else row_data['scope'],
            evidence=row_data['evidence'],
            importance=row_data['importance'],
            pinned=row_data['pinned'],
            status=status,
            valid_from=valid_from,
            agent_client=row_data['agent_client'],
            model=row_data['model'],
            source_device_id=row_data['device_id'],
        ),
    )
    return resp['experience']['id']


def _spawn_refuted_misbelief_proposal(
    conn,
    *,
    proposal_uid: str,
    row_data: dict[str, Any],
    incumbent: dict[str, Any],
    judgment: dict[str, Any],
) -> str:
    prob = judgment['misbelief_problem'] or _clip_single_line(f"勿将 {incumbent['problem']}", L0_LIMITS['problem'])
    sol = judgment['misbelief_solution'] or _clip_single_line(row_data['solution'], L0_LIMITS['solution'])
    reus = judgment['misbelief_reusable'] or _clip_single_line(row_data['reusable_experience'], L0_LIMITS['reusable_experience'])
    mis = judgment['misbelief_text'] or _clip_single_line(f"曾误信：{incumbent['solution']}", L0_LIMITS['misbelief'])
    mis_uid = 'prop-' + uuid.uuid4().hex[:20]
    canonical = json.dumps(
        {
            'op': 'add', 'category': row_data['category'], 'kind': 'pitfall',
            'problem': prob, 'solution': sol, 'reusable_experience': reus,
            'misbelief': mis, 'scope': row_data['scope'], 'evidence': row_data['evidence'],
            'source_track': row_data['source_track'],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    conn.execute(
        """
        INSERT INTO experience_proposals(
            uid, owner_id, op, category, kind, problem, solution, reusable_experience,
            misbelief, scope, payload_json, evidence_json, source_kind, source_track,
            agent_client, model, device_id, actor_id, session_id, request_id, state, importance, pinned
        ) VALUES (
            %s, %s, 'add', %s, 'pitfall', %s, %s, %s,
            %s, %s::jsonb, %s::jsonb, %s::jsonb, 'pipeline', %s,
            %s, %s, %s, %s, %s, %s, 'review', %s, false
        ) ON CONFLICT (owner_id, request_id) DO NOTHING
        """,
        (
            mis_uid, row_data['owner_id'], row_data['category'], prob, sol, reus,
            mis, json.dumps(row_data['scope'], ensure_ascii=False), canonical,
            json.dumps(row_data['evidence'], ensure_ascii=False), row_data['source_track'],
            row_data['agent_client'], row_data['model'], row_data['device_id'], row_data['actor_id'],
            row_data['session_id'], f'l3-misbelief:{proposal_uid}', max(6, row_data['importance']),
        ),
    )
    return mis_uid


def run_l3_reconcile_gate(
    conn,
    proposal_uid: str,
    *,
    judge_model: str,
    judge_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]],
    writer: ExperienceWriter,
    ticket_issuer: TicketIssuer,
    gate_mode: str = 'review',
    user_confirmed: bool = False,
    required_successes: int = 2,
    budget: BudgetConfig | None = None,
) -> dict[str, Any]:
    """Reconcile a passed L2 proposal against existing notes with 6 relations & challenger probation."""
    row = conn.execute(
        """
        SELECT owner_id, actor_id, category, kind, problem, solution, reusable_experience,
               misbelief, scope, evidence_json, source_kind, source_track,
               agent_client, model, device_id, session_id, importance, pinned, embedding
        FROM experience_proposals
        WHERE uid = %s
        FOR UPDATE
        """,
        (proposal_uid,),
    ).fetchone()
    if row is None:
        raise ValueError(f'proposal_not_found: {proposal_uid}')

    row_data = {
        'owner_id': row[0], 'actor_id': row[1], 'category': row[2], 'kind': row[3],
        'problem': row[4], 'solution': row[5], 'reusable_experience': row[6],
        'misbelief': row[7],
        'scope': row[8] if isinstance(row[8], dict) else json.loads(row[8] or '{}'),
        'evidence': row[9] if isinstance(row[9], list) else json.loads(row[9] or '[]'),
        'source_kind': row[10], 'source_track': row[11],
        'agent_client': row[12], 'model': row[13], 'device_id': row[14],
        'session_id': row[15], 'importance': int(row[16]), 'pinned': bool(row[17]),
        'embedding': row[18],
    }

    judgment = assess_l3_shadow(
        conn,
        owner_id=row_data['owner_id'],
        proposer_model=row_data['model'],
        judge_model=judge_model,
        judge_transport=judge_transport,
        problem=row_data['problem'],
        solution=row_data['solution'],
        reusable_experience=row_data['reusable_experience'],
        category=row_data['category'],
        scope=row_data['scope'],
        embedding=row_data['embedding'],
        track=row_data['source_track'],
        session_id=row_data['session_id'],
        proposal_uid=proposal_uid,
        is_shadow=False,
        budget=budget,
    )

    rel = judgment['relation']
    decision_id = 'dec-l3-' + uuid.uuid4().hex[:20]
    verdict = 'merge' if rel == 'duplicate' else 'pass'
    reason_code = 'duplicate' if rel == 'duplicate' else None
    new_note_id: str | None = None
    misbelief_uid: str | None = None
    trial_id: int | None = None
    now = datetime.now(timezone.utc)
    origin = 'pipeline' if row_data['source_kind'] == 'pipeline' else 'agent'

    with conn.transaction():
        conn.execute(
            """
            INSERT INTO proposal_decisions(
                decision_id, proposal_uid, layer, verdict, reason_code,
                confidence, judge_model, detail, target_note_id
            ) VALUES (%s, %s, 'L3', %s, %s, %s, %s, %s, %s)
            """,
            (
                decision_id, proposal_uid, verdict, reason_code, judgment['confidence'],
                judge_model if rel else 'rule-no-candidates', judgment['reason'][:200], judgment['target_note_id'],
            ),
        )

        if rel is None:
            if gate_mode == 'auto':
                new_note_id = _create_note_via_writer(
                    conn, writer=writer, issuer=ticket_issuer, proposal_uid=proposal_uid,
                    decision_id=decision_id, row_data=row_data, req_suffix='create', valid_from=now,
                )
                conn.execute("UPDATE experience_proposals SET state = 'accepted', updated_at = now() WHERE uid = %s", (proposal_uid,))
            else:
                conn.execute("UPDATE experience_proposals SET state = 'review', updated_at = now() WHERE uid = %s", (proposal_uid,))
            return {'decision_id': decision_id, 'layer': 'L3', 'verdict': 'pass', 'relation': None, 'new_note_id': new_note_id}

        inc = judgment['incumbent']
        inc_id = inc['id']
        needs_probation = (inc['importance'] >= 8 or inc['pinned']) and not user_confirmed

        if gate_mode == 'auto':
            ticket = ticket_issuer.issue(proposal_uid=proposal_uid, decision_id=decision_id, lane='gated')
            if rel == 'duplicate':
                writer.apply(
                    conn,
                    ticket,
                    Op(
                        action='merge_evidence',
                        owner_id=row_data['owner_id'],
                        actor_id=row_data['actor_id'],
                        request_id=f'l3:{proposal_uid}:dup',
                        note_id=inc_id,
                        evidence=row_data['evidence'],
                        origin=origin,
                        agent_client=row_data['agent_client'],
                        model=row_data['model'],
                    ),
                )
                conn.execute("UPDATE experience_proposals SET state = 'merged', merged_into_uid = %s, updated_at = now() WHERE uid = %s", (inc_id, proposal_uid))
            elif rel == 'refines':
                new_note_id = _create_note_via_writer(
                    conn, writer=writer, issuer=ticket_issuer, proposal_uid=proposal_uid,
                    decision_id=decision_id, row_data=row_data, req_suffix='refines', valid_from=now,
                )
                conn.execute("UPDATE experience_proposals SET state = 'accepted', updated_at = now() WHERE uid = %s", (proposal_uid,))
            elif rel == 'scope_split':
                if needs_probation:
                    new_note_id = _create_note_via_writer(
                        conn, writer=writer, issuer=ticket_issuer, proposal_uid=proposal_uid,
                        decision_id=decision_id, row_data=row_data, req_suffix='split_ch', status='challenger',
                        scope_override=judgment['challenger_scope'], valid_from=now,
                    )
                    trial_id = conn.execute(
                        "INSERT INTO challenger_trials(owner_id, challenger_note_id, incumbent_note_id, relation, proposal_uid, required_successes) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                        (row_data['owner_id'], new_note_id, inc_id, rel, proposal_uid, required_successes),
                    ).fetchone()[0]
                else:
                    if judgment['incumbent_scope']:
                        writer.apply(
                            conn,
                            ticket,
                            Op(
                                action='reconcile_state',
                                owner_id=row_data['owner_id'],
                                actor_id=row_data['actor_id'],
                                request_id=f'l3:{proposal_uid}:inc_scope',
                                note_id=inc_id,
                                status='active',
                                scope=judgment['incumbent_scope'],
                                origin=origin,
                            ),
                        )
                    new_note_id = _create_note_via_writer(
                        conn, writer=writer, issuer=ticket_issuer, proposal_uid=proposal_uid,
                        decision_id=decision_id, row_data=row_data, req_suffix='split',
                        scope_override=judgment['challenger_scope'], valid_from=now,
                    )
                conn.execute("UPDATE experience_proposals SET state = 'accepted', updated_at = now() WHERE uid = %s", (proposal_uid,))
            elif rel in ('outdated', 'refuted'):
                if rel == 'refuted':
                    misbelief_uid = _spawn_refuted_misbelief_proposal(conn, proposal_uid=proposal_uid, row_data=row_data, incumbent=inc, judgment=judgment)
                if needs_probation:
                    new_note_id = _create_note_via_writer(
                        conn, writer=writer, issuer=ticket_issuer, proposal_uid=proposal_uid,
                        decision_id=decision_id, row_data=row_data, req_suffix=f'{rel}_ch', status='challenger', valid_from=now,
                    )
                    trial_id = conn.execute(
                        "INSERT INTO challenger_trials(owner_id, challenger_note_id, incumbent_note_id, relation, proposal_uid, required_successes) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                        (row_data['owner_id'], new_note_id, inc_id, rel, proposal_uid, required_successes),
                    ).fetchone()[0]
                else:
                    writer.apply(
                        conn,
                        ticket,
                        Op(
                            action='reconcile_state',
                            owner_id=row_data['owner_id'],
                            actor_id=row_data['actor_id'],
                            request_id=f'l3:{proposal_uid}:retire_inc',
                            note_id=inc_id,
                            status='retired',
                            valid_until=now,
                            origin=origin,
                        ),
                    )
                    new_note_id = _create_note_via_writer(
                        conn, writer=writer, issuer=ticket_issuer, proposal_uid=proposal_uid,
                        decision_id=decision_id, row_data=row_data, req_suffix=rel, valid_from=now,
                    )
                conn.execute("UPDATE experience_proposals SET state = 'accepted', updated_at = now() WHERE uid = %s", (proposal_uid,))
            elif rel == 'contested':
                if not inc['pinned']:
                    writer.apply(
                        conn,
                        ticket,
                        Op(
                            action='reconcile_state',
                            owner_id=row_data['owner_id'],
                            actor_id=row_data['actor_id'],
                            request_id=f'l3:{proposal_uid}:contested_inc',
                            note_id=inc_id,
                            status='contested',
                            origin=origin,
                        ),
                    )
                new_note_id = _create_note_via_writer(
                    conn, writer=writer, issuer=ticket_issuer, proposal_uid=proposal_uid,
                    decision_id=decision_id, row_data=row_data, req_suffix='contested', status='contested', valid_from=now,
                )
                conn.execute("UPDATE experience_proposals SET state = 'accepted', updated_at = now() WHERE uid = %s", (proposal_uid,))
        else:
            if rel == 'refuted':
                misbelief_uid = _spawn_refuted_misbelief_proposal(conn, proposal_uid=proposal_uid, row_data=row_data, incumbent=inc, judgment=judgment)
            conn.execute("UPDATE experience_proposals SET state = 'review', updated_at = now() WHERE uid = %s", (proposal_uid,))

        conn.execute(
            """
            INSERT INTO experience_relations(
                owner_id, from_uid, to_uid, relation, reason, evidence_ref, proposal_uid
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                row_data['owner_id'], new_note_id or proposal_uid, inc_id, rel,
                judgment['reason'][:200], json.dumps(row_data['evidence'], ensure_ascii=False), proposal_uid,
            ),
        )

    return {
        'decision_id': decision_id,
        'layer': 'L3',
        'verdict': verdict,
        'relation': rel,
        'target_note_id': inc_id,
        'new_note_id': new_note_id,
        'misbelief_proposal_uid': misbelief_uid,
        'trial_id': trial_id,
        'needs_probation': needs_probation,
        'calls_used': judgment['calls_used'],
        'tokens_used': judgment['tokens_used'],
    }


def record_challenger_outcome(
    conn,
    *,
    owner_id: str,
    challenger_note_id: str,
    succeeded: bool,
    writer: ExperienceWriter,
    ticket_issuer: TicketIssuer,
) -> dict[str, Any]:
    """Update challenger probation count: promote after k successes, auto-rollback on failure."""
    with conn.transaction():
        row = conn.execute(
            """
            SELECT id, incumbent_note_id, relation, proposal_uid, required_successes, success_count, failure_count
            FROM challenger_trials
            WHERE owner_id = %s AND challenger_note_id = %s AND status = 'trial'
            FOR UPDATE
            """,
            (owner_id, challenger_note_id),
        ).fetchone()
        if row is None:
            raise ValueError(f'active_challenger_trial_not_found: {challenger_note_id}')
        trial_id, inc_id, rel, prop_uid, req_succ, succ_cnt, fail_cnt = row

        dec_row = conn.execute(
            "SELECT decision_id FROM proposal_decisions WHERE proposal_uid = %s AND verdict IN ('pass', 'merge') ORDER BY created_at DESC LIMIT 1",
            (prop_uid,),
        ).fetchone()
        if not dec_row:
            raise ValueError(f'missing_decision_for_trial_proposal: {prop_uid}')
        ticket = ticket_issuer.issue(proposal_uid=prop_uid, decision_id=dec_row[0], lane='gated')
        now = datetime.now(timezone.utc)

        if succeeded:
            new_succ = int(succ_cnt) + 1
            if new_succ >= int(req_succ):
                writer.apply(
                    conn, ticket,
                    Op(action='reconcile_state', owner_id=owner_id, actor_id='challenger-probation', request_id=f'ch-promote:{trial_id}', note_id=challenger_note_id, status='active', valid_from=now, origin='pipeline'),
                )
                writer.apply(
                    conn, ticket,
                    Op(action='reconcile_state', owner_id=owner_id, actor_id='challenger-probation', request_id=f'ch-retire-inc:{trial_id}', note_id=inc_id, status='retired', valid_until=now, origin='pipeline'),
                )
                conn.execute("UPDATE challenger_trials SET success_count = %s, status = 'promoted', updated_at = now() WHERE id = %s", (new_succ, trial_id))
                return {'trial_id': trial_id, 'trial_status': 'promoted', 'success_count': new_succ, 'failure_count': int(fail_cnt)}
            conn.execute("UPDATE challenger_trials SET success_count = %s, updated_at = now() WHERE id = %s", (new_succ, trial_id))
            return {'trial_id': trial_id, 'trial_status': 'trial', 'success_count': new_succ, 'failure_count': int(fail_cnt)}

        new_fail = int(fail_cnt) + 1
        writer.apply(
            conn, ticket,
            Op(action='reconcile_state', owner_id=owner_id, actor_id='challenger-probation', request_id=f'ch-rollback:{trial_id}', note_id=challenger_note_id, status='retired', valid_until=now, origin='pipeline'),
        )
        writer.apply(
            conn, ticket,
            Op(action='reconcile_state', owner_id=owner_id, actor_id='challenger-probation', request_id=f'ch-restore-inc:{trial_id}', note_id=inc_id, status='active', origin='pipeline'),
        )
        conn.execute("UPDATE challenger_trials SET failure_count = %s, status = 'rolled_back', updated_at = now() WHERE id = %s", (new_fail, trial_id))
        return {'trial_id': trial_id, 'trial_status': 'rolled_back', 'success_count': int(succ_cnt), 'failure_count': new_fail}
