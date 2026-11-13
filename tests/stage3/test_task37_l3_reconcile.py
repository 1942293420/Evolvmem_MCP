"""TDD tests for Stage 3 Task 3.7: L3 6-relation reconciliation, challenger probation & misbelief."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evolvmem.experience_v2.control.agent_identity import AgentAttribution
from evolvmem.experience_v2.control.reputation_policy import TrustedChannel
from evolvmem.experience_v2.gate.propose import ProposalService
from evolvmem.experience_v2.gate.rules import L0_LIMITS
from evolvmem.experience_v2.llm_meter import estimate_tokens, scan_unmetered_llm_calls
from evolvmem.experience_v2.pipeline.history_reader import scan_direct_sqlite_in_pipeline
from evolvmem.experience_v2.writer import (
    ExperienceWriter,
    Op,
    TicketIssuer,
    scan_unauthorized_experience_notes_writes,
)


def _seed_incumbent(conn, issuer: TicketIssuer, writer: ExperienceWriter, *, req_id: str, problem: str, solution: str, reusable: str, importance: int = 5, pinned: bool = False) -> str:
    svc = ProposalService()
    attr = AgentAttribution(actor="owner-37", agent_id="seed", client="codex", model="seed-model")
    prop = svc.propose(
        conn,
        owner_id="owner-37",
        actor_id="owner-37",
        channel=TrustedChannel.WEB_HUMAN,
        attribution=attr,
        body={
            "op": "add",
            "request_id": f"seed-inc-{req_id}",
            "category": "系统环境经验",
            "kind": "env_fact",
            "problem": problem,
            "solution": solution,
            "reusable_experience": reusable,
            "evidence": [{"library": "dev", "source": "archive:1", "event": 1}],
            "importance": importance,
            "pinned": pinned,
        },
        source_kind="pipeline",
    )
    dec_id = f"dec-seed-{req_id}"
    with conn.transaction():
        conn.execute(
            "INSERT INTO proposal_decisions(decision_id, proposal_uid, layer, verdict, confidence, judge_model, detail) VALUES (%s, %s, 'L0', 'pass', 1.0, 'seed', 'ok')",
            (dec_id, prop["uid"]),
        )
        res = writer.apply(
            conn,
            issuer.issue(proposal_uid=prop["uid"], decision_id=dec_id, lane="gated"),
            Op(
                action="create",
                owner_id="owner-37",
                actor_id="owner-37",
                request_id=f"note-{req_id}",
                category="系统环境经验",
                problem=problem,
                solution=solution,
                reusable_experience=reusable,
                origin="pipeline",
                kind="env_fact",
                evidence=[{"library": "dev", "source": "archive:1", "event": 1}],
                importance=importance,
                pinned=pinned,
            ),
        )
    return res["experience"]["id"]


def _propose_candidate(conn, *, req_id: str, problem: str, solution: str, reusable: str, model: str = "deepseek-v4-flash", scope: dict | None = None) -> str:
    svc = ProposalService()
    attr = AgentAttribution(actor="owner-37", agent_id="pipe", client="codex", model=model)
    prop = svc.propose(
        conn,
        owner_id="owner-37",
        actor_id="owner-37",
        channel=TrustedChannel.CLI_HOOK,
        attribution=attr,
        body={
            "op": "add",
            "request_id": f"cand-req-{req_id}",
            "category": "系统环境经验",
            "kind": "env_fact",
            "problem": problem,
            "solution": solution,
            "reusable_experience": reusable,
            "scope": scope or {},
            "evidence": [{"library": "dev", "source": "archive:2", "event": 1}],
            "source_track": "live",
            "session_id": f"sess-{req_id}",
        },
        source_kind="pipeline",
    )
    return prop["uid"]


def test_self_judging_forbidden_and_prompt_limit(pg_s3_migrated) -> None:
    from evolvmem.experience_v2.gate.reconcile import (
        L3_SYSTEM_PROMPT,
        RELATIONS,
        SelfJudgingForbiddenError,
        run_l3_reconcile_gate,
    )

    conn, *_ = pg_s3_migrated
    assert set(RELATIONS) == {"duplicate", "refines", "scope_split", "outdated", "refuted", "contested"}
    assert estimate_tokens(L3_SYSTEM_PROMPT) <= 600

    secret = b"test-secret-37"
    issuer = TicketIssuer(secret, gate_id="L3_reconcile")
    writer = ExperienceWriter(secret)
    _seed_incumbent(conn, issuer, writer, req_id="sj", problem="PG连接池超时设置问题", solution="保持 idle_timeout 低于服务端阈值", reusable="连接池超时应小于服务端空闲断开时间")
    uid = _propose_candidate(conn, req_id="cand-sj", model="mimo-v2.6-flash", problem="PG连接池空闲断连报错", solution="连接超时需设置短于网关空闲超时时间", reusable="客户端空闲阈值须短于网关断开窗口")

    with pytest.raises(SelfJudgingForbiddenError):
        run_l3_reconcile_gate(
            conn,
            uid,
            judge_model="mimo-v2.6-flash",
            judge_transport=lambda m, msgs, k: {"text": "{}"},
            writer=writer,
            ticket_issuer=issuer,
            gate_mode="auto",
        )


def test_all_six_relations_and_refuted_misbelief_without_physical_delete(pg_s3_migrated) -> None:
    from evolvmem.experience_v2.gate.reconcile import run_l3_reconcile_gate

    conn, *_ = pg_s3_migrated
    secret = b"test-secret-37"
    issuer = TicketIssuer(secret, gate_id="L3_reconcile")
    writer = ExperienceWriter(secret)

    scenarios = [
        ("duplicate", {}),
        ("refines", {}),
        ("scope_split", {"incumbent_scope": {"os": "Linux"}, "challenger_scope": {"os": "Windows"}}),
        ("outdated", {}),
        (
            "refuted",
            {
                "misbelief_problem": "误把页面采集失败当成HTTP通道故障",
                "misbelief_solution": "排查先看标签页后台挂起而非立即切换HTTP通道",
                "misbelief_reusable": "看到通道异常先排宿主挂起，勿轻易判定传输失败",
                "misbelief_text": "此前误以为是HTTP provider损坏，实际为标签页后台节流",
            },
        ),
        ("contested", {}),
    ]

    for rel, extra in scenarios:
        inc_id = _seed_incumbent(
            conn,
            issuer,
            writer,
            req_id=f"inc-{rel}",
            problem=f"{rel} 场景初始问题说明",
            solution=f"{rel} 场景初始标准方案说明",
            reusable=f"{rel} 场景初始可复用经验说明",
            importance=5,
            pinned=False,
        )
        prop_uid = _propose_candidate(
            conn,
            req_id=f"prop-{rel}",
            problem=f"{rel} 场景新发现触发条件",
            solution=f"{rel} 场景更新后验证结论与做法",
            reusable=f"{rel} 场景更新后可复用工程原则",
            model="deepseek-v4-flash",
            scope={"os": "Windows"},
        )
        count_before = conn.execute("SELECT COUNT(*) FROM experience_notes").fetchone()[0]

        def transport(m, msgs, k, _rel=rel, _inc=inc_id, _extra=extra):
            payload = {"relation": _rel, "target_note_id": _inc, "confidence": 0.9, "reason": f"判定为 {_rel}"}
            payload.update(_extra)
            return {"text": json.dumps(payload, ensure_ascii=False), "input_tokens": 120, "output_tokens": 45}

        out = run_l3_reconcile_gate(
            conn,
            prop_uid,
            judge_model="mimo-v2.6-flash",
            judge_transport=transport,
            writer=writer,
            ticket_issuer=issuer,
            gate_mode="auto",
        )
        assert out["relation"] == rel
        count_after = conn.execute("SELECT COUNT(*) FROM experience_notes").fetchone()[0]
        assert count_after >= count_before  # zero physical delete

        rel_row = conn.execute(
            "SELECT relation, to_uid FROM experience_relations WHERE owner_id = 'owner-37' AND proposal_uid = %s ORDER BY relation_id DESC LIMIT 1",
            (prop_uid,),
        ).fetchone()
        assert rel_row == (rel, inc_id)

        inc_row = conn.execute("SELECT status, deleted_at, scope FROM experience_notes WHERE id = %s", (inc_id,)).fetchone()
        assert inc_row[1] is None  # never physically soft/hard deleted in outdated/refuted
        if rel in ("outdated", "refuted"):
            assert inc_row[0] == "retired"
        elif rel == "contested":
            assert inc_row[0] == "contested"
        elif rel == "scope_split":
            assert inc_row[2].get("os") == "Linux"

        if rel == "refuted":
            mis_prop = conn.execute(
                "SELECT kind, problem, solution, reusable_experience, misbelief FROM experience_proposals WHERE request_id = %s",
                (f"l3-misbelief:{prop_uid}",),
            ).fetchone()
            assert mis_prop is not None
            assert mis_prop[0] == "pitfall"
            assert 0 < len(mis_prop[1]) <= L0_LIMITS["problem"]
            assert 0 < len(mis_prop[2]) <= L0_LIMITS["solution"]
            assert 0 < len(mis_prop[3]) <= L0_LIMITS["reusable_experience"]
            assert 0 < len(mis_prop[4]) <= L0_LIMITS["misbelief"]


def test_challenger_probation_promote_and_auto_rollback_and_guards(pg_s3_migrated) -> None:
    from evolvmem.experience_v2.gate.reconcile import record_challenger_outcome, run_l3_reconcile_gate

    conn, *_ = pg_s3_migrated
    secret = b"test-secret-37"
    issuer = TicketIssuer(secret, gate_id="L3_reconcile")
    writer = ExperienceWriter(secret)

    # 1) High-importance incumbent -> challenger probation -> 2 successes promote it
    inc_prom = _seed_incumbent(conn, issuer, writer, req_id="prom", problem="高权重规则1问题", solution="高权重旧方案1", reusable="高权重旧经验1", importance=9)
    uid_prom = _propose_candidate(conn, req_id="cand-prom", problem="高权重替换新问题1", solution="高权重新验证方案1", reusable="高权重新验证经验1")
    out1 = run_l3_reconcile_gate(
        conn,
        uid_prom,
        judge_model="mimo-v2.6-flash",
        judge_transport=lambda m, msgs, k: {"text": json.dumps({"relation": "outdated", "target_note_id": inc_prom, "confidence": 0.9, "reason": "版本演进"}), "input_tokens": 100, "output_tokens": 30},
        writer=writer,
        ticket_issuer=issuer,
        gate_mode="auto",
        required_successes=2,
    )
    ch_id_1 = out1["new_note_id"]
    assert conn.execute("SELECT status FROM experience_notes WHERE id = %s", (ch_id_1,)).fetchone()[0] == "challenger"
    assert conn.execute("SELECT status FROM experience_notes WHERE id = %s", (inc_prom,)).fetchone()[0] == "active"

    r1 = record_challenger_outcome(conn, owner_id="owner-37", challenger_note_id=ch_id_1, succeeded=True, writer=writer, ticket_issuer=issuer)
    assert r1["trial_status"] == "trial"
    r2 = record_challenger_outcome(conn, owner_id="owner-37", challenger_note_id=ch_id_1, succeeded=True, writer=writer, ticket_issuer=issuer)
    assert r2["trial_status"] == "promoted"
    assert conn.execute("SELECT status FROM experience_notes WHERE id = %s", (ch_id_1,)).fetchone()[0] == "active"
    assert conn.execute("SELECT status FROM experience_notes WHERE id = %s", (inc_prom,)).fetchone()[0] == "retired"

    # 2) Pinned incumbent -> challenger probation -> 1 failure auto-rolls back
    inc_rb = _seed_incumbent(conn, issuer, writer, req_id="rb", problem="Pinned核心约束问题", solution="Pinned旧做法", reusable="Pinned旧原则", pinned=True)
    uid_rb = _propose_candidate(conn, req_id="cand-rb", problem="Pinned替代尝试问题", solution="Pinned替代做法", reusable="Pinned替代原则")
    out2 = run_l3_reconcile_gate(
        conn,
        uid_rb,
        judge_model="mimo-v2.6-flash",
        judge_transport=lambda m, msgs, k: {"text": json.dumps({"relation": "outdated", "target_note_id": inc_rb, "confidence": 0.85, "reason": "尝试替换"}), "input_tokens": 100, "output_tokens": 30},
        writer=writer,
        ticket_issuer=issuer,
        gate_mode="auto",
        required_successes=2,
    )
    ch_id_2 = out2["new_note_id"]
    rb_res = record_challenger_outcome(conn, owner_id="owner-37", challenger_note_id=ch_id_2, succeeded=False, writer=writer, ticket_issuer=issuer)
    assert rb_res["trial_status"] == "rolled_back"
    assert conn.execute("SELECT status FROM experience_notes WHERE id = %s", (ch_id_2,)).fetchone()[0] == "retired"
    assert conn.execute("SELECT status FROM experience_notes WHERE id = %s", (inc_rb,)).fetchone()[0] == "active"

    assert scan_unauthorized_experience_notes_writes(Path(".")) == []
    assert scan_unmetered_llm_calls(Path("evolvmem/experience_v2")) == []
    assert scan_direct_sqlite_in_pipeline(Path("evolvmem/experience_v2/pipeline")) == []
