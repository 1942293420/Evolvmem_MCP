"""Task 2.7: L1 local embedding cosine dedupe and gated evidence merge."""
from __future__ import annotations

from pathlib import Path
import uuid

from evolvmem.experience_v2.control.agent_identity import AgentAttribution
from evolvmem.experience_v2.control.reputation_policy import TrustedChannel
from evolvmem.experience_v2.gate.propose import ProposalService
from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.experience_v2.writer import (
    ExperienceWriter,
    Op,
    TicketIssuer,
    scan_unauthorized_experience_notes_writes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = b'stage2-l1-test-secret'


class _DeterministicEmbedder:
    """Maps related topics into aligned direction vectors for PG HNSW cosine testing."""

    def initialize(self):
        return None

    def encode(self, text: str, kind: str = 'document') -> list[float]:
        if 'DNS' in text or 'resolv' in text:
            return [0.98, 0.20, 0.0]
        if 'CSS' in text or 'grid' in text:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


def _seed_active_note(conn, *, owner_id: str, category: str, problem: str, embedding_3d: list[float]) -> str:
    from evolvmem.experience_v2.gate.dedupe import to_unit_vec768_literal

    issuer = TicketIssuer(SECRET, gate_id='seed-user')
    writer = ExperienceWriter(SECRET)
    req_id = f'seed-{uuid.uuid4().hex[:12]}'
    ticket = issuer.issue(proposal_uid=f'manual:{req_id}', decision_id=f'dec:{req_id}', lane='user_fast')
    # Pad to 768 inside writer op
    padded = list(embedding_3d) + [0.0] * (768 - len(embedding_3d))
    _ = to_unit_vec768_literal  # ensure helper exists
    res = writer.apply(
        conn,
        ticket,
        Op(
            action='create',
            owner_id=owner_id,
            actor_id=owner_id,
            request_id=req_id,
            category=category,
            problem=problem,
            solution='固定 resolv.conf 与 daemon DNS 地址',
            reusable_experience='容器域名解析异常优先检查 daemon DNS 配置',
            evidence=[{'library': 'personal', 'source': 'archive:10'}],
            embedding=padded,
            origin='manual',
        ),
    )
    return res['experience']['id']


def test_l1_merges_same_category_high_cosine_and_passes_different_category(pg_exp_schema):
    from evolvmem.experience_v2.gate.dedupe import run_l1_dedupe

    conn, _schema, _dsn = pg_exp_schema
    apply_experience_migrations(conn)
    conn.commit()

    embedder = _DeterministicEmbedder()
    existing_id = _seed_active_note(
        conn,
        owner_id='u1',
        category='系统环境经验',
        problem='容器内 DNS 解析超时',
        embedding_3d=[0.99, 0.14, 0.0],
    )
    conn.commit()

    svc = ProposalService()
    attr = AgentAttribution(actor='u1', agent_id='a1', client='claude', model='claude-sonnet-4')
    issuer = TicketIssuer(SECRET, gate_id='l1-gate')
    writer = ExperienceWriter(SECRET)

    # 1. Same owner + same category + cosine >= 0.88 -> MERGE + occurrence_count + 1
    p_dup = svc.propose(
        conn,
        owner_id='u1',
        actor_id='u1',
        channel=TrustedChannel.MCP_CLIENT,
        attribution=attr,
        body={
            'category': '系统环境经验',
            'kind': 'pitfall',
            'problem': 'Docker 容器访问内网 DNS 超时',
            'solution': '在 daemon 配置固定内网 DNS 后重启容器服务',
            'reusable_experience': '遇到容器内网解析卡顿先核查 daemon resolv 配置',
            'evidence': [{'library': 'personal', 'source': 'archive:22'}],
            'session_id': 'sess-l1-1',
            'request_id': f'l1-dup-{uuid.uuid4().hex[:10]}',
        },
    )
    res_dup = run_l1_dedupe(
        conn,
        p_dup['uid'],
        embedder=embedder,
        ticket_issuer=issuer,
        writer=writer,
        threshold=0.88,
        gate_mode='review',
    )
    assert res_dup['verdict'] == 'merge'
    assert res_dup['reason_code'] == 'duplicate'
    assert res_dup['target_note_id'] == existing_id

    note_row = conn.execute(
        'SELECT occurrence_count, evidence FROM experience_notes WHERE id = %s',
        (existing_id,),
    ).fetchone()
    assert note_row[0] == 2
    assert len(note_row[1]) == 2
    prop_row = conn.execute(
        'SELECT state, merged_into_uid FROM experience_proposals WHERE uid = %s',
        (p_dup['uid'],),
    ).fetchone()
    assert prop_row == ('merged', existing_id)
    rel_row = conn.execute(
        "SELECT relation, to_uid FROM experience_relations WHERE from_uid = %s",
        (p_dup['uid'],),
    ).fetchone()
    assert rel_row == ('duplicate', existing_id)

    # 2. High vector similarity but DIFFERENT category -> must NOT merge -> pass into review
    p_diff_cat = svc.propose(
        conn,
        owner_id='u1',
        actor_id='u1',
        channel=TrustedChannel.MCP_CLIENT,
        attribution=attr,
        body={
            'category': '前端经验',
            'kind': 'pitfall',
            'problem': '前端 DNS 预解析标签写错域名',
            'solution': '修正 link rel=dns-prefetch 配置的域名地址',
            'reusable_experience': '静态资源预解析需同步环境域名模板',
            'evidence': [{'library': 'personal', 'source': 'archive:23'}],
            'session_id': 'sess-l1-2',
            'request_id': f'l1-cat-{uuid.uuid4().hex[:10]}',
        },
    )
    res_cat = run_l1_dedupe(
        conn,
        p_diff_cat['uid'],
        embedder=embedder,
        ticket_issuer=issuer,
        writer=writer,
        threshold=0.88,
        gate_mode='review',
    )
    assert res_cat['verdict'] == 'pass'
    assert conn.execute(
        'SELECT state FROM experience_proposals WHERE uid = %s', (p_diff_cat['uid'],)
    ).fetchone()[0] == 'review'
    assert scan_unauthorized_experience_notes_writes(REPO_ROOT) == []
