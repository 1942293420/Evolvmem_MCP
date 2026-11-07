"""Read-only access to the stage-2 history candidate database.

This is an additive reader, not a migration: it opens the candidate SQLite file
with ``mode=ro`` and a deny-write authorizer, and never touches the live
``memory.db``, the experience store, workspace bindings or project decisions.

Configuration is explicit and fail-closed (environment, no global switch):

* ``EVOLVMEM_HISTORY_CANDIDATE_DB`` — absolute path to the candidate database.
  Unset or missing => the reader is disabled (HTTP 404).
* ``EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE`` — owner-only file holding the
  personal read token. Unset/empty => disabled.
* ``EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE`` — expected namespace label,
  default ``personal``. Requests must echo it in ``X-EvolvMem-Namespace``.

Only the project key, display name or the explicit stage-2 aliases are resolved;
there is no fuzzy or substring matching and no project merging.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import pathlib
import re
import sqlite3
from typing import Any

BEIJING = dt.timezone(dt.timedelta(hours=8))
DB_ENV = "EVOLVMEM_HISTORY_CANDIDATE_DB"
TOKEN_ENV = "EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE"
NAMESPACE_ENV = "EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE"
DEFAULT_NAMESPACE = "personal"
TOKEN_HEADER = "X-EvolvMem-History-Token"
NAMESPACE_HEADER = "X-EvolvMem-Namespace"

# Explicit, user-confirmed read aliases only. "集成平台/集成化平台" refers to
# automation_control; no other project is folded into it.
READ_ALIASES = {
    "集成平台": "automation_control",
    "集成化平台": "automation_control",
}

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')


class HistoryCandidateError(Exception):
    """Bounded error whose code is safe to return to the caller."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def settings(environ: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    db_path = (env.get(DB_ENV) or "").strip()
    token_path = (env.get(TOKEN_ENV) or "").strip()
    namespace = (env.get(NAMESPACE_ENV) or DEFAULT_NAMESPACE).strip() or DEFAULT_NAMESPACE
    return {
        "db_path": pathlib.Path(db_path).expanduser() if db_path else None,
        "token_path": pathlib.Path(token_path).expanduser() if token_path else None,
        "namespace": namespace,
        "enabled": bool(db_path) and bool(token_path),
    }


def read_token(path: pathlib.Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        if path.stat().st_size > 4096:
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def authorize(headers, environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Validate the personal namespace token; raise a bounded error otherwise."""
    cfg = settings(environ)
    if not cfg["enabled"]:
        raise HistoryCandidateError("history_candidate_disabled", 404)
    supplied = headers.get(TOKEN_HEADER) if headers is not None else None
    namespace = headers.get(NAMESPACE_HEADER) if headers is not None else None
    expected = read_token(cfg["token_path"])
    if not expected or not supplied or not hmac.compare_digest(str(supplied), expected):
        raise HistoryCandidateError("history_candidate_unauthorized", 401)
    if not namespace or not hmac.compare_digest(str(namespace), cfg["namespace"]):
        raise HistoryCandidateError("history_candidate_namespace_mismatch", 403)
    return cfg


def format_beijing(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def _recovery_contract(evidence_status, intro):
    """Expose the data worker's explicit recovery object without inferring completeness."""
    recovered = intro.get("recovered") if isinstance(intro, dict) else None
    if not isinstance(recovered, dict):
        recovered = None
    partial = bool(recovered and recovered.get("partial"))
    if str(evidence_status or "") == "recovered_partial_missing_answers":
        partial = True
    reason = None
    if partial and recovered:
        reason = recovered.get("partial_reason") or recovered.get("note")
    return {
        "recovery": recovered,
        "recovery_status": recovered.get("status") if recovered else None,
        "partial": partial,
        "partial_reason": reason,
    }


class HistoryCandidateReader:
    """One short-lived, read-only connection to the candidate database."""

    def __init__(self, db_path: pathlib.Path):
        self.db_path = pathlib.Path(db_path)
        if not self.db_path.is_file():
            raise HistoryCandidateError("history_candidate_db_missing", 404)
        self.conn = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only=ON")
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                   sqlite3.SQLITE_PRAGMA, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_RECURSIVE}
        def authorizer(action, first, second, dbname, trigger):
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
        self.conn.set_authorizer(authorizer)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- project resolution -------------------------------------------------
    def _projects(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT project,display_name,status,intro_json,summary,evidence_status,"
            "block_count,message_count,turn_count,first_at,last_at FROM projects").fetchall()
        return {row["project"]: dict(row) for row in rows}

    def resolve_project(self, identifier: str) -> dict[str, Any]:
        if not isinstance(identifier, str) or not identifier.strip():
            raise HistoryCandidateError("project_required", 400)
        value = identifier.strip()
        projects = self._projects()
        if value in projects:
            return projects[value]
        alias_target = READ_ALIASES.get(value)
        if alias_target and alias_target in projects:
            return projects[alias_target]
        normalised = value.casefold()
        matches = [row for key, row in projects.items()
                   if (row.get("display_name") or "").strip().casefold() == normalised]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise HistoryCandidateError("project_ambiguous", 409)
        raise HistoryCandidateError("project_not_registered", 404)

    def projects(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT p.project,p.display_name,p.status,p.evidence_status,p.message_count,p.turn_count,p.intro_json,
                      (SELECT count(*) FROM blocks b JOIN block_projects bp ON bp.block_id=b.block_id
                        WHERE bp.project=p.project AND b.message_count>0) AS dialogue_blocks,
                      (SELECT count(*) FROM blocks b JOIN block_projects bp ON bp.block_id=b.block_id
                        WHERE bp.project=p.project AND b.message_count=0) AS empty_blocks
               FROM projects p ORDER BY dialogue_blocks DESC, p.message_count DESC, p.project""").fetchall()
        result = []
        for row in rows:
            intro = {}
            if row["intro_json"]:
                try:
                    intro = json.loads(row["intro_json"])
                except ValueError:
                    intro = {}
            result.append({
                "project": row["project"],
                "display_name": row["display_name"] or "",
                "status": row["status"] or "",
                "evidence_status": row["evidence_status"],
                "dialogue_blocks": row["dialogue_blocks"],
                "empty_sources": row["empty_blocks"],
                "messages": row["message_count"] or 0,
                "turns": row["turn_count"] or 0,
                **_recovery_contract(row["evidence_status"], intro),
            })
        return result

    # ---- read projections --------------------------------------------------
    def project_overview(self, identifier: str) -> dict[str, Any]:
        project = self.resolve_project(identifier)
        key = project["project"]
        intro = {}
        if project.get("intro_json"):
            try:
                intro = json.loads(project["intro_json"])
            except ValueError:
                intro = {}
        blocks = [dict(row) for row in self.conn.execute(
            """SELECT b.block_id,b.title,b.message_count,b.turn_count,b.observed_first_at,
                      b.observed_last_at,b.coverage_state,b.attachment_recovered
               FROM blocks b JOIN block_projects p ON p.block_id=b.block_id
               WHERE p.project=? AND b.message_count>0
               ORDER BY (b.observed_first_at IS NULL),b.observed_first_at,b.block_id""", (key,))]
        empty_count = self.conn.execute(
            """SELECT count(*) FROM blocks b JOIN block_projects p ON p.block_id=b.block_id
               WHERE p.project=? AND b.message_count=0""", (key,)).fetchone()[0]
        history = [{
            "block_id": row["block_id"],
            "title": row["title"],
            "messages": row["message_count"],
            "turns": row["turn_count"],
            "first_at_beijing": format_beijing(row["observed_first_at"]),
            "last_at_beijing": format_beijing(row["observed_last_at"]),
            "coverage": row["coverage_state"],
            "attachments": row["attachment_recovered"],
            "ended_at": None,
        } for row in blocks]
        source_refs = intro.get("source_refs") or []
        scope_conflicts = [ref for ref in source_refs if ref.get("scope_conflict")]
        conflicts = [ref for ref in source_refs
                     if ref.get("attribution_conflict") or ref.get("scope_conflict")
                     or "conflict" in str(ref.get("note") or "").lower()
                     or "conflict" in str(ref.get("status") or "").lower()]
        return {
            "ok": True,
            "project": key,
            "display_name": project.get("display_name") or "",
            "status": project.get("status") or "",
            "evidence_status": project.get("evidence_status"),
            **_recovery_contract(project.get("evidence_status"), intro),
            "summary": {
                "purpose": intro.get("purpose") or (project.get("summary") or ""),
                "environment": intro.get("environment") or [],
                "main_features": intro.get("main_features") or [],
                "necessary_info": intro.get("necessary_info") or [],
                "unknowns": intro.get("unknowns") or [],
                "historical_notes": intro.get("historical_notes") or [],
                "source_conflict_refs": conflicts,
                "scope_conflicts": scope_conflicts,
                "source_ref_count": len(source_refs),
                "source_refs": source_refs,
            },
            "history": history,
            "history_count": len(history),
            "empty_source_count": empty_count,
            "boundaries": {
                "ended_at": None,
                "native_coverage_complete": False,
                "project_relation_is_not_whole_block_access_grant": True,
                "semantic_split": False,
                "historical_notes_are_not_current_state": True,
            },
        }

    # ---- block reads -------------------------------------------------------
    def _block(self, block_id: str):
        if not isinstance(block_id, str) or not block_id.strip():
            raise HistoryCandidateError("block_required", 400)
        block = self.conn.execute(
            """SELECT block_id,title,adapter,kind,native_session_id,message_count,turn_count,
                      observed_first_at,observed_last_at,coverage_state,coverage_json,
                      attachment_recovered,attachment_missing,source_archive_ids
               FROM blocks WHERE block_id=?""", (block_id.strip(),)).fetchone()
        if block is None:
            raise HistoryCandidateError("block_not_found", 404)
        return block

    @staticmethod
    def _block_meta(block) -> dict[str, Any]:
        return {
            "block_id": block["block_id"],
            "title": block["title"],
            "adapter": block["adapter"],
            "kind": block["kind"],
            "native_session_id": block["native_session_id"],
            "messages": block["message_count"],
            "turns": block["turn_count"],
            "coverage": block["coverage_state"],
            "coverage_detail": json.loads(block["coverage_json"]),
            "attachments_recovered": block["attachment_recovered"],
            "attachments_missing": block["attachment_missing"],
            "source_archive_ids": json.loads(block["source_archive_ids"] or "[]"),
            "first_at_raw": block["observed_first_at"],
            "last_at_raw": block["observed_last_at"],
            "first_at_beijing": format_beijing(block["observed_first_at"]),
            "last_at_beijing": format_beijing(block["observed_last_at"]),
            "ended_at": None,
        }

    def block_projects(self, block_id: str) -> dict[str, Any]:
        """Return persisted project associations for one block (owner surface)."""
        block = self._block(block_id)
        rows = self.conn.execute(
            "SELECT project,evidence FROM block_projects WHERE block_id=? ORDER BY project",
            (block["block_id"],)).fetchall()
        return {
            "ok": True,
            "block_id": block["block_id"],
            "title": block["title"],
            "projects": [{"project": row["project"], "evidence": row["evidence"]} for row in rows],
        }

    @staticmethod
    def _message(row) -> dict[str, Any]:
        message = dict(row)
        message["recorded_at_beijing"] = format_beijing(message.get("recorded_at"))
        message["attachments"] = json.loads(message.get("attachments") or "[]")
        return message

    def _all_messages(self, block_id: str) -> list[dict[str, Any]]:
        return [self._message(row) for row in self.conn.execute(
            """SELECT ordinal,role,content,recorded_at,archive_id,attachments
               FROM messages WHERE block_id=? ORDER BY ordinal""", (block_id,))]

    def turns_for_block(self, block_id: str) -> list[dict[str, Any]]:
        """Group the whole block into complete Q&A turns (no semantic split)."""
        block = self._block(block_id)
        turns: list[dict[str, Any]] = []
        current = None
        for message in self._all_messages(block["block_id"]):
            if message["role"] == "user":
                current = {"turn_index": len(turns) + 1, "question": message, "answers": []}
                turns.append(current)
            else:
                if current is None:
                    current = {"turn_index": 1, "question": None, "answers": []}
                    turns.append(current)
                current["answers"].append(message)
        return turns

    def block_page(self, block_id: str, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
        """Message-based API page, with explicit mid-turn continuation semantics."""
        block = self._block(block_id)
        try:
            page = int(page)
            page_size = int(page_size)
        except (TypeError, ValueError):
            raise HistoryCandidateError("invalid_pagination", 400)
        if page < 1 or page_size < 1:
            raise HistoryCandidateError("invalid_pagination", 400)
        page_size = min(page_size, MAX_PAGE_SIZE)
        total = block["message_count"]
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            raise HistoryCandidateError("page_out_of_range", 404)
        offset = (page - 1) * page_size
        rows = [self._message(row) for row in self.conn.execute(
            """SELECT ordinal,role,content,recorded_at,archive_id,attachments
               FROM messages WHERE block_id=? ORDER BY ordinal LIMIT ? OFFSET ?""",
            (block["block_id"], page_size, offset))]
        first_ordinal = rows[0]["ordinal"] if rows else None
        last_ordinal = rows[-1]["ordinal"] if rows else None
        previous_question = self.conn.execute(
            """SELECT ordinal FROM messages WHERE block_id=? AND role='user' AND ordinal<?
               ORDER BY ordinal DESC LIMIT 1""", (block["block_id"], first_ordinal)).fetchone()
        next_message = self.conn.execute(
            """SELECT role FROM messages WHERE block_id=? AND ordinal=?""",
            (block["block_id"], (last_ordinal or -1) + 1)).fetchone()
        starts_mid_turn = bool(rows and rows[0]["role"] != "user" and page > 1)
        ends_mid_turn = bool(rows and rows[-1]["role"] == "assistant"
                             and next_message is not None and next_message["role"] == "assistant")
        if starts_mid_turn:
            note = ("本页从上一页同一问答的助手续文开始；原问题在上一页"
                    + (f"（ordinal {previous_question['ordinal']}）" if previous_question else "")
                    + "，不是会话缺少原问题。")
        elif page > 1:
            note = "本页接续前面的问答，不是会话开头；完整会话仍是一个逻辑块。"
        else:
            note = "本页从会话已捕获内容的开头开始。"
        return {
            "ok": True,
            "block": self._block_meta(block),
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "messages": rows,
            "pagination": {
                "mode": "message",
                "starts_mid_turn": starts_mid_turn,
                "ends_mid_turn": ends_mid_turn,
                "previous_question_ordinal": previous_question["ordinal"] if previous_question else None,
                "first_ordinal": first_ordinal,
                "last_ordinal": last_ordinal,
                "continuation_note": note,
                "turn_based_ui": "/history/block/<id> is turn-paginated",
            },
        }

    def block_turn_page(self, block_id: str, page: int = 1,
                        max_messages: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
        """Turn-aware page for the UI: whole Q&A turns, never split or rebound."""
        block = self._block(block_id)
        try:
            page = int(page)
            max_messages = int(max_messages)
        except (TypeError, ValueError):
            raise HistoryCandidateError("invalid_pagination", 400)
        if page < 1 or max_messages < 1:
            raise HistoryCandidateError("invalid_pagination", 400)
        max_messages = min(max_messages, MAX_PAGE_SIZE)
        turns = self.turns_for_block(block["block_id"])
        pages: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        count = 0
        for turn in turns:
            size = 1 + len(turn["answers"])
            if current and count + size > max_messages:
                pages.append(current)
                current, count = [], 0
            current.append(turn)
            count += size
        if current:
            pages.append(current)
        if not pages:
            pages = [[]]
        if page > len(pages):
            raise HistoryCandidateError("page_out_of_range", 404)
        first_turn = pages[page - 1][0]["turn_index"] if pages[page - 1] else None
        last_turn = pages[page - 1][-1]["turn_index"] if pages[page - 1] else None
        note = None
        if page > 1:
            note = (f"本页从第 {first_turn} 个问答继续（接续前面的问答，不是会话开头）；"
                    f"每个问答在同一页内完整呈现，完整会话仍是一个逻辑块。")
        return {
            "ok": True,
            "block": self._block_meta(block),
            "page": page,
            "total_pages": len(pages),
            "turns": pages[page - 1],
            "pagination": {
                "mode": "turn",
                "first_turn_index": first_turn,
                "last_turn_index": last_turn,
                "continuation_note": note,
                "whole_turns": True,
            },
        }

    def unassigned(self) -> dict[str, Any]:
        def rows(has_dialogue: bool):
            return [{
                "block_id": row["block_id"],
                "title": row["title"],
                "messages": row["message_count"],
                "turns": row["turn_count"],
                "first_at_beijing": format_beijing(row["observed_first_at"]),
                "last_at_beijing": format_beijing(row["observed_last_at"]),
                "coverage": row["coverage_state"],
            } for row in self.conn.execute(
                """SELECT b.block_id,b.title,b.message_count,b.turn_count,b.observed_first_at,
                          b.observed_last_at,b.coverage_state
                   FROM blocks b WHERE b.message_count """ + ("> 0" if has_dialogue else "= 0") + """
                     AND NOT EXISTS (SELECT 1 FROM block_projects p WHERE p.block_id=b.block_id)
                   ORDER BY (b.observed_first_at IS NULL),b.observed_first_at,b.block_id""")]
        dialogue = rows(True)
        empty = rows(False)
        return {"ok": True, "dialogue": dialogue, "empty": empty,
                "dialogue_count": len(dialogue), "empty_count": len(empty)}

    def attachment(self, sha256: str) -> dict[str, Any]:
        """Resolve one restored attachment by content SHA, validated on disk."""
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            raise HistoryCandidateError("attachment_not_found", 404)
        row = self.conn.execute(
            "SELECT sha256,mime_type,bytes,path FROM attachments WHERE sha256=?",
            (sha256,)).fetchone()
        if row is None or not row["path"]:
            raise HistoryCandidateError("attachment_not_found", 404)
        root = (self.db_path.parent / "attachments").resolve()
        candidate = (root / row["path"]).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise HistoryCandidateError("attachment_not_found", 404)
        digest = hashlib.sha256()
        size = 0
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
        if size != row["bytes"] or digest.hexdigest() != row["sha256"]:
            raise HistoryCandidateError("attachment_changed", 409)
        return {"sha256": row["sha256"], "mime_type": row["mime_type"] or "application/octet-stream",
                "bytes": row["bytes"], "path": candidate}


def open_reader(environ: dict[str, str] | None = None) -> HistoryCandidateReader:
    cfg = settings(environ)
    if not cfg["enabled"]:
        raise HistoryCandidateError("history_candidate_disabled", 404)
    from evolvmem.history_management import ManagedReader, State, management_path
    path = management_path(environ)
    scope = hashlib.sha256((str(cfg['db_path'].resolve().parent.parent) + '\n' + str(cfg['namespace'])).encode()).hexdigest()
    return ManagedReader(cfg["db_path"], State(path, scope))


def database_fingerprint(path: pathlib.Path) -> tuple[int, str]:
    """Size and SHA256 for read-only proof; never returns content."""
    digest = hashlib.sha256()
    size = 0
    with pathlib.Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()
