"""TDD tests for Stage 3 Task 3.6: L2 Value Gate cascade (flash -> strong blind test)."""
from __future__ import annotations

import json
from pathlib import Path

from evolvmem.experience_v2.control.agent_identity import AgentAttribution
from evolvmem.experience_v2.control.reputation_policy import TrustedChannel
from evolvmem.experience_v2.gate.propose import ProposalService
from evolvmem.experience_v2.llm_meter import estimate_tokens, scan_unmetered_llm_calls
from evolvmem.experience_v2.pipeline.history_reader import scan_direct_sqlite_in_pipeline
from evolvmem.experience_v2.writer import scan_unauthorized_experience_notes_writes


def _insert_proposal(conn, req_id: str, problem: str, solution: str, reusable: str, scope: dict) -> str:
    svc = ProposalService()
    attr = AgentAttribution(
        actor="owner-36",
        agent_id="agent-36",
        client="codex",
        model="gpt-5-codex",
        device_id="dev-1",
    )
    res = svc.propose(
        conn,
        owner_id="owner-36",
        actor_id="owner-36",
        channel=TrustedChannel.CLI_HOOK,
        attribution=attr,
        body={
            "op": "add",
            "request_id": req_id,
            "category": "系统环境经验",
            "kind": "pitfall",
            "problem": problem,
            "solution": solution,
            "reusable_experience": reusable,
            "scope": scope,
            "evidence": [{"library": "dev", "source": "archive:101", "event": 1}],
            "source_track": "live",
            "session_id": f"sess-{req_id}",
        },
        source_kind="pipeline",
    )
    return res["uid"]


def test_l2a_flash_solves_common_knowledge_skips_strong_model(pg_s3_migrated) -> None:
    from evolvmem.experience_v2.gate.value import (
        L2_JUDGE_SYSTEM_PROMPT,
        L2_PREDICT_SYSTEM_PROMPT,
        run_l2_value_gate,
    )

    conn, *_ = pg_s3_migrated
    assert estimate_tokens(L2_PREDICT_SYSTEM_PROMPT) <= 600
    assert estimate_tokens(L2_JUDGE_SYSTEM_PROMPT) <= 600
    assert "对比参考答案" in L2_JUDGE_SYSTEM_PROMPT
    assert "手滑" in L2_JUDGE_SYSTEM_PROMPT
    assert "sha256" in L2_JUDGE_SYSTEM_PROMPT

    uid = _insert_proposal(
        conn,
        "req-l2-common",
        "Python读取UTF-8文件在Windows默认编码下报错UnicodeDecodeError",
        "打开文件时显式传入 encoding='utf-8' 参数即可避免默认 GBK 解码异常",
        "跨平台读写文本一律显式指定 UTF-8 编码",
        {"os": "Windows", "runtime": "Python"},
    )

    strong_calls = 0

    def flash_transport(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        sys_p = messages[0]["content"]
        if "对比参考答案" in sys_p or "一致性" in sys_p:
            return {"text": json.dumps({"consistent": True, "confidence": 0.96, "detail": "flash 直接命中 UTF-8 解法"}), "input_tokens": 90, "output_tokens": 25}
        assert "显式传入" not in messages[1]["content"]
        assert estimate_tokens(messages[1]["content"]) <= 300
        return {"text": "open(..., encoding='utf-8')", "input_tokens": 50, "output_tokens": 20}

    def strong_transport(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        nonlocal strong_calls
        strong_calls += 1
        return {"text": "{}"}

    out = run_l2_value_gate(
        conn,
        uid,
        flash_model="deepseek-v4-flash",
        strong_model="mimo-v2.6-flash",
        flash_transport=flash_transport,
        strong_transport=strong_transport,
    )
    assert out["layer"] == "L2a"
    assert out["verdict"] == "reject"
    assert out["reason_code"] == "common_knowledge"
    assert strong_calls == 0

    state = conn.execute("SELECT state FROM experience_proposals WHERE uid = %s", (uid,)).fetchone()[0]
    assert state == "rejected"
    decs = conn.execute("SELECT layer, verdict, reason_code FROM proposal_decisions WHERE proposal_uid = %s", (uid,)).fetchall()
    assert decs == [("L2a", "reject", "common_knowledge")]


def test_l2b_cascade_rejects_strong_known_and_passes_private_knowledge(pg_s3_migrated) -> None:
    from evolvmem.experience_v2.gate.value import run_l2_value_gate

    conn, *_ = pg_s3_migrated
    uid_strong_known = _insert_proposal(
        conn,
        "req-l2-strong-known",
        "PostgreSQL GIN索引大表全表更新出现严重膨胀",
        "将 fastupdate 调整或分批更新并执行 VACUUM ANALYZE 控制 pending list",
        "高频更新 GIN 索引列需控制 pending list 并分批写入",
        {"system": "postgresql"},
    )
    uid_private = _insert_proposal(
        conn,
        "req-l2-private",
        "内网采集端切后台超过15分钟页面定时器停止",
        "内网网关与浏览器节能会挂起页面setTimeout，等待计时必须放在宿主执行端",
        "后台采集任务的阻塞计时必须放在宿主进程而非页面脚本",
        {"system": "internal-crawler", "os": "Windows", "extra": "x " * 400},
    )

    strong_msgs: list[str] = []

    def flash_miss(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        if "对比参考答案" in messages[0]["content"]:
            return {"text": json.dumps({"consistent": False, "confidence": 0.88, "detail": "flash 未给出正确机制"}), "input_tokens": 80, "output_tokens": 25}
        assert estimate_tokens(messages[1]["content"]) <= 300
        return {"text": "可能是网络断开或参数写错", "input_tokens": 60, "output_tokens": 20}

    def strong_hits_pg(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        if "对比参考答案" in messages[0]["content"]:
            return {"text": json.dumps({"consistent": True, "confidence": 0.91, "detail": "强模型准确命中 GIN pending list 机制"}), "input_tokens": 85, "output_tokens": 25}
        strong_msgs.append(messages[1]["content"])
        assert estimate_tokens(messages[1]["content"]) <= 300
        assert "VACUUM ANALYZE" not in messages[1]["content"]
        return {"text": "关闭或控制 fastupdate pending list 并分批写入后 VACUUM", "input_tokens": 70, "output_tokens": 30}

    res1 = run_l2_value_gate(conn, uid_strong_known, flash_model="flash-m", strong_model="strong-m", flash_transport=flash_miss, strong_transport=strong_hits_pg)
    assert res1["layer"] == "L2b"
    assert res1["verdict"] == "reject"
    assert res1["reason_code"] == "common_knowledge"
    assert len(strong_msgs) == 1

    def strong_misses_private(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        if "对比参考答案" in messages[0]["content"]:
            return {"text": json.dumps({"consistent": False, "confidence": 0.93, "detail": "强模型无法推导私有内网行为"}), "input_tokens": 85, "output_tokens": 25}
        assert estimate_tokens(messages[1]["content"]) <= 300
        assert "宿主执行端" not in messages[1]["content"]
        return {"text": "建议加大前端重试次数", "input_tokens": 65, "output_tokens": 20}

    res2 = run_l2_value_gate(conn, uid_private, flash_model="flash-m", strong_model="strong-m", flash_transport=flash_miss, strong_transport=strong_misses_private)
    assert res2["layer"] == "L2b"
    assert res2["verdict"] == "pass"
    assert res2["reason_code"] is None
    assert res2["calls_used"] == 4

    decs = conn.execute("SELECT layer, verdict FROM proposal_decisions WHERE proposal_uid = %s ORDER BY created_at ASC", (uid_private,)).fetchall()
    assert decs == [("L2a", "pass"), ("L2b", "pass")]
    call_cnt = conn.execute("SELECT COUNT(*) FROM llm_calls WHERE proposal_uid = %s", (uid_private,)).fetchone()[0]
    assert call_cnt == 4

    assert scan_unmetered_llm_calls(Path("evolvmem/experience_v2")) == []
    assert scan_direct_sqlite_in_pipeline(Path("evolvmem/experience_v2/pipeline")) == []
    assert scan_unauthorized_experience_notes_writes(Path(".")) == []


def test_l2_judge_receives_problem_and_unclipped_long_solution(pg_s3_migrated) -> None:
    from evolvmem.experience_v2.gate.value import run_l2_value_gate

    conn, *_ = pg_s3_migrated
    long_prob = "飞书多维表跨会话自动同步任务在他人同步修改记录期间如何防止覆盖冲突并实现基于修订版本的精确增量拉取校验"
    long_sol = (
        "由于多维表在长会话执行期间会被其他协作人员实时增删改记录，每轮写回或汇总前绝不能复用开局缓存的全量快照；"
        "必须先通过团队封装的 lark-cli 读取当前表的最新 revision 版本号及 records_count 统计，"
        "若 revision 发生变化则按主键与字段级增量 diff 重新对齐本地状态后再提交变更，从而避免盲写覆盖协作数据。"
    )
    assert len(long_sol) > 150
    uid = _insert_proposal(
        conn,
        "req-l2-judge-ctx",
        long_prob,
        long_sol,
        "多维表协同更新前先用 lark-cli 检查 revision 变化并执行字段级增量 diff",
        {"system": "lark-bitable"},
    )
    judge_users: list[str] = []

    def flash_tr(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        if "对比参考答案" in messages[0]["content"]:
            judge_users.append(messages[1]["content"])
            return {"text": json.dumps({"consistent": False, "confidence": 0.9, "detail": "missed lark-cli revision diff"}), "input_tokens": 90, "output_tokens": 25}
        assert estimate_tokens(messages[1]["content"]) <= 300
        assert long_sol not in messages[1]["content"]
        return {"text": "加锁重试即可", "input_tokens": 60, "output_tokens": 20}

    def strong_tr(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        if "对比参考答案" in messages[0]["content"]:
            judge_users.append(messages[1]["content"])
            return {"text": json.dumps({"consistent": False, "confidence": 0.95, "detail": "private lark-cli revision workflow"}), "input_tokens": 95, "output_tokens": 25}
        assert estimate_tokens(messages[1]["content"]) <= 300
        assert "lark-cli" not in messages[1]["content"]
        return {"text": "使用通用乐观锁版本号", "input_tokens": 65, "output_tokens": 20}

    out = run_l2_value_gate(
        conn, uid, flash_model="flash-m", strong_model="strong-m",
        flash_transport=flash_tr, strong_transport=strong_tr,
    )
    assert out["verdict"] == "pass"
    assert len(judge_users) == 2
    for ju in judge_users:
        assert "问题现象:" in ju
        assert "参考解法:" in ju
        assert "模型盲测答案:" in ju
        assert "从而避免盲写覆盖协作数据" in ju
