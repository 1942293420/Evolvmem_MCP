"""Regression tests for the read-only history-candidate surface.

Run with: .venv/bin/python -B -m pytest tests/test_history_candidate_read.py
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest

from evolvmem.history_candidate_read import (
    HistoryCandidateError,
    HistoryCandidateReader,
    format_beijing,
)
from evolvmem.history_candidate_web import dispatch


SCHEMA = """
CREATE TABLE projects(project TEXT PRIMARY KEY, display_name TEXT, status TEXT, revision INTEGER,
  created_at TEXT, updated_at TEXT, block_count INTEGER, message_count INTEGER, turn_count INTEGER,
  attachment_count INTEGER, first_at TEXT, last_at TEXT, summary TEXT, summary_purpose TEXT,
  summary_source TEXT, environments TEXT, evidence_status TEXT, intro_json TEXT);
CREATE TABLE blocks(block_id TEXT PRIMARY KEY, adapter TEXT, kind TEXT, family_key TEXT,
  native_session_id TEXT, native_device_id TEXT, native_id_evidence TEXT, title TEXT,
  message_count INTEGER, turn_count INTEGER, attachment_recovered INTEGER, attachment_missing INTEGER,
  observed_first_at TEXT, observed_last_at TEXT, ended_at TEXT, coverage_state TEXT,
  coverage_json TEXT, source_archive_ids TEXT, failed_archive_ids TEXT, projects TEXT,
  block_json TEXT, block_sha256 TEXT);
CREATE TABLE messages(block_id TEXT, ordinal INTEGER, role TEXT, content TEXT, recorded_at TEXT,
  archive_id INTEGER, message_index INTEGER, attachments TEXT, PRIMARY KEY(block_id,ordinal));
CREATE TABLE block_projects(block_id TEXT, project TEXT, evidence TEXT, PRIMARY KEY(block_id,project));
"""


def build_fixture(path: pathlib.Path, *, duplicate_display=False) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    intro = {"purpose": "集成平台用于统一管控自动化任务。",
             "environment": ["Linux 控制端"],
             "main_features": ["任务调度"],
             "necessary_info": ["不改正式库"],
             "unknowns": ["当前运行状态未核"],
             "historical_notes": ["2026-10-16 运行对应 2026-10-15 业务日；历史 43 条差异"],
             "source_refs": [{"kind": "file", "source": "README.md"},
                             {"kind": "project_event", "title": "事件一"},
                             {"kind": "event", "title": "冲突事件", "attribution_conflict": "归属未证实"},
                             {"kind": "project_event", "title": "平台冲突事件", "scope_conflict": True,
                              "note": "属另一平台流程，未合并"}]}
    projects = [
        ("automation_control", "自动化控制", "active", intro, "documented"),
        ("other", "其他项目", "active", None, None),
    ]
    if duplicate_display:
        projects.append(("other2", "自动化控制", "active", None, None))
    for key, display, status, intro_value, evidence in projects:
        conn.execute("""INSERT INTO projects(project,display_name,status,revision,created_at,updated_at,
            block_count,message_count,turn_count,attachment_count,first_at,last_at,summary,summary_purpose,
            summary_source,environments,evidence_status,intro_json)
            VALUES(?,?,?,1,'','',0,0,0,0,NULL,NULL,'','','','',?,?)""",
                     (key, display, status, evidence, json.dumps(intro_value, ensure_ascii=False) if intro_value else None))
    blocks = [
        ("blk-dialogue", "automation_control", "第一问", 4, 2, "2026-10-09T16:33:29.272Z", "2026-10-09T17:00:00Z", 1),
        ("blk-empty", "automation_control", "（无可见用户问题）", 0, 0, None, None, 0),
    ]
    for block_id, project, title, messages, turns, first, last, recovered in blocks:
        conn.execute("""INSERT INTO blocks(block_id,adapter,kind,title,message_count,turn_count,
            attachment_recovered,attachment_missing,observed_first_at,observed_last_at,ended_at,
            coverage_state,coverage_json,source_archive_ids,failed_archive_ids,projects,block_json,block_sha256)
            VALUES(?,?,?,?,?,?,?,0,?,?,NULL,?,?,?,?,'[]','blocks/x.json','abc')""",
                     (block_id, "codex", "whole_snapshot_or_legacy", title, messages, turns, recovered,
                      first, last, "whole_payload_observed",
                      json.dumps({"native_coverage_complete": False, "note": "observed"}),
                      json.dumps([3014]), "[]"))
        conn.execute("INSERT INTO block_projects(block_id,project,evidence) VALUES(?,?,?)",
                     (block_id, project, "saved"))
    for ordinal, role in enumerate(["user", "assistant", "user", "assistant"]):
        conn.execute("""INSERT INTO messages(block_id,ordinal,role,content,recorded_at,archive_id,
            message_index,attachments) VALUES(?,?,?,?,?,?,?,?)""",
                     ("blk-dialogue", ordinal, role, f"内容 {ordinal}",
                      "2026-10-09T16:33:29.272Z", 3014, ordinal, "[]"))
    conn.commit()
    conn.close()


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.db = self.root / "candidate.db"
        build_fixture(self.db)
        self.reader = HistoryCandidateReader(self.db)

    def tearDown(self):
        self.reader.close()
        self.temp.cleanup()

    def test_project_overview_lists_only_dialogue_blocks(self):
        data = self.reader.project_overview("automation_control")
        self.assertEqual(data["summary"]["purpose"], "集成平台用于统一管控自动化任务。")
        self.assertEqual(data["history_count"], 1)
        self.assertEqual(data["history"][0]["block_id"], "blk-dialogue")
        self.assertEqual(data["history"][0]["first_at_beijing"], "2026-10-10 00:33:29")
        self.assertEqual(data["empty_source_count"], 1)
        self.assertEqual(data["summary"]["historical_notes"],
                         ["2026-10-16 运行对应 2026-10-15 业务日；历史 43 条差异"])
        self.assertEqual(len(data["summary"]["source_conflict_refs"]), 2)
        self.assertEqual(len(data["summary"]["scope_conflicts"]), 1)
        self.assertIn("未合并", data["summary"]["scope_conflicts"][0]["note"])
        self.assertIsNone(data["boundaries"]["ended_at"])
        self.assertFalse(data["boundaries"]["native_coverage_complete"])

    def test_explicit_alias_resolves_only_automation_control(self):
        self.assertEqual(self.reader.resolve_project("集成平台")["project"], "automation_control")
        self.assertEqual(self.reader.resolve_project("集成化平台")["project"], "automation_control")
        with self.assertRaises(HistoryCandidateError) as raised:
            self.reader.resolve_project("集成")
        self.assertEqual(raised.exception.code, "project_not_registered")

    def test_display_name_match_is_exact_and_ambiguity_is_rejected(self):
        self.assertEqual(self.reader.resolve_project("自动化控制")["project"], "automation_control")
        self.reader.close()
        db2 = self.root / "duplicate.db"
        build_fixture(db2, duplicate_display=True)
        reader = HistoryCandidateReader(db2)
        try:
            with self.assertRaises(HistoryCandidateError) as raised:
                reader.resolve_project("自动化控制")
            self.assertEqual(raised.exception.code, "project_ambiguous")
        finally:
            reader.close()
        self.reader = HistoryCandidateReader(self.db)

    def test_block_pagination_is_stable_and_bounded(self):
        data = self.reader.block_page("blk-dialogue", 2, 2)
        self.assertEqual([m["ordinal"] for m in data["messages"]], [2, 3])
        self.assertEqual(data["total_pages"], 2)
        self.assertEqual(data["messages"][0]["recorded_at_beijing"], "2026-10-10 00:33:29")
        with self.assertRaises(HistoryCandidateError):
            self.reader.block_page("blk-dialogue", 0, 2)
        with self.assertRaises(HistoryCandidateError) as raised:
            self.reader.block_page("missing", 1, 2)
        self.assertEqual(raised.exception.code, "block_not_found")

    def test_reader_refuses_writes_and_leaves_file_unchanged(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with self.assertRaises(sqlite3.DatabaseError):
            self.reader.conn.execute("INSERT INTO projects(project) VALUES('x')")
        self.reader.conn.rollback()
        after = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(before, after)


class CrossPageTurnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.db = self.root / "candidate.db"
        build_fixture(self.db)
        conn = sqlite3.connect(self.db)
        self._add_block(conn, "blk-long", [("user", "长问答")] + [("assistant", f"a{i}") for i in range(60)])
        two = [("user", "Q1")] + [("assistant", f"q1a{i}") for i in range(3)] + \
              [("user", "Q2")] + [("assistant", f"q2a{i}") for i in range(3)]
        self._add_block(conn, "blk-two", two)
        conn.commit()
        conn.close()
        self.reader = HistoryCandidateReader(self.db)

    @staticmethod
    def _add_block(conn, block_id, rows):
        conn.execute("""INSERT INTO blocks(block_id,adapter,kind,title,message_count,turn_count,
            attachment_recovered,attachment_missing,observed_first_at,observed_last_at,ended_at,
            coverage_state,coverage_json,source_archive_ids,failed_archive_ids,projects,block_json,block_sha256)
            VALUES(?,?,?,?,?,?,0,0,?,?,NULL,'whole_payload_observed','{}','[]','[]','[]','x','x')""",
                     (block_id, "codex", "whole_snapshot_or_legacy", block_id, len(rows),
                      sum(1 for role, _ in rows if role == "user"),
                      "2026-10-09T16:33:29.272Z", "2026-10-09T17:00:00Z"))
        for ordinal, (role, content) in enumerate(rows):
            conn.execute("""INSERT INTO messages(block_id,ordinal,role,content,recorded_at,archive_id,
                message_index,attachments) VALUES(?,?,?,?,?,?,?,?)""",
                         (block_id, ordinal, role, content, "2026-10-09T16:33:29.272Z", 3014, ordinal, "[]"))

    def tearDown(self):
        self.reader.close()
        self.temp.cleanup()

    def test_message_page_2_marks_continuation_instead_of_missing_question(self):
        data = self.reader.block_page("blk-long", 2, 50)
        self.assertTrue(data["pagination"]["starts_mid_turn"])
        self.assertEqual(data["pagination"]["previous_question_ordinal"], 0)
        self.assertIn("不是会话缺少原问题", data["pagination"]["continuation_note"])
        self.assertEqual(data["messages"][0]["role"], "assistant")
        self.assertFalse(data["pagination"]["ends_mid_turn"])

    def test_turn_page_keeps_one_long_turn_intact(self):
        data = self.reader.block_turn_page("blk-long", 1, 50)
        self.assertEqual(data["total_pages"], 1)
        self.assertEqual(len(data["turns"]), 1)
        self.assertEqual(data["turns"][0]["question"]["content"], "长问答")
        self.assertEqual(len(data["turns"][0]["answers"]), 60)
        with self.assertRaises(HistoryCandidateError):
            self.reader.block_turn_page("blk-long", 2, 50)

    def test_turn_pages_never_split_or_rebind_answers(self):
        pages = []
        for page in (1, 2):
            data = self.reader.block_turn_page("blk-two", page, 5)
            pages.append(data)
            for turn in data["turns"]:
                self.assertIsNotNone(turn["question"])
                for answer in turn["answers"]:
                    self.assertTrue(answer["content"].startswith("q1a" if turn["question"]["content"] == "Q1" else "q2a"))
        self.assertEqual(len(pages[0]["turns"]), 1)
        self.assertEqual(pages[0]["turns"][0]["question"]["content"], "Q1")
        self.assertEqual(pages[1]["turns"][0]["question"]["content"], "Q2")
        self.assertIn("不是会话开头", pages[1]["pagination"]["continuation_note"])
        ordinals = [m["ordinal"] for data in pages for turn in data["turns"]
                    for m in ([turn["question"]] if turn["question"] else []) + turn["answers"]]
        self.assertEqual(ordinals, list(range(len(ordinals))))


class DispatchAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.db = self.root / "candidate.db"
        build_fixture(self.db)
        self.token = self.root / "history.token"
        self.token.write_text("s3cret-token", encoding="utf-8")
        os.chmod(self.token, 0o600)
        self.env = {
            "EVOLVMEM_HISTORY_CANDIDATE_DB": str(self.db),
            "EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE": str(self.token),
            "EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE": "personal",
        }
        self.headers = {"X-EvolvMem-History-Token": "s3cret-token",
                        "X-EvolvMem-Namespace": "personal"}

    def tearDown(self):
        self.temp.cleanup()

    def test_disabled_without_configuration(self):
        status, payload = dispatch("GET", "/api/history-candidate/projects", {}, {}, {})
        self.assertEqual((status, payload["error"]), (404, "history_candidate_disabled"))

    def test_token_and_namespace_are_required(self):
        status, payload = dispatch("GET", "/api/history-candidate/projects", {}, {}, self.env)
        self.assertEqual(status, 401)
        bad = dict(self.headers)
        bad["X-EvolvMem-History-Token"] = "wrong"
        status, _ = dispatch("GET", "/api/history-candidate/projects", {}, bad, self.env)
        self.assertEqual(status, 401)
        wrong_ns = dict(self.headers)
        wrong_ns["X-EvolvMem-Namespace"] = "other"
        status, _ = dispatch("GET", "/api/history-candidate/projects", {}, wrong_ns, self.env)
        self.assertEqual(status, 403)

    def test_authorized_reads_and_method_gate(self):
        status, payload = dispatch("GET", "/api/history-candidate/projects", {}, self.headers, self.env)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        status, payload = dispatch("GET", "/api/history-candidate/project/%E9%9B%86%E6%88%90%E5%B9%B3%E5%8F%B0",
                                   {}, self.headers, self.env)
        self.assertEqual(status, 200)
        self.assertEqual(payload["project"], "automation_control")
        status, payload = dispatch("GET", "/api/history-candidate/block/blk-dialogue",
                                   {"page": "1", "page_size": "3"}, self.headers, self.env)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["messages"]), 3)
        status, payload = dispatch("POST", "/api/history-candidate/block/blk-dialogue",
                                   {}, self.headers, self.env)
        self.assertEqual(status, 405)
        status, payload = dispatch("GET", "/api/history-candidate/unknown", {}, self.headers, self.env)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
