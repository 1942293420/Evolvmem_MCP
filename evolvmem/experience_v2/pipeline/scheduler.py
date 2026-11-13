"""Task 3.8: Stage 3 pipeline scheduler, R6 idempotency, R7 queueing, and R9 circuit breaker."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Callable, Literal, Sequence

from evolvmem.experience_v2.control.agent_identity import AgentAttribution
from evolvmem.experience_v2.control.reputation_policy import TrustedChannel
from evolvmem.experience_v2.gate.dedupe import assess_l1_shadow, run_l1_dedupe
from evolvmem.experience_v2.gate.propose import ProposalPolicyError, ProposalService
from evolvmem.experience_v2.gate.reconcile import assess_l3_shadow, run_l3_reconcile_gate
from evolvmem.experience_v2.gate.rules import evaluate_l0_fields, run_l0_gate
from evolvmem.experience_v2.gate.value import assess_value_cascade, run_l2_value_gate
from evolvmem.experience_v2.llm_meter import BudgetConfig, BudgetDeferred
from evolvmem.experience_v2.pipeline.events import ParsedSessionEvents, parse_session_events
from evolvmem.experience_v2.pipeline.extract import HistoryReaderEvidenceVerifier, extract_session_moments
from evolvmem.experience_v2.pipeline.history_reader import HistoryReader, HistorySession
from evolvmem.experience_v2.pipeline.moments import detect_key_moments
from evolvmem.experience_v2.writer import ExperienceWriter, TicketIssuer
from evolvmem.product_policy import ProductPolicy

IDLE_TIMEOUT_SECONDS = 30 * 60
DEFAULT_PIPELINE_VERSION = 'stage3-v1'
_REQ_SAFE_RE = re.compile(r'[^A-Za-z0-9._:-]')


def should_trigger_session(
    *,
    is_closed: bool,
    last_message_at: datetime,
    now: datetime,
    idle_timeout_seconds: int = IDLE_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """Trigger when session closes OR idle time reaches >= 30 minutes."""
    if is_closed:
        return True, 'session_closed'
    elapsed = (now - last_message_at).total_seconds()
    if elapsed >= idle_timeout_seconds:
        return True, 'idle_30m'
    return False, 'active_wait'


def evaluate_circuit_breaker(
    conn,
    *,
    owner_id: str,
    track: Literal['live', 'backfill'] = 'live',
    daily_token_per_note: Sequence[tuple[str, int, int]] | None = None,
    target_tokens_per_note: int = 30_000,
    min_pass_rate: float = 0.30,
    min_review_samples: int = 5,
) -> dict[str, Any]:
    """Enforce R9: pause pipeline on 3 bad cost days (>2x target) or <30% review pass rate."""
    series = list(daily_token_per_note or [])
    if not series and daily_token_per_note is None:
        tok_rows = conn.execute(
            """
            SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day,
                   COALESCE(SUM(input_tokens + output_tokens), 0) AS toks
            FROM llm_calls
            WHERE track = %s AND is_shadow = false
            GROUP BY 1 ORDER BY 1 ASC
            """,
            (track,),
        ).fetchall()
        note_rows = conn.execute(
            """
            SELECT to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day, COUNT(*)
            FROM experience_events
            WHERE owner_id = %s AND action = 'create'
            GROUP BY 1
            """,
            (owner_id,),
        ).fetchall()
        notes_by_day = {r[0]: int(r[1]) for r in note_rows}
        series = [(r[0], int(r[1]), notes_by_day.get(r[0], 0)) for r in tok_rows]

    threshold = target_tokens_per_note * 2
    consecutive_bad = 0
    for _day, toks, notes in series:
        per_note = float(toks) / max(1, int(notes))
        if int(toks) > 0 and per_note > threshold:
            consecutive_bad += 1
        else:
            consecutive_bad = 0

    rev_row = conn.execute(
        """
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE user_verdict IN ('accept', 'edit_accept', 'rescue'))
        FROM review_decisions
        WHERE owner_id = %s AND source_track = %s
        """,
        (owner_id, track),
    ).fetchone()
    total_rev = int(rev_row[0] or 0) if rev_row else 0
    admitted_rev = int(rev_row[1] or 0) if rev_row else 0
    pass_rate = (admitted_rev / total_rev) if total_rev > 0 else None

    alerts: list[dict[str, str]] = []
    if consecutive_bad >= 3:
        alerts.append({'reason': 'token_cost_exceeded_3d', 'message': f'连续 {consecutive_bad} 天单条经验消耗超过 {threshold} tokens'})
    if total_rev >= min_review_samples and pass_rate is not None and pass_rate < min_pass_rate:
        alerts.append({'reason': 'low_review_pass_rate', 'message': f'审阅通过率 {pass_rate:.1%} 低于 {min_pass_rate:.0%}'})

    paused = bool(alerts)
    pause_reason = '; '.join(a['reason'] for a in alerts)
    metrics = {'consecutive_bad_days': consecutive_bad, 'total_reviews': total_rev, 'pass_rate': pass_rate, 'alerts': alerts}
    conn.execute(
        """
        INSERT INTO pipeline_circuit_state(
            owner_id, track, paused, pause_reason, consecutive_bad_days, metrics_json, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, now())
        ON CONFLICT (owner_id, track) DO UPDATE SET
            paused = EXCLUDED.paused,
            pause_reason = EXCLUDED.pause_reason,
            consecutive_bad_days = EXCLUDED.consecutive_bad_days,
            metrics_json = EXCLUDED.metrics_json,
            updated_at = now()
        """,
        (owner_id, track, paused, pause_reason, consecutive_bad, json.dumps(metrics, ensure_ascii=False)),
    )
    return {'paused': paused, 'pause_reason': pause_reason, 'consecutive_bad_days': consecutive_bad, 'pass_rate': pass_rate, 'alerts': alerts}


def _slice_delta_parsed(parsed: ParsedSessionEvents, prev_last_idx: int) -> tuple[ParsedSessionEvents | None, int]:
    new_events = tuple(e for e in parsed.events if int(e.get('index', 0)) > prev_last_idx)
    if not new_events:
        return None, prev_last_idx
    end_idx = max(int(e.get('index', 0)) for e in new_events)
    delta = ParsedSessionEvents(
        session_id=parsed.session_id,
        adapter=parsed.adapter,
        source_format=parsed.source_format,
        project=parsed.project,
        source_ref=parsed.source_ref,
        events=new_events,
        tool_outcomes=tuple(o for o in parsed.tool_outcomes if o.event_index > prev_last_idx),
        file_edits=tuple(f for f in parsed.file_edits if f.event_index > prev_last_idx),
        rollbacks=parsed.rollbacks if prev_last_idx < 0 else (),
        retries=tuple(r for r in parsed.retries if any(i > prev_last_idx for i in r.event_indices)),
        user_intents=tuple(u for u in parsed.user_intents if u.event_index > prev_last_idx),
        evidence_pointers=parsed.evidence_pointers,
        llm_calls_used=0,
    )
    return delta, end_idx


def _upsert_segment_run(
    conn,
    *,
    owner_id: str,
    track: str,
    session_id: str,
    segment_hash: str,
    pipeline_version: str,
    status: str,
    moments_count: int = 0,
    proposals_count: int = 0,
    tokens_spent: int = 0,
    last_event_index: int = 0,
    error_code: str = '',
    is_shadow: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO pipeline_segment_runs(
            owner_id, track, session_id, segment_hash, pipeline_version,
            status, moments_count, proposals_count, tokens_spent, last_event_index,
            error_code, is_shadow, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (owner_id, track, session_id, segment_hash, pipeline_version)
        DO UPDATE SET
            status = EXCLUDED.status,
            moments_count = EXCLUDED.moments_count,
            proposals_count = EXCLUDED.proposals_count,
            tokens_spent = EXCLUDED.tokens_spent,
            last_event_index = EXCLUDED.last_event_index,
            error_code = EXCLUDED.error_code,
            is_shadow = EXCLUDED.is_shadow,
            updated_at = now()
        """,
        (
            owner_id, track, session_id, segment_hash, pipeline_version,
            status, moments_count, proposals_count, tokens_spent, last_event_index,
            error_code, is_shadow,
        ),
    )


class PipelineScheduler:
    """Orchestrates Session -> Moments -> Extract -> L0 -> L1 -> L2 -> L3 under R1-R9."""

    def __init__(
        self,
        *,
        history_reader: HistoryReader,
        policy: ProductPolicy,
        owner_id: str,
        flash_model: str,
        strong_model: str,
        judge_model: str,
        flash_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]],
        strong_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]] | None = None,
        judge_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]] | None = None,
        embedder: Any = None,
        pipeline_version: str = DEFAULT_PIPELINE_VERSION,
        track: Literal['live', 'backfill'] = 'live',
        ticket_secret: bytes | str = b'stage3-pipeline-secret',
        budget: BudgetConfig | None = None,
        gate_mode: str = 'review',
    ) -> None:
        self.history_reader = history_reader
        self.policy = policy
        self.owner_id = owner_id
        self.flash_model = flash_model
        self.strong_model = strong_model
        self.judge_model = judge_model
        self.flash_transport = flash_transport
        self.strong_transport = strong_transport or flash_transport
        self.judge_transport = judge_transport or strong_transport or flash_transport
        self.embedder = embedder
        self.pipeline_version = pipeline_version
        self.track: Literal['live', 'backfill'] = track
        self.budget = budget or BudgetConfig()
        self.gate_mode = gate_mode
        self._proposer = ProposalService()
        self._writer = ExperienceWriter(ticket_secret)
        self._issuer = TicketIssuer(ticket_secret, gate_id='stage3_pipeline')
        self._verifier = HistoryReaderEvidenceVerifier(history_reader)

    def _run_shadow_moments(
        self,
        conn,
        *,
        run_id: str,
        session: HistorySession,
        moments,
        valid_indices: list[int],
    ) -> tuple[int, int]:
        conn.execute(
            """
            INSERT INTO shadow_pilot_runs(run_id, owner_id, track, pipeline_version)
            VALUES (%s, %s, %s, %s) ON CONFLICT (run_id) DO NOTHING
            """,
            (run_id, self.owner_id, self.track, self.pipeline_version),
        )
        extracted = extract_session_moments(
            conn,
            moments,
            model=self.flash_model,
            transport=self.flash_transport,
            track=self.track,
            owner_id=self.owner_id,
            library=session.summary.library or 'default',
            valid_record_indices=valid_indices,
            is_shadow=True,
            budget=self.budget,
        )
        tokens_total = 0
        proposals_cnt = 0
        pending_items: list[tuple[Any, ...]] = []
        for mom, ext in zip(moments, extracted):
            toks = ext.tokens_used
            if ext.skipped:
                tokens_total += toks
                pending_items.append((
                    run_id, session.summary.session_id, mom.kind, 'skipped_extract',
                    '', '', '', '', '', '', '[]', 'extract', 'reject', ext.skip_reason[:64], toks,
                ))
                continue

            proposals_cnt += 1
            p = ext.payload
            l0_verdict, l0_reason, _ = evaluate_l0_fields(p, evidence_verifier=self._verifier, require_evidence=True)
            layer, verdict, reason, emb = 'L0', l0_verdict, (l0_reason or ''), None
            if l0_verdict == 'pass' and self.embedder is not None:
                l1 = assess_l1_shadow(
                    conn,
                    owner_id=self.owner_id,
                    category=p['category'],
                    problem=p['problem'],
                    solution=p['solution'],
                    reusable_experience=p['reusable_experience'],
                    embedder=self.embedder,
                )
                emb = l1['embedding']
                if l1['verdict'] == 'merge':
                    layer, verdict, reason = 'L1', 'merge', 'duplicate'
            if layer == 'L0' and l0_verdict == 'pass':
                l2 = assess_value_cascade(
                    conn,
                    problem=p['problem'],
                    solution=p['solution'],
                    scope=p.get('scope'),
                    flash_model=self.flash_model,
                    strong_model=self.strong_model,
                    flash_transport=self.flash_transport,
                    strong_transport=self.strong_transport,
                    track=self.track,
                    session_id=session.summary.session_id,
                    owner_id=self.owner_id,
                    is_shadow=True,
                    budget=self.budget,
                )
                toks += l2.tokens_used
                layer, verdict, reason = l2.layer, l2.verdict, (l2.reason_code or '')
                if l2.verdict == 'pass':
                    l3 = assess_l3_shadow(
                        conn,
                        owner_id=self.owner_id,
                        proposer_model=self.flash_model,
                        judge_model=self.judge_model,
                        judge_transport=self.judge_transport,
                        problem=p['problem'],
                        solution=p['solution'],
                        reusable_experience=p['reusable_experience'],
                        category=p['category'],
                        scope=p.get('scope'),
                        embedding=emb,
                        track=self.track,
                        session_id=session.summary.session_id,
                        is_shadow=True,
                        budget=self.budget,
                    )
                    toks += int(l3.get('tokens_used', 0))
                    layer = 'L3'
                    rel = l3.get('relation')
                    verdict = 'merge' if rel == 'duplicate' else 'pass'
                    reason = str(rel or 'novel')

            tokens_total += toks
            pending_items.append((
                run_id, session.summary.session_id, mom.kind, f'{layer}_{verdict}',
                p['category'], p['kind'], p['problem'], p['solution'], p['reusable_experience'],
                p.get('misbelief', ''), json.dumps(p['evidence'], ensure_ascii=False),
                layer, verdict, reason, toks,
            ))

        with conn.transaction():
            for row_vals in pending_items:
                conn.execute(
                    """
                    INSERT INTO shadow_pilot_items(
                        run_id, session_id, moment_kind, stage_outcome, category, kind,
                        problem, solution, reusable_experience, misbelief, evidence_json,
                        gate_layer, gate_verdict, reason_code, tokens_used
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                    """,
                    row_vals,
                )
        return proposals_cnt, tokens_total

    def _run_live_moments(self, conn, *, session: HistorySession, segment_hash: str, moments, valid_indices: list[int]) -> tuple[int, int]:
        extracted = extract_session_moments(
            conn,
            moments,
            model=self.flash_model,
            transport=self.flash_transport,
            track=self.track,
            owner_id=self.owner_id,
            library=session.summary.library or 'default',
            valid_record_indices=valid_indices,
            is_shadow=False,
            budget=self.budget,
        )
        proposals_cnt = 0
        tokens_total = 0
        safe_sid = _REQ_SAFE_RE.sub('_', session.summary.session_id)[:32]
        attr = AgentAttribution(
            actor=self.owner_id,
            agent_id='stage3-pipeline',
            client=session.summary.adapter or 'codex',
            model=self.flash_model,
        )
        for idx, ext in enumerate(extracted):
            tokens_total += ext.tokens_used
            if ext.skipped:
                continue
            p = ext.payload
            req_id = f'pipe-{safe_sid}-{segment_hash[:8]}-{idx}'
            try:
                res = self._proposer.propose(
                    conn,
                    owner_id=self.owner_id,
                    actor_id=self.owner_id,
                    channel=TrustedChannel.CLI_HOOK,
                    attribution=attr,
                    body={
                        'op': 'add',
                        'request_id': req_id,
                        'category': p['category'],
                        'kind': p['kind'],
                        'problem': p['problem'],
                        'solution': p['solution'],
                        'reusable_experience': p['reusable_experience'],
                        'misbelief': p.get('misbelief', ''),
                        'scope': p.get('scope', {}),
                        'evidence': p['evidence'],
                        'source_track': self.track,
                        'session_id': session.summary.session_id,
                    },
                    source_kind='pipeline',
                )
            except ProposalPolicyError:
                continue
            proposals_cnt += 1
            uid = res['uid']
            l0 = run_l0_gate(conn, uid, evidence_verifier=self._verifier, require_evidence=True)
            if l0['verdict'] != 'pass':
                continue
            if self.embedder is not None:
                l1 = run_l1_dedupe(conn, uid, embedder=self.embedder, ticket_issuer=self._issuer, writer=self._writer, gate_mode=self.gate_mode)
                if l1['verdict'] != 'pass':
                    continue
            l2 = run_l2_value_gate(
                conn, uid, flash_model=self.flash_model, strong_model=self.strong_model,
                flash_transport=self.flash_transport, strong_transport=self.strong_transport,
                gate_mode=self.gate_mode, budget=self.budget,
            )
            tokens_total += int(l2.get('tokens_used', 0))
            if l2['verdict'] != 'pass':
                continue
            l3 = run_l3_reconcile_gate(
                conn, uid, judge_model=self.judge_model, judge_transport=self.judge_transport,
                writer=self._writer, ticket_issuer=self._issuer, gate_mode=self.gate_mode, budget=self.budget,
            )
            tokens_total += int(l3.get('tokens_used', 0))
        return proposals_cnt, tokens_total

    def process_session_if_ready(
        self,
        conn,
        source_ref: str,
        *,
        is_closed: bool,
        last_message_at: datetime,
        now: datetime,
        shadow_mode: bool = False,
        shadow_run_id: str | None = None,
    ) -> dict[str, Any]:
        if not shadow_mode and not self.policy.experience_pipeline_enabled:
            return {'status': 'disabled', 'llm_calls': 0}

        ready, trig_reason = should_trigger_session(is_closed=is_closed, last_message_at=last_message_at, now=now)
        if not ready:
            return {'status': 'active_wait', 'trigger_reason': trig_reason, 'llm_calls': 0}

        session = self.history_reader.get_session(source_ref)
        if session is None:
            return {'status': 'not_found', 'llm_calls': 0}

        sid = session.summary.session_id
        prev_row = conn.execute(
            """
            SELECT MAX(last_event_index) FROM pipeline_segment_runs
            WHERE owner_id = %s AND track = %s AND session_id = %s
              AND pipeline_version = %s AND is_shadow = %s
              AND status IN ('completed', 'skipped_zero_moments')
            """,
            (self.owner_id, self.track, sid, self.pipeline_version, shadow_mode),
        ).fetchone()
        prev_idx = int(prev_row[0]) if (prev_row and prev_row[0] is not None) else -1

        parsed = parse_session_events(session)
        mode_tag = 'shadow' if shadow_mode else 'live'
        if not parsed.events and prev_idx < 0:
            empty_hash = hashlib.sha256(f'{mode_tag}:{sid}:-1:0:empty'.encode('utf-8')).hexdigest()[:32]
            empty_done = conn.execute(
                """
                SELECT status FROM pipeline_segment_runs
                WHERE owner_id = %s AND track = %s AND session_id = %s
                  AND segment_hash = %s AND pipeline_version = %s AND is_shadow = %s
                """,
                (self.owner_id, self.track, sid, empty_hash, self.pipeline_version, shadow_mode),
            ).fetchone()
            if empty_done and empty_done[0] in ('completed', 'skipped_zero_moments'):
                return {'status': 'already_processed', 'replayed': True, 'llm_calls': 0}
            _upsert_segment_run(
                conn,
                owner_id=self.owner_id, track=self.track, session_id=sid, segment_hash=empty_hash,
                pipeline_version=self.pipeline_version, status='skipped_zero_moments',
                moments_count=0, proposals_count=0, tokens_spent=0, last_event_index=0, is_shadow=shadow_mode,
            )
            return {'status': 'skipped_zero_moments', 'moments': 0, 'llm_calls': 0}

        delta_parsed, end_idx = _slice_delta_parsed(parsed, prev_idx)
        if delta_parsed is None:
            return {'status': 'already_processed', 'replayed': True, 'llm_calls': 0}
        seg_hash = hashlib.sha256(
            f'{mode_tag}:{sid}:{prev_idx}:{end_idx}:{json.dumps(delta_parsed.events, sort_keys=True, ensure_ascii=False)}'.encode('utf-8')
        ).hexdigest()[:32]
        done_row = conn.execute(
            """
            SELECT status FROM pipeline_segment_runs
            WHERE owner_id = %s AND track = %s AND session_id = %s
              AND segment_hash = %s AND pipeline_version = %s AND is_shadow = %s
            """,
            (self.owner_id, self.track, sid, seg_hash, self.pipeline_version, shadow_mode),
        ).fetchone()
        if done_row and done_row[0] in ('completed', 'skipped_zero_moments'):
            return {'status': 'already_processed', 'replayed': True, 'llm_calls': 0}

        moments = detect_key_moments(delta_parsed)
        if not moments:
            _upsert_segment_run(
                conn,
                owner_id=self.owner_id, track=self.track, session_id=sid, segment_hash=seg_hash,
                pipeline_version=self.pipeline_version, status='skipped_zero_moments',
                moments_count=0, proposals_count=0, tokens_spent=0, last_event_index=end_idx, is_shadow=shadow_mode,
            )
            return {'status': 'skipped_zero_moments', 'moments': 0, 'llm_calls': 0}

        valid_indices = [r.event_index for r in session.records]
        try:
            if shadow_mode:
                props, toks = self._run_shadow_moments(
                    conn, run_id=shadow_run_id or f'shadow-{sid}', session=session, moments=moments, valid_indices=valid_indices,
                )
            else:
                props, toks = self._run_live_moments(
                    conn, session=session, segment_hash=seg_hash, moments=moments, valid_indices=valid_indices,
                )
        except BudgetDeferred as exc:
            _upsert_segment_run(
                conn,
                owner_id=self.owner_id, track=self.track, session_id=sid, segment_hash=seg_hash,
                pipeline_version=self.pipeline_version, status='deferred',
                moments_count=len(moments), proposals_count=0, tokens_spent=0,
                last_event_index=prev_idx, error_code=exc.scope, is_shadow=shadow_mode,
            )
            return {'status': 'deferred', 'reason': exc.scope, 'detail': exc.detail, 'source_ref': source_ref}

        _upsert_segment_run(
            conn,
            owner_id=self.owner_id, track=self.track, session_id=sid, segment_hash=seg_hash,
            pipeline_version=self.pipeline_version, status='completed',
            moments_count=len(moments), proposals_count=props, tokens_spent=toks,
            last_event_index=end_idx, error_code='', is_shadow=shadow_mode,
        )
        return {'status': 'completed', 'moments': len(moments), 'proposals': props, 'tokens_spent': toks}

    def drain_deferred_segments(self, conn, *, shadow_mode: bool = False, shadow_run_id: str | None = None) -> list[dict[str, Any]]:
        """Re-process previously deferred segments once daily/stage/circuit limits recover."""
        rows = conn.execute(
            """
            SELECT DISTINCT session_id FROM pipeline_segment_runs
            WHERE owner_id = %s AND track = %s AND pipeline_version = %s
              AND is_shadow = %s AND status = 'deferred'
            """,
            (self.owner_id, self.track, self.pipeline_version, shadow_mode),
        ).fetchall()
        now = datetime.now(timezone.utc)
        out = []
        for (sid,) in rows:
            out.append(self.process_session_if_ready(
                conn, sid, is_closed=True, last_message_at=now, now=now, shadow_mode=shadow_mode, shadow_run_id=shadow_run_id,
            ))
        return out
