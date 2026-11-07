"""Owner-only HTML/attachment entry tests for the history candidate."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest

from evolvmem.history_candidate_view import handle, render_block
from evolvmem.history_candidate_web import dispatch
from tests.test_history_candidate_read import build_fixture


PNG = b"\x89PNG\r\n\x1a\n" + b"stage2-image-fixture"


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.db = self.root / "candidate.db"
        build_fixture(self.db)
        sha = hashlib.sha256(PNG).hexdigest()
        self.sha = sha
        conn = sqlite3.connect(self.db)
        conn.execute("""CREATE TABLE attachments(sha256 TEXT PRIMARY KEY,mime_type TEXT,bytes INTEGER,
            path TEXT,format_ok INTEGER,width INTEGER,height INTEGER,occurrences INTEGER)""")
        conn.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?)",
                     (sha, "image/png", len(PNG), f"ab/{sha}.png", 1, 12, 8, 1))
        conn.execute("""UPDATE messages SET attachments=? WHERE block_id='blk-dialogue' AND ordinal=0""",
                     (json.dumps([{"status": "recovered", "mime_type": "image/png", "sha256": sha,
                                   "bytes": len(PNG), "path": f"ab/{sha}.png", "format_ok": True,
                                   "width": 12, "height": 8}]),))
        conn.commit()
        conn.close()
        target = self.root / "attachments" / "ab" / f"{sha}.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(PNG)
        self.long_name = "采购 物流&成本-超长项目名称-用于URL编码验证-0123456789"
        conn = sqlite3.connect(self.db)
        conn.execute("""INSERT INTO projects(project,display_name,status,revision,created_at,updated_at,
            block_count,message_count,turn_count,attachment_count,first_at,last_at,summary,summary_purpose,
            summary_source,environments,evidence_status,intro_json)
            VALUES(?,?, 'active',1,'','',0,0,0,0,NULL,NULL,'','','','',?,?)""",
                     (self.long_name, self.long_name, "partial",
                      json.dumps({"purpose": "长项目名测试。", "environment": [],
                                  "main_features": [], "necessary_info": [], "unknowns": [],
                                  "historical_notes": [], "source_refs": []}, ensure_ascii=False)))
        rows = [("user", "Q1"), ("assistant", "A1"), ("assistant", "A2"),
                ("user", "Q2"), ("assistant", "B1"), ("assistant", "B2")]
        conn.execute("""INSERT INTO blocks(block_id,adapter,kind,title,message_count,turn_count,
            attachment_recovered,attachment_missing,observed_first_at,observed_last_at,ended_at,
            coverage_state,coverage_json,source_archive_ids,failed_archive_ids,projects,block_json,block_sha256)
            VALUES('blk-two','codex','whole_snapshot_or_legacy','两轮',6,2,0,0,
            '2026-10-09T16:33:29.272Z','2026-10-09T17:00:00Z',NULL,'whole_payload_observed','{}','[]','[]','[]','x','x')""")
        for ordinal, (role, content) in enumerate(rows):
            conn.execute("""INSERT INTO messages(block_id,ordinal,role,content,recorded_at,archive_id,
                message_index,attachments) VALUES('blk-two',?,?,?,?,3014,?,?)""",
                         (ordinal, role, content, "2026-10-09T16:33:29.272Z", ordinal, "[]"))
        conn.execute("""INSERT INTO blocks(block_id,adapter,kind,title,message_count,turn_count,
            attachment_recovered,attachment_missing,observed_first_at,observed_last_at,ended_at,
            coverage_state,coverage_json,source_archive_ids,failed_archive_ids,projects,block_json,block_sha256)
            VALUES('blk-uempty','codex','whole_snapshot_or_legacy','未归属空源',0,0,0,0,
            NULL,NULL,NULL,'no_visible_dialogue','{}','[]','[]','[]','x','x')""")
        conn.execute("""INSERT INTO blocks(block_id,adapter,kind,title,message_count,turn_count,
            attachment_recovered,attachment_missing,observed_first_at,observed_last_at,ended_at,
            coverage_state,coverage_json,source_archive_ids,failed_archive_ids,projects,block_json,block_sha256)
            VALUES('blk-lost','codex','whole_snapshot_or_legacy','丢失图',1,1,0,1,
            '2026-10-09T16:33:29.272Z','2026-10-09T16:33:29.272Z',NULL,'whole_payload_observed','{}','[]','[]','[]','x','x')""")
        conn.execute("""INSERT INTO messages(block_id,ordinal,role,content,recorded_at,archive_id,
            message_index,attachments) VALUES('blk-lost',0,'user','带丢失图 <image path="/tmp/lost.png"></image>',
            '2026-10-09T16:33:29.272Z',3014,0,?)""",
                     (json.dumps([{"status": "reference_not_recovered", "source_ref": "/tmp/lost.png"}]),))
        conn.commit()
        conn.close()
        self.env = {"EVOLVMEM_HISTORY_CANDIDATE_DB": str(self.db),
                    "EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE": str(self.root / "token"),
                    # Explicitly isolate from the root-accepted production audit.
                    # Keep both the named backup-audit variable and the reader's
                    # accepted-audit variable on absent temp paths.
                    "EVOLVMEM_HISTORY_BACKUP_AUDIT_FILE": str(self.root / "missing-backup-audit.json"),
                    "EVOLVMEM_HISTORY_ACCEPTED_AUDIT_FILE": str(self.root / "missing-accepted-audit.json")}
        self.coverage = self.root / "gap_audit.json"
        self.coverage.write_text(json.dumps({"schema": "evolvmem.gap_audit.v1",
            "projects": [{"project": "p"}], "empty_sources": [{"block_id": "b"}]}), encoding="utf-8")
        self.env["EVOLVMEM_HISTORY_COVERAGE_FILE"] = str(self.coverage)

    def tearDown(self):
        self.temp.cleanup()

    def test_owner_index_and_project_show_history_notes(self):
        status, ctype, body = handle("GET", "/history", {}, owner_verified=True, environ=self.env)
        self.assertEqual(status, 200)
        self.assertIn("项目摘要入口", body)
        status, _, body = handle("GET", "/history/project/%E9%9B%86%E6%88%90%E5%B9%B3%E5%8F%B0",
                                 {}, owner_verified=True, environ=self.env)
        self.assertEqual(status, 200)
        self.assertIn("历史记录（历史时点，不作为当前状态）", body)
        self.assertIn("2026-10-16 运行对应 2026-10-15 业务日", body)
        self.assertIn("平台范围冲突", body)
        self.assertIn("属另一平台流程", body)
        self.assertIn("blk-dialogue", body)

    def test_block_page_has_gated_image_and_hides_unrecovered(self):
        status, _, body = handle("GET", "/history/block/blk-dialogue", {}, owner_verified=True,
                                 environ=self.env)
        self.assertEqual(status, 200)
        self.assertIn(f'/history/attachment/{self.sha}', body)
        self.assertNotIn("SHA", body.split("<main>")[1])

    def test_attachment_streams_verified_bytes(self):
        status, ctype, body = handle("GET", f"/history/attachment/{self.sha}", {},
                                     owner_verified=True, environ=self.env)
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "image/png")
        self.assertEqual(body, PNG)
        status, _, _ = handle("GET", "/history/attachment/../etc/passwd", {},
                              owner_verified=True, environ=self.env)
        self.assertEqual(status, 404)
        status, _, _ = handle("GET", "/history/attachment/" + "0" * 64, {},
                              owner_verified=True, environ=self.env)
        self.assertEqual(status, 404)

    def test_viewer_or_unauthenticated_is_denied(self):
        status, _, body = handle("GET", "/history", {}, owner_verified=False, environ=self.env)
        self.assertEqual(status, 403)
        self.assertIn("owner", body)
        status, _, _ = handle("GET", f"/history/attachment/{self.sha}", {},
                              owner_verified=False, environ=self.env)
        self.assertEqual(status, 403)

    def test_cross_page_turn_page_keeps_question_and_answers_together(self):
        status, _, body = handle("GET", "/history/block/blk-two",
                                 {"page": "2", "turn_page_size": "4"},
                                 owner_verified=True, environ=self.env)
        self.assertEqual(status, 200)
        self.assertIn("Q2", body)
        self.assertIn("B1", body)
        self.assertIn("B2", body)
        self.assertNotIn("A1", body)
        self.assertNotIn("A2", body)
        self.assertIn("不是会话开头", body)
        self.assertNotIn("会话原始记录以助手内容开头", body)

    def test_unrecovered_image_is_hidden_in_owner_entry(self):
        status, _, body = handle("GET", "/history/block/blk-lost", {},
                                 owner_verified=True, environ=self.env)
        self.assertEqual(status, 200)
        self.assertNotIn("<image", body)
        self.assertNotIn("lost.png", body)
        self.assertIn("带丢失图", body)

    def test_long_project_name_is_url_encoded(self):
        from urllib.parse import quote
        status, _, body = handle("GET", "/history", {}, owner_verified=True, environ=self.env)
        self.assertEqual(status, 200)
        self.assertIn(f'/history/project/{quote(self.long_name, safe="")}', body)
        status, _, body = handle("GET", "/history/project/" + quote(self.long_name, safe=""),
                                 {}, owner_verified=True, environ=self.env)
        self.assertEqual(status, 200)
        self.assertIn("长项目名测试", body)

    def test_unassigned_and_turn_api_routes(self):
        status, payload = dispatch("GET", "/api/history-candidate/unassigned", {},
                                   {}, self.env, owner_verified=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["dialogue_count"], 2)
        self.assertEqual(payload["empty_count"], 1)
        status, payload = dispatch("GET", "/api/history-candidate/block/blk-two/turns",
                                   {"page": "2", "page_size": "4"}, {}, self.env,
                                   owner_verified=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["pagination"]["mode"], "turn")
        self.assertEqual(payload["turns"][0]["question"]["content"], "Q2")
        self.assertIn("不是会话开头", payload["pagination"]["continuation_note"])
        status, _ = dispatch("GET", "/api/history-candidate/unassigned", {}, {},
                             self.env, owner_verified=False)
        self.assertEqual(status, 401)

    def test_coverage_sidecar_is_owner_only_and_validated(self):
        status, payload = dispatch("GET", "/api/history-candidate/coverage", {}, {},
                                   self.env, owner_verified=True)
        self.assertEqual(status, 200)
        self.assertTrue(payload["deprecated"])
        self.assertNotIn("projects", payload)
        self.assertEqual(payload["counts"]["projects"], 1)
        status, payload = dispatch("GET", "/api/history-candidate/coverage/summary", {}, {},
                                   self.env, owner_verified=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"], {"projects": 1, "sources": 1})
        status, payload = dispatch("GET", "/api/history-candidate/coverage/projects",
                                   {"page": "1", "page_size": "5", "q": "p"}, {},
                                   self.env, owner_verified=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["group"], "projects")
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["project"], "p")
        self.assertNotIn("empty_sources", payload)
        status, payload = dispatch("GET", "/api/history-candidate/coverage/sources",
                                   {"page": "1", "page_size": "5", "q": "b"}, {},
                                   self.env, owner_verified=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["group"], "sources")
        self.assertEqual(payload["items"][0]["block_id"], "b")
        status, payload = dispatch("GET", "/api/history-candidate/coverage/source/b", {}, {},
                                   self.env, owner_verified=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["item"]["block_id"], "b")
        status, _ = dispatch("GET", "/api/history-candidate/coverage", {}, {},
                             self.env, owner_verified=False)
        self.assertEqual(status, 401)
        bad = self.root / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        bad_env = dict(self.env, EVOLVMEM_HISTORY_COVERAGE_FILE=str(bad))
        status, payload = dispatch("GET", "/api/history-candidate/coverage/summary", {}, {},
                                   bad_env, owner_verified=True)
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "history_candidate_coverage_invalid")
        missing_env = dict(self.env, EVOLVMEM_HISTORY_COVERAGE_FILE=str(self.root / "missing.json"))
        status, payload = dispatch("GET", "/api/history-candidate/coverage/summary", {}, {},
                                   missing_env, owner_verified=True)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "history_candidate_coverage_unavailable")

    def test_accepted_audit_overlay_pending_and_exact_join(self):
        status, payload = dispatch("GET", "/api/history-candidate/coverage/summary", {}, {},
                                   self.env, owner_verified=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["backup_audit"]["status"], "pending_acceptance")
        accepted = self.root / "accepted.json"
        accepted.write_text(json.dumps({
            "generated_at_local": "2026-11-07T12:34:00+08:00",
            "summary": {"found_original": 1, "not_found_in_checked_scope": 1},
            "scope": {"checked": "fixture backups"},
            "limitations": ["fixture limitation"],
            "items": [
                {"item_type": "project", "identity": {"project": "p"},
                 "status": "found_original", "conclusion": "fixture conclusion",
                 "evidence": "fixture evidence", "scope": {"paths": ["fixture/archive"]},
                 "limitations": ["fixture item limitation"]},
                {"item_type": "source", "identity": {"block_id": "b"},
                 "status": "not_found_in_checked_scope", "conclusion": "fixture source conclusion",
                 "evidence": "fixture source evidence", "scope": {}, "limitations": []},
                {"item_type": "project", "identity": {"project": "p2"},
                 "status": "linked_elsewhere", "conclusion": "not p"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        env = dict(self.env, EVOLVMEM_HISTORY_ACCEPTED_AUDIT_FILE=str(accepted),
                   EVOLVMEM_HISTORY_BACKUP_AUDIT_FILE=str(accepted))
        status, payload = dispatch("GET", "/api/history-candidate/coverage/summary", {}, {},
                                   env, owner_verified=True)
        self.assertEqual(status, 200)
        backup = payload["backup_audit"]
        self.assertEqual(backup["status"], "accepted")
        self.assertEqual(backup["item_count"], 3)
        self.assertEqual(backup["status_counts"]["found_original"], 1)
        status, payload = dispatch("GET", "/api/history-candidate/coverage/projects",
                                   {"page": "1", "page_size": "5"}, {}, env, owner_verified=True)
        row = payload["items"][0]
        self.assertEqual(row["backup_audit"]["status"], "found_original")
        self.assertEqual(row["backup_audit"]["status_label"], "已找到原文，待接入")
        self.assertNotIn("evidence", row["backup_audit"])
        status, payload = dispatch("GET", "/api/history-candidate/coverage/audit/project/p", {}, {},
                                   env, owner_verified=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["backup_audit"]["evidence"], "fixture evidence")
        status, payload = dispatch("GET", "/api/history-candidate/coverage/sources",
                                   {"page": "1", "page_size": "5"}, {}, env, owner_verified=True)
        self.assertEqual(payload["items"][0]["backup_audit"]["status_label"], "已查范围内未找到")
        status, payload = dispatch("GET", "/api/history-candidate/coverage/source/b", {}, {},
                                   env, owner_verified=True)
        self.assertEqual(payload["backup_audit"]["status"], "not_found_in_checked_scope")
        self.assertIn("fixture source evidence", payload["backup_audit"]["evidence"])
        status, payload = dispatch("GET", "/api/history-candidate/coverage/projects",
                                   {"page": "1", "page_size": "5"}, {},
                                   dict(env, EVOLVMEM_HISTORY_ACCEPTED_AUDIT_FILE=str(self.root / "missing.json")),
                                   owner_verified=True)
        self.assertNotIn("backup_audit", payload["items"][0])
        status, _ = dispatch("GET", "/api/history-candidate/coverage/audit/project/p", {}, {},
                             env, owner_verified=False)
        self.assertEqual(status, 401)
        invalid = self.root / "invalid-accepted.json"
        invalid.write_text("{}", encoding="utf-8")
        status, payload = dispatch("GET", "/api/history-candidate/coverage/summary", {}, {},
                                   dict(env, EVOLVMEM_HISTORY_ACCEPTED_AUDIT_FILE=str(invalid)),
                                   owner_verified=True)
        self.assertEqual(payload["backup_audit"]["status"], "invalid")

    def test_block_context_returns_persisted_projects(self):
        status, payload = dispatch("GET", "/api/history-candidate/block/blk-dialogue/context",
                                   {}, {}, self.env, owner_verified=True)
        self.assertEqual(status, 200)
        self.assertEqual(payload["block_id"], "blk-dialogue")
        self.assertIn("automation_control", [x["project"] for x in payload["projects"]])
        status, _ = dispatch("GET", "/api/history-candidate/block/blk-dialogue/context",
                             {}, {}, self.env, owner_verified=False)
        self.assertEqual(status, 401)

    def test_post_is_rejected_at_the_entry(self):
        status, ctype, body = handle("POST", "/history/project/x", {}, owner_verified=True,
                                     environ=self.env)
        self.assertEqual(status, 405)
        self.assertEqual(body["error"], "history_candidate_method_not_allowed")

    def test_json_api_accepts_owner_session_without_token(self):
        status, payload = dispatch("GET", "/api/history-candidate/projects", {}, {},
                                   self.env, owner_verified=True)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
