"""Task 2.6: L0 deterministic hard rules, task-log rejection, secret scan, and evidence verifier."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Protocol, Sequence
import uuid

from evolvmem.experience_v2.gate.propose import VALID_KINDS
from evolvmem.experience_v2.writer import CATEGORIES

REASON_CODES: dict[str, str] = {
    'common_knowledge': '常识',
    'task_log': '任务日志',
    'one_off_bug': '一次性 bug',
    'weak_evidence': '证据不足',
    'duplicate': '重复',
    'wrong': '内容错误',
    'wrong_scope': '范围不对',
    'other': '其他',
}

L0_LIMITS = {
    'problem': 120,
    'solution': 240,
    'reusable_experience': 120,
    'misbelief': 160,
}

_LINE_BREAKS_RE = re.compile(r'[\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]')
_TASK_LOG_RE = re.compile(
    r'^(?:'
    r'用户(?:要求|询问|希望|让|指示|提到|请求|反馈了)|'
    r'帮用户|查看了|检查了|'
    r'已(?:完成|创建并|按要求)|'
    r'本次(?:会话|对话|任务)|'
    r'(?:按照|根据)用户(?:要求|指示)'
    r')'
)
_SECRET_RES = (
    re.compile(r'\b(?:sk|ghp|gho|xoxb|xoxp|AKIA)[A-Za-z0-9_-]{8,}\b'),
    re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}'),
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    re.compile(r'(?i)\b(?:postgres(?:ql)?|mysql|redis|amqp)://[^:\s/]+:[^@\s]+@'),
)
_PTR_RE = re.compile(r'^(archive|session|context|event):([A-Za-z0-9._:-]{1,128})\Z')


class EvidenceVerifier(Protocol):
    def verify_all(self, evidence: list[dict[str, Any]]) -> tuple[bool, str]: ...


class SqliteHistoryEvidenceVerifier:
    """Read-only ?mode=ro verifier checking experience evidence pointers in SQLite history."""

    def __init__(self, db_path: Path | str | Sequence[Path | str]):
        raw = [db_path] if isinstance(db_path, (str, Path)) else list(db_path)
        self.db_paths: tuple[Path, ...] = tuple(Path(p) for p in raw if p and str(p).strip())
        self.db_path = self.db_paths[0] if self.db_paths else Path('')

    @staticmethod
    def _has_ref(conn: sqlite3.Connection, tables: set[str], kind: str, ref_id: str) -> tuple[bool, str]:
        if kind == 'archive':
            row = None
            if 'session_archives' in tables:
                row = conn.execute('SELECT 1 FROM session_archives WHERE id = ?', (int(ref_id),)).fetchone()
            if row is None and 'conversation_history' in tables:
                row = conn.execute('SELECT 1 FROM conversation_history WHERE archive_id = ?', (int(ref_id),)).fetchone()
            return (row is not None), f'未找到归档记录 archive:{ref_id}'
        if kind == 'session':
            if 'session_archives' not in tables:
                return False, '缺少 session_archives 表'
            row = conn.execute(
                'SELECT 1 FROM session_archives WHERE external_session_id = ? OR CAST(id AS TEXT) = ?',
                (ref_id, ref_id),
            ).fetchone()
            return (row is not None), f'未找到会话记录 session:{ref_id}'
        if kind == 'context':
            if 'context_items' not in tables:
                return False, '未找到上下文条目'
            row = conn.execute('SELECT 1 FROM context_items WHERE id = ?', (int(ref_id),)).fetchone()
            return (row is not None), f'未找到上下文 context:{ref_id}'
        if 'project_events' not in tables:
            return False, '缺少 project_events 表'
        row = conn.execute('SELECT 1 FROM project_events WHERE event_id = ?', (ref_id,)).fetchone()
        return (row is not None), f'未找到项目事件 event:{ref_id}'

    def verify_all(self, evidence: list[dict[str, Any]]) -> tuple[bool, str]:
        if not isinstance(evidence, list) or not evidence:
            return False, '缺少历史会话证据指针'
        existing = [p for p in self.db_paths if p.is_file()]
        if not existing:
            return False, '历史库文件不存在，无法核验证据'

        parsed_refs: list[tuple[str, str]] = []
        for item in evidence:
            if not isinstance(item, dict):
                return False, '证据条目格式错误'
            src = item.get('source')
            if not isinstance(src, str):
                return False, '证据缺少 source 指针'
            match = _PTR_RE.fullmatch(src.strip())
            if not match:
                return False, f'不支持的证据指针格式: {src[:32]}'
            kind, ref_id = match.group(1), match.group(2)
            if kind in ('archive', 'context') and not ref_id.isdigit():
                return False, f'{kind} 指针必须为整数ID'
            parsed_refs.append((kind, ref_id))

        conns: list[tuple[sqlite3.Connection, set[str]]] = []
        try:
            for path in existing:
                try:
                    c = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
                    c.execute('PRAGMA query_only=ON')
                    tbls = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                    conns.append((c, tbls))
                except sqlite3.Error:
                    continue
            if not conns:
                return False, '只读打开历史库失败'
            for kind, ref_id in parsed_refs:
                err_msg = '证据校验失败'
                found = False
                for c, tbls in conns:
                    ok, msg = self._has_ref(c, tbls, kind, ref_id)
                    if ok:
                        found = True
                        break
                    err_msg = msg
                if not found:
                    return False, err_msg
            return True, 'ok'
        finally:
            for c, _ in conns:
                try:
                    c.close()
                except Exception:
                    pass


def _contains_secret(texts: list[str]) -> bool:
    for text in texts:
        for pat in _SECRET_RES:
            if pat.search(text):
                return True
    return False


def _is_task_log(problem: str, solution: str, reusable: str) -> bool:
    for field_val in (problem, reusable, solution):
        if _TASK_LOG_RE.search(field_val.strip()):
            return True
    return False


def evaluate_l0_fields(
    proposal: dict[str, Any],
    *,
    evidence_verifier: EvidenceVerifier | None = None,
    require_evidence: bool = True,
) -> tuple[str, str | None, str]:
    category = proposal.get('category')
    kind = proposal.get('kind')
    if category not in CATEGORIES or kind not in VALID_KINDS:
        return 'reject', 'other', '分类或类型不合法'

    problem = str(proposal.get('problem') or '').strip()
    solution = str(proposal.get('solution') or '').strip()
    reusable = str(proposal.get('reusable_experience') or '').strip()
    misbelief = str(proposal.get('misbelief') or '').strip()

    for name, val in (('problem', problem), ('solution', solution), ('reusable_experience', reusable)):
        if not val or _LINE_BREAKS_RE.search(val):
            return 'reject', 'other', f'{name} 不能为空且必须单行'
        if len(val) > L0_LIMITS[name]:
            return 'reject', 'other', f'{name} 超过 {L0_LIMITS[name]} 字限制'
    if misbelief:
        if _LINE_BREAKS_RE.search(misbelief) or len(misbelief) > L0_LIMITS['misbelief']:
            return 'reject', 'other', f"misbelief 超过 {L0_LIMITS['misbelief']} 字或含换行"

    if _contains_secret([problem, solution, reusable, misbelief]):
        return 'reject', 'wrong', '检测到疑似密钥或凭据片段，已拦截'

    if _is_task_log(problem, solution, reusable):
        return 'reject', 'task_log', '判定为单次会话任务日志而非可复用工程经验'

    evidence = proposal.get('evidence') or []
    if require_evidence:
        if evidence_verifier is None:
            if not isinstance(evidence, list) or not evidence:
                return 'reject', 'weak_evidence', '缺少可核验的历史证据指针'
        else:
            ok, msg = evidence_verifier.verify_all(evidence)
            if not ok:
                return 'reject', 'weak_evidence', msg[:200]

    return 'pass', None, 'L0 硬规则校验通过'


def run_l0_gate(
    conn,
    proposal_uid: str,
    *,
    evidence_verifier: EvidenceVerifier | None = None,
    require_evidence: bool = True,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT category, kind, problem, solution, reusable_experience, misbelief, evidence_json
        FROM experience_proposals
        WHERE uid = %s
        FOR UPDATE
        """,
        (proposal_uid,),
    ).fetchone()
    if row is None:
        raise ValueError(f'proposal_not_found: {proposal_uid}')

    ev = row[6] if not isinstance(row[6], str) else json.loads(row[6])
    verdict, reason_code, detail = evaluate_l0_fields(
        {
            'category': row[0],
            'kind': row[1],
            'problem': row[2],
            'solution': row[3],
            'reusable_experience': row[4],
            'misbelief': row[5],
            'evidence': ev,
        },
        evidence_verifier=evidence_verifier,
        require_evidence=require_evidence,
    )
    l0_meta = {
        'field_lengths': {k: len(str(v or '').strip()) for k, v in (('problem', row[2]), ('solution', row[3]), ('reusable_experience', row[4]), ('misbelief', row[5]))},
        'limits': dict(L0_LIMITS),
        'require_evidence': bool(require_evidence),
        'evidence_count': len(ev) if isinstance(ev, list) else 0,
    }
    decision_id = 'dec-l0-' + uuid.uuid4().hex[:20]
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO proposal_decisions(
                decision_id, proposal_uid, layer, verdict, reason_code, confidence, judge_model, detail, meta_json
            ) VALUES (%s, %s, 'L0', %s, %s, %s, 'rule-l0', %s, %s::jsonb)
            """,
            (decision_id, proposal_uid, verdict, reason_code, 1.0, detail[:200], json.dumps(l0_meta, ensure_ascii=False)),
        )
        if verdict == 'reject':
            conn.execute(
                "UPDATE experience_proposals SET state = 'rejected', updated_at = now() WHERE uid = %s",
                (proposal_uid,),
            )
    return {
        'decision_id': decision_id,
        'layer': 'L0',
        'verdict': verdict,
        'reason_code': reason_code,
        'confidence': 1.0,
        'judge_model': 'rule-l0',
        'detail': detail[:200],
    }
