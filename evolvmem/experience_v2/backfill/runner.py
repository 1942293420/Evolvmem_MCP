"""Task B.2: Isolated historical backfill runner reusing Stage 3 components on track='backfill'."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Literal

from evolvmem.experience_v2.backfill.queue import BackfillQueue
from evolvmem.experience_v2.llm_meter import BudgetConfig, BudgetDeferred
from evolvmem.experience_v2.pipeline.history_reader import HistoryReader
from evolvmem.experience_v2.pipeline.scheduler import PipelineScheduler
from evolvmem.product_policy import ProductPolicy

DEFAULT_BACKFILL_PIPELINE_VERSION = 'stage3-v2'


class BackfillRunner:
    """Executes historical session backfill strictly isolated to track='backfill'."""

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
        pipeline_version: str = DEFAULT_BACKFILL_PIPELINE_VERSION,
        ticket_secret: bytes | str = b'stage4-backfill-secret',
        budget: BudgetConfig | None = None,
        gate_mode: str | dict[str, str] = 'review',
    ) -> None:
        self.owner_id = owner_id
        self.policy = policy
        self.queue = BackfillQueue(owner_id=owner_id)
        effective_mode: str | dict[str, str] = gate_mode if isinstance(gate_mode, (str, dict)) else 'review'
        self.scheduler = PipelineScheduler(
            history_reader=history_reader,
            policy=policy,
            owner_id=owner_id,
            flash_model=flash_model,
            strong_model=strong_model,
            judge_model=judge_model,
            flash_transport=flash_transport,
            strong_transport=strong_transport,
            judge_transport=judge_transport,
            embedder=embedder,
            pipeline_version=pipeline_version,
            track='backfill',
            ticket_secret=ticket_secret,
            budget=budget or BudgetConfig(backfill_daily_tokens=3_000_000),
            gate_mode=effective_mode,
        )

    def run_queued_batch(
        self,
        conn,
        *,
        scope_kind: Literal['project', 'month'],
        scope_value: str,
        now: datetime | None = None,
        max_jobs: int = 50,
        shadow_mode: bool = False,
        shadow_run_id: str | None = None,
        enforce_night_window: bool = True,
        on_job_done: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
        clock_fn: Callable[[], datetime] | None = None,
    ) -> dict[str, Any]:
        """Claim queued backfill jobs and run them via Stage 3 components under track='backfill'."""
        ts = now or datetime.now(timezone.utc)
        gate = self.queue.check_run_gate(
            policy=self.policy,
            now=ts,
            shadow_mode=shadow_mode,
            enforce_night_window=enforce_night_window,
        )
        if not gate['allowed']:
            return {
                'status': gate['status'],
                'processed': 0,
                'proposals': 0,
                'deferred': 0,
                'tokens_spent': 0,
                'llm_calls': 0,
                'results': [],
            }

        jobs = self.queue.claim_next_jobs(
            conn, scope_kind=scope_kind, scope_value=scope_value, limit=max_jobs, is_shadow=shadow_mode,
        )
        if not jobs:
            return {
                'status': 'empty',
                'processed': 0,
                'proposals': 0,
                'deferred': 0,
                'tokens_spent': 0,
                'llm_calls': 0,
                'results': [],
            }

        results: list[dict[str, Any]] = []
        total_props = 0
        total_toks = 0
        deferred = 0
        completed_cnt = 0
        gate_stop_status = ''

        for idx, job in enumerate(jobs):
            live_now = clock_fn() if clock_fn is not None else (ts if now is not None else datetime.now(timezone.utc))
            if not shadow_mode:
                step_gate = self.queue.check_run_gate(
                    policy=self.policy, now=live_now, shadow_mode=False, enforce_night_window=enforce_night_window,
                )
                if not step_gate['allowed']:
                    self.queue.requeue_jobs(conn, job_ids=[j['job_id'] for j in jobs[idx:]])
                    gate_stop_status = str(step_gate['status'])
                    break
            try:
                with conn.transaction():
                    res = self.scheduler.process_session_if_ready(
                        conn,
                        job['source_ref'],
                        is_closed=True,
                        last_message_at=live_now,
                        now=live_now,
                        shadow_mode=shadow_mode,
                        shadow_run_id=shadow_run_id,
                    )
            except BudgetDeferred as exc:
                res = {'status': 'deferred', 'reason': str(exc) or 'budget_deferred'}
            except Exception as exc:
                res = {'status': 'failed', 'reason': type(exc).__name__}
            st = str(res.get('status') or '')
            if st == 'deferred':
                deferred += 1
                self.queue.mark_job_result(
                    conn, job_id=job['job_id'], status='deferred',
                    error_code=str(res.get('reason') or 'deferred'),
                )
                self.queue.requeue_jobs(conn, job_ids=[j['job_id'] for j in jobs[idx + 1:]])
                row_summary = {'job': job, 'result': res}
                results.append(row_summary)
                if on_job_done is not None:
                    on_job_done(job, res)
                break
            elif st == 'skipped_zero_moments':
                completed_cnt += 1
                self.queue.mark_job_result(conn, job_id=job['job_id'], status='skipped_zero_moments')
            elif st in ('completed', 'already_processed'):
                completed_cnt += 1
                props = int(res.get('proposals') or 0)
                toks = int(res.get('tokens_spent') or 0)
                moments = int(res.get('moments') or 0)
                total_props += props
                total_toks += toks
                self.queue.mark_job_result(
                    conn, job_id=job['job_id'], status='completed',
                    moments_count=moments, proposals_count=props, tokens_spent=toks,
                )
            elif st in ('not_found', 'disabled'):
                self.queue.mark_job_result(conn, job_id=job['job_id'], status='failed', error_code=st)
            else:
                self.queue.mark_job_result(conn, job_id=job['job_id'], status='failed', error_code=str(res.get('reason') or st or 'unknown'))

            row_summary = {'job': job, 'result': res}
            results.append(row_summary)
            if on_job_done is not None:
                on_job_done(job, res)

        out_status = gate_stop_status or ('completed' if deferred == 0 else 'partial_deferred')
        return {
            'status': out_status,
            'processed': len(results),
            'completed': completed_cnt,
            'proposals': total_props,
            'deferred': deferred,
            'tokens_spent': total_toks,
            'results': results,
        }
