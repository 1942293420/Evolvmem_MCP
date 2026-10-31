"""Read-only source coverage and exact, bounded restoration of current work.

No models run here. prepare opens no service; apply reuses enqueue/retry and
never resegments or clears protection. Only explicit repair_cleaning decrypts
the bounded, missing-body entries selected by a content-addressed plan.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from evolvmem.capability_provenance import ProvenanceRead, ReadStore
from evolvmem.context_store import _now_iso
from evolvmem import ownership_reasoning

VERSION = 1
AUDIT = "governance_migration_audit"
QUEUE_FIELDS = ("stage", "status", "error_code", "error_detail", "finished_at", "updated_at")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params)]


def _has(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _plan_hash(plan):
    return _hash({k: v for k, v in plan.items() if k != "plan_sha256"})


def _validate(plan):
    if (not isinstance(plan, dict) or plan.get("version") != VERSION
            or plan.get("kind") != "current_source_governance"
            or plan.get("policy_sha256") != plan.get("rule_revision")
            or plan.get("ownership_policy_version") != ownership_reasoning.VERSION
            or plan.get("plan_sha256") != _plan_hash(plan)):
        raise ValueError("invalid_migration_plan")
    for name in ("actions", "cleaning_repairs"):
        entries = plan.get(name)
        if not isinstance(entries, list) or len({e["source_key"] for e in entries}) != len(entries):
            raise ValueError("invalid_migration_plan")


def _limit(limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("invalid_migration_limit")
    return limit


class Inventory:
    """A single read snapshot; no raw decrypt, schema initialization or provider."""
    def __init__(self, conn, rule_revision, *, keys=None):
        from evolvmem import history_organization as history
        self.conn, self.rule_revision = conn, rule_revision
        self.store = ReadStore(conn)
        self.service = SimpleNamespace(store=self.store)
        self.reader = ProvenanceRead(self.store)
        self.excluded = history._excluded_archive_ids(self.service)
        self.unassigned = {r["id"] for r in history._unassigned_archives(self.service, excluded=self.excluded)}
        from evolvmem import governance_sources
        self.enrolled = governance_sources.valid_ids(self.service)
        self.visible = governance_sources.visible_ids(self.service, excluded=self.excluded)
        self.reader.visible = self.visible
        self.refs = {r["key"] for r in history.source_refs(self.service)} | {r['key'] for r in governance_sources.pending_refs(self.service)}
        self.archives = {r["id"]: r for r in _rows(conn, "SELECT * FROM session_archives ORDER BY id")}
        self.storage = {r["archive_id"]: r for r in _rows(conn, "SELECT * FROM session_archive_storage")} if _has(conn, "session_archive_storage") else {}
        # Current family heads include already-assigned archives for cleaning coverage.
        from evolvmem.session_identity import identity_key
        ignored = set(self.excluded)
        if _has(conn, "lan_session_uploads") and _has(conn, "lan_session_heads"):
            ignored.update(r[0] for r in conn.execute(
                "SELECT u.archive_id FROM lan_session_uploads u JOIN lan_session_heads h "
                "ON h.device_id=u.device_id AND h.session_id=u.session_id "
                "WHERE h.sha256 IS NOT u.sha256 AND u.archive_id IS NOT NULL"))
        seen = set()
        self.current_archives = set()
        for row in sorted(self.archives.values(), key=lambda r: (r["created_at"], r["id"]), reverse=True):
            family = identity_key(row["adapter"], row["external_session_id"])
            if row["id"] in ignored or family in seen:
                continue
            seen.add(family)
            self.current_archives.add(row["id"])
        self.source_hashes = {}
        self.clean = {r["archive_id"]: r for r in _rows(conn, "SELECT archive_id,length(body) AS chars,content_hash FROM conversation_history")}
        self.reviews = {r["source_key"]: r for r in _rows(conn, "SELECT * FROM knowledge_cleaning_reviews")}
        self.manifests = {r["source_key"]: r for r in _rows(conn, "SELECT * FROM organization_conversations ORDER BY source_key")}
        self.tasks = defaultdict(list)
        self.units = defaultdict(list)
        selected_keys = set(keys) if keys is not None else None
        if selected_keys is not None:
            for key in list(selected_keys):
                if key in self.manifests:
                    selected_keys.update(json.loads(self.manifests[key]["members"]))
        task_sql = "SELECT * FROM organization_tasks"
        params = []
        if selected_keys is not None:
            params = sorted(selected_keys)
            task_sql += " WHERE source_key IN (" + ",".join("?" for _ in params) + ")" if params else " WHERE 0"
        for raw in conn.execute(task_sql + " ORDER BY id", params):
            row = dict(raw)
            row_hash = _hash(row)
            row["snapshot_chars"] = len(row.pop("source_snapshot"))
            # The fingerprint binds all fields; long narrative/snapshot bodies
            # are never retained for a whole-library scan or exported in a plan.
            row.pop("task_draft", None)
            row["_row_hash"] = row_hash
            self.tasks[row["source_key"]].append(row)
        unit_sql = "SELECT u.* FROM organization_units u"
        if selected_keys is not None:
            unit_sql += " JOIN organization_tasks t ON t.id=u.task_id"
            unit_sql += " WHERE t.source_key IN (" + ",".join("?" for _ in params) + ")" if params else " WHERE 0"
        for raw in conn.execute(unit_sql + " ORDER BY u.task_id,u.ordinal", params):
            row = dict(raw)
            fields = ("task_id", "ordinal", "decision", "disposition", "project", "projects", "knowledge_scope", "extraction_stage")
            self.units[row["task_id"]].append({**{k: row[k] for k in fields}, "_row_hash": _hash(row),
                "ownership_current": bool(ownership_reasoning.validated(row, self.rule_revision))})
        self.protection = defaultdict(list)
        for row in _rows(conn, "SELECT * FROM organization_scope_exclusions ORDER BY source_key,digest,scope_key"):
            self.protection[row["source_key"]].append(row)
        self.active_projects = self.reader.active_projects
        self.registry_hash = _hash(_rows(conn, "SELECT * FROM context_project_registry ORDER BY project"))
        self.covered = defaultdict(list)
        # Even a failed or orphaned current manifest reserves its members.
        # Enqueue that logical conversation once, never each fragment again.
        for key, manifest in self.manifests.items():
            members = json.loads(manifest["members"])
            if keys is not None and not set(members).intersection(keys):
                continue
            revision, reason = self.reader.source_revision(key)
            if not reason:
                for member in members:
                    self.covered[member].append(key)

    def _source_state(self, key, visiting=()):
        if key in visiting:
            return ["cycle"]
        if key.startswith("archive:"):
            identity = int(key.split(":")[1])
            from evolvmem.governance_sources import _grants
            return [self.archives.get(identity), self.storage.get(identity), _rows(self.conn, "SELECT * FROM conversation_history WHERE archive_id=?", (identity,)), self.reviews.get(key), _grants(self.conn).get(identity)]
        if key.startswith("conversation:"):
            manifest = self.manifests.get(key)
            members = json.loads(manifest["members"]) if manifest else []
            # register() refreshes updated_at even when the logical identity and
            # membership are unchanged. Bind every semantic manifest field and
            # each original, while excluding only that refresh timestamp.
            state = {k: v for k, v in manifest.items() if k != "updated_at"} if manifest else None
            return [state, self.reviews.get(key), [self.source_state(k, (*visiting, key)) for k in members]]
        revision, reason = self.reader.source_revision(key)
        return [revision, reason, self.reviews.get(key)]

    def source_state(self, key, visiting=()):
        if key not in self.source_hashes:
            self.source_hashes[key] = _hash(self._source_state(key, visiting))
        return self.source_hashes[key]

    def stamp(self, key):
        tasks = self.tasks[key]
        members = json.loads(self.manifests[key]["members"]) if key in self.manifests else []
        prior = [[member, self.tasks[member], [self.units[t["id"]] for t in self.tasks[member]],
                  self.protection[member]] for member in members]
        return _hash([self.source_state(key), tasks, [self.units[t["id"]] for t in tasks],
                      self.protection[key], prior, self.registry_hash, self.covered.get(key, [])])

    def _unresolved(self, unit):
        if unit["disposition"] == "set_aside":
            return False
        if unit["decision"] not in {"auto", "manual"} or unit["disposition"] == "review":
            return True
        if unit["knowledge_scope"] == "global":
            return False
        projects = json.loads(unit["projects"] or "[]") or ([unit["project"]] if unit["project"] else [])
        return not projects or not set(projects).issubset(self.active_projects)

    def entry(self, key):
        from evolvmem.session_identity import identity_key, is_incremental_batch
        family = key.split(":")[0]
        revision, reason = "", ""
        if family != "archive" or int(key.split(":")[1]) in self.visible:
            revision, reason = self.reader.source_revision(key)
        tasks = self.tasks[key]
        current = [t for t in tasks if t["status"] != "superseded" and not t["superseded_by"]
                   and t["source_revision"] == revision and revision]
        task = current[-1] if current else None
        units = self.units[task["id"]] if task else []
        state = "not_applicable"
        chars = 0
        logical_family = key
        if family == "archive":
            archive = self.archives[int(key.split(":")[1])]
            clean = self.clean.get(archive["id"])
            state = "missing" if clean is None else "present" if clean["chars"] else "empty_unverified"
            chars = clean["chars"] if clean else 0
            logical_family = list(identity_key(archive["adapter"], archive["external_session_id"]))
            if archive["id"] in self.excluded:
                reason = "system_source"
            elif self.reviews.get(key, {}).get("state") == "deleted":
                reason = "source_deleted"
            elif archive["state"] != "available":
                reason = "archive_unavailable"
            elif archive["project"] and archive['id'] not in self.enrolled:
                reason = "already_assigned_archive"
            elif archive["id"] not in self.visible:
                reason = "retired_snapshot"
            raw = {"state": archive["state"], "payload_sha256": archive["payload_sha256"],
                   "storage": "encrypted_manifest_and_compressed_chunks" if archive["id"] in self.storage else "encrypted_payload", "integrity": "metadata_only_not_decrypted",
                   "incremental_batch": is_incremental_batch(archive["external_session_id"])}
        else:
            raw = None
            chars = task["snapshot_chars"] if task else 0
            if family == "conversation":
                logical_family = self.manifests[key]["identity"]
                if not chars:
                    chars = sum(self.clean.get(int(k.split(":")[1]), {}).get("chars", 0)
                                for k in json.loads(self.manifests[key]["members"]) if k.startswith("archive:"))
            elif family == "item":
                identity = int(key.split(":")[1])
                layers = _rows(self.conn, "SELECT layer,length(content) chars FROM context_layers WHERE item_id=?", (identity,))
                chars = max((r["chars"] for r in layers), default=0)
        unresolved = sum(self._unresolved(u) for u in units)
        automatic = sum(self._unresolved(u) and u["decision"] != "manual" for u in units)
        outdated_ownership = sum(u["decision"] == "auto" and u["disposition"] in {"keep", "history_only"}
                                 and not u["ownership_current"] for u in units)
        entry = {"source_key": key, "family": family, "logical_family": logical_family,
                 "current_revision": revision, "current_task_id": task["id"] if task else None,
                 "task_status": task["status"] if task else None,
                 "task_status_counts": dict(Counter(t["status"] for t in tasks)),
                 "units": len(units), "historical_units": sum(len(self.units[t["id"]]) for t in tasks if t != task),
                 "manual_units": sum(u["decision"] == "manual" for u in units),
                 "unresolved_units": unresolved, "automatic_unresolved_units": automatic,
                 "outdated_ownership_units": outdated_ownership,
                 "estimated_chars": chars, "cleaning": {"body_state": state,
                 "review_state": self.reviews.get(key, {}).get("state", "absent"),
                 "review_current": bool(self.reviews.get(key) and revision and
                     ProvenanceRead._review_revision(self.reviews[key]["source_revision"], self.reviews[key]) == revision)},
                 "raw": raw, "action": None, "reason": reason,
                 "current_archive": family == "archive" and int(key.split(":")[1]) in self.current_archives}
        if reason:
            return entry
        if len(current) > 1:
            entry["reason"] = "ambiguous_current_tasks"
        elif self.covered.get(key):
            entry["reason"] = "conversation_member"
        elif task:
            if task["error_code"] == "source_no_dialogue":
                entry["reason"] = "verified_no_dialogue"
            elif task["rule_revision"] != self.rule_revision:
                entry["reason"] = "rule_revision_changed"
            elif task["status"] == "running":
                entry["reason"] = "worker_running"
            elif task["status"] == "pending":
                entry["reason"] = "already_pending"
            elif task["status"] in {"completed", "review"} and outdated_ownership:
                # Completed AUTO decisions still belong to the old reasoning
                # policy. Resume assignment; retry preserves all manual rows
                # and the worker revalidates the original source before judging.
                entry["action"] = "retry"
                entry["reason"] = "ownership_policy_changed"
            elif task["status"] == "failed" or (task["status"] in {"completed", "review"} and automatic):
                entry["action"] = "retry"
                entry["reason"] = "completed_unassigned" if task["status"] == "completed" else "retry_current_work"
            else:
                entry["reason"] = "manual_attention" if unresolved else "already_organized"
        elif tasks:
            # Do not bypass a retired job's protection by silently creating a
            # fresh same-source task. A separate explicit revision upgrade owns it.
            entry["reason"] = "historical_tasks_require_upgrade"
        elif key in self.refs or family == "conversation":
            entry["action"] = "enqueue"
            entry["reason"] = "unprocessed_current_source"
        else:
            entry["reason"] = "source_not_in_current_queue"
        return entry


def build_plan(conn, *, rule_revision):
    """SELECT-only coverage; use a read transaction for a consistent snapshot."""
    inventory = Inventory(conn, rule_revision)
    keys = {f"archive:{identity}" for identity in inventory.archives}
    keys.update(inventory.manifests)
    keys.update(k for k in inventory.refs if k.startswith("item:"))
    keys.update(k for k in inventory.tasks if k.startswith("item:"))
    sources, actions, repairs = [], [], []
    for key in sorted(keys):
        entry = inventory.entry(key)
        sources.append(entry)
        if entry["action"]:
            actions.append({"source_key": key, "action": entry["action"],
                            "task_id": entry["current_task_id"], "expected": inventory.stamp(key),
                            "source_revision": entry["current_revision"]})
        if (entry["current_archive"] and entry["raw"]["state"] == "available"
                and entry["reason"] in {"source_body_unverifiable", "already_assigned_archive"}
                and entry["cleaning"]["body_state"] == "missing"):
            repairs.append({"source_key": key, "expected": inventory.stamp(key)})
    actions.sort(key=lambda a: (a["action"] != "retry", a["source_key"]))
    current_tasks = {e["current_task_id"]: e for e in sources if e["current_task_id"]}
    summary = {
        "archives_total": len(inventory.archives),
        "current_unassigned_archives": len(inventory.unassigned),
        "current_enrolled_assigned_archives": len(inventory.enrolled - inventory.unassigned),
        "current_logical_archives": len(inventory.current_archives),
        "current_tasks": len(current_tasks),
        "current_tasks_by_status": dict(Counter(e["task_status"] for e in current_tasks.values())),
        "current_units": sum(e["units"] for e in current_tasks.values()),
        "completed_unassigned_tasks": sum(e["task_status"] == "completed" and e["unresolved_units"] > 0 for e in current_tasks.values()),
        "selected_work": len(actions), "by_action": dict(Counter(a["action"] for a in actions)),
        "by_family": dict(Counter(a["source_key"].split(":")[0] for a in actions)),
        "missing_current_bodies": len(repairs),
        "current_archive_body_states": dict(Counter(e["cleaning"]["body_state"] for e in sources if e["family"] == "archive" and int(e["source_key"].split(":")[1]) in inventory.visible)),
        "reasons": dict(Counter(e["reason"] for e in sources)),
        "all_archive_body_states": dict(Counter(e["cleaning"]["body_state"] for e in sources if e["family"] == "archive")),
        "current_logical_body_states": dict(Counter(e["cleaning"]["body_state"] for e in sources if e["current_archive"])),
        "raw_archive_states": dict(Counter(a["state"] for a in inventory.archives.values())),
        "current_manual_units": sum(e["manual_units"] for e in current_tasks.values()),
        "selected_estimated_units": sum(e["units"] for e in sources if e["action"]),
        "selected_estimated_chars": sum(e["estimated_chars"] for e in sources if e["action"]),
        "already_pending": sum(e["reason"] == "already_pending" for e in sources),
        "current_unit_extraction_stages": dict(Counter(u["extraction_stage"] for task_id in current_tasks for u in inventory.units[task_id])),
        "derived_layer_coverage": _rows(conn,
            "SELECT content_type,status,count(*) items,"
            "sum(EXISTS(SELECT 1 FROM context_layers l WHERE l.item_id=c.id AND l.layer='l0')) with_l0,"
            "sum(EXISTS(SELECT 1 FROM context_layers l WHERE l.item_id=c.id AND l.layer='l1')) with_l1,"
            "sum(EXISTS(SELECT 1 FROM context_layers l WHERE l.item_id=c.id AND l.layer='l2')) with_l2 "
            "FROM context_items c GROUP BY content_type,status ORDER BY content_type,status"),
        "raw_integrity": "metadata_only_not_decrypted",
    }
    result = {"version": VERSION, "kind": "current_source_governance", "ownership_policy_version": ownership_reasoning.VERSION, "rule_revision": rule_revision, "policy_sha256": rule_revision,
              "sources": sources, "actions": actions, "cleaning_repairs": repairs, "summary": summary,
              "upgrade_candidates": [{"source_key": e["source_key"], "reason": e["reason"],
                                      "current_revision": e["current_revision"], "task_id": e["current_task_id"]}
                                     for e in sources if e["reason"] in {"rule_revision_changed", "historical_tasks_require_upgrade"}]}
    result["plan_sha256"] = _plan_hash(result)
    return result


def _audit_rows(conn, plan):
    if not _has(conn, AUDIT):
        return {}
    rows = _rows(conn, f"SELECT * FROM {AUDIT} WHERE plan_sha256=?", (plan["plan_sha256"],))
    return {(r["operation"], r["source_key"]): r for r in rows}


def _ensure_audit(conn):
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {AUDIT}(
        plan_sha256 TEXT NOT NULL, operation TEXT NOT NULL, source_key TEXT NOT NULL,
        state TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(plan_sha256,operation,source_key))""")


def _record_audit(conn, plan, operation, key, detail):
    conn.execute(f"INSERT INTO {AUDIT} VALUES(?,?,?,?,?,?,?)",
                 (plan["plan_sha256"], operation, key, "applied", _json(detail), _now_iso(), _now_iso()))


def _task_state(conn, task_id):
    task = _rows(conn, "SELECT * FROM organization_tasks WHERE id=?", (task_id,))
    units = _rows(conn, "SELECT * FROM organization_units WHERE task_id=? ORDER BY ordinal", (task_id,))
    return task[0] if task else None, units


def apply_plan(service, plan, *, limit=5):
    """Restore at most limit entries atomically; repeating resumes unapplied entries."""
    _validate(plan); _limit(limit)
    conn = service.store._connection()
    if service.knowledge().rules.read()["revision"] != plan["rule_revision"]:
        raise ValueError("migration_revision_conflict")
    with service.store.transaction():
        audit = _audit_rows(conn, plan)
        selected = [e for e in plan["actions"] if ("queue", e["source_key"]) not in audit][:limit]
        inventory = Inventory(conn, plan["rule_revision"], keys={e["source_key"] for e in selected})
        for entry in selected:
            live = inventory.entry(entry["source_key"])
            if (inventory.stamp(entry["source_key"]) != entry["expected"]
                    or live["action"] != entry["action"] or live["current_revision"] != entry["source_revision"]):
                raise ValueError("migration_revision_conflict")
        from evolvmem import auto_organization as org
        # Validate the frozen coordinates against deterministic current cleaning
        # before touching any queue row. Runtime policy can change this without
        # changing stored conversation_history.
        for entry in selected:
            try:
                snapshot = org.source_snapshot(service, entry["source_key"])[1]
            except (ValueError, KeyError, OSError, TypeError) as error:
                raise ValueError("migration_snapshot_conflict") from error
            if entry["task_id"]:
                task, units = _task_state(conn, entry["task_id"])
                if units and snapshot != task["source_snapshot"]:
                    raise ValueError("migration_snapshot_conflict")
        if selected:
            _ensure_audit(conn)
        for entry in selected:
            before, units_before = _task_state(conn, entry["task_id"]) if entry["task_id"] else (None, [])
            if entry["action"] == "retry":
                org.retry(service, entry["task_id"])
                task_id = entry["task_id"]
            else:
                result = org.enqueue(service, [entry["source_key"]])
                if result["created"] != 1 or result["items"][0]["status"] != "pending":
                    raise ValueError("migration_enqueue_conflict")
                task_id = result["items"][0]["id"]
            after, units_after = _task_state(conn, task_id)
            detail = {"task_id": task_id, "before_queue": {k: before[k] for k in QUEUE_FIELDS} if before else None,
                      "after_hash": _hash([after, units_after]),
                      "protection_hash": _protection_hash(conn, entry["source_key"]),
                      "unit_queue_before": [{"ordinal": u["ordinal"], "extraction_stage": u["extraction_stage"],
                                             "updated_at": u["updated_at"]} for u in units_before if u["extraction_stage"] == "failed"]}
            _record_audit(conn, plan, "queue", entry["source_key"], detail)
    return {"applied": len(selected), "remaining": len(plan["actions"]) - sum(k[0] == "queue" for k in audit) - len(selected),
            "plan_sha256": plan["plan_sha256"]}


def _protection_hash(conn, key):
    keys = [key]
    if key.startswith("conversation:"):
        manifest = conn.execute("SELECT members FROM organization_conversations WHERE source_key=?", (key,)).fetchone()
        if manifest:
            keys.extend(json.loads(manifest[0]))
    protected = []
    for source in keys:
        protected.extend(_rows(conn, "SELECT * FROM organization_scope_exclusions WHERE source_key=? ORDER BY digest,scope_key", (source,)))
    return _hash(protected)


def rollback_plan(service, plan):
    """Only undo our exact unclaimed queue state. Changed/processed outputs remain."""
    _validate(plan)
    conn = service.store._connection()
    rolled, retained = 0, 0
    with service.store.transaction():
        reader = ProvenanceRead(service.store)
        policy_current = service.knowledge().rules.read()["revision"] == plan["rule_revision"]
        entries = {e["source_key"]: e for e in plan["actions"]}
        for (operation, key), row in _audit_rows(conn, plan).items():
            if operation != "queue" or row["state"] != "applied":
                continue
            detail = json.loads(row["detail"])
            task, units = _task_state(conn, detail["task_id"])
            if (not task or task["status"] != "pending" or _hash([task, units]) != detail["after_hash"]
                    or _protection_hash(conn, key) != detail["protection_hash"]
                    or not policy_current or reader.source_revision(key) != (entries[key]["source_revision"], "")):
                retained += 1
                continue
            if detail["before_queue"] is None:
                if units:
                    retained += 1
                    continue
                conn.execute("DELETE FROM organization_tasks WHERE id=?", (task["id"],))
            else:
                before = detail["before_queue"]
                conn.execute("UPDATE organization_tasks SET " + ",".join(k + "=?" for k in QUEUE_FIELDS) + " WHERE id=?",
                             [*[before[k] for k in QUEUE_FIELDS], task["id"]])
                for u in detail["unit_queue_before"]:
                    conn.execute("UPDATE organization_units SET extraction_stage=?,updated_at=? WHERE task_id=? AND ordinal=?",
                                 (u["extraction_stage"], u["updated_at"], task["id"], u["ordinal"]))
            conn.execute(f"UPDATE {AUDIT} SET state='rolled_back',updated_at=? WHERE plan_sha256=? AND operation=? AND source_key=?",
                         (_now_iso(), plan["plan_sha256"], operation, key))
            rolled += 1
    return {"rolled_back": rolled, "retained": retained}


def repair_cleaning(service, plan, *, limit=5, max_archive_bytes=32 * 1024 * 1024,
                    max_batch_bytes=64 * 1024 * 1024):
    """Restore missing derived body rows from explicitly selected original payloads.

    Existing cleaning reviews, body rows, raw encrypted files and tasks remain.
    Unknown/unreadable is an error; an actually parsed empty dialogue is recorded
    with its true empty body, never synthesized from a missing payload.
    """
    _validate(plan); _limit(limit)
    if (type(max_archive_bytes) is not int or not 0 < max_archive_bytes <= 32 * 1024 * 1024
            or type(max_batch_bytes) is not int or not 0 < max_batch_bytes <= 64 * 1024 * 1024):
        raise ValueError("invalid_cleaning_budget")
    from evolvmem.history_memory import save_clean
    from evolvmem.session_archive import SessionArchiver
    conn = service.store._connection()
    archiver = SessionArchiver(service.config, service.store)
    if not archiver._key_path.is_file():
        raise ValueError("archive_key_missing")
    from evolvmem.archive_storage import ArchiveStorage
    # Read the existing key directly: SessionArchiver._storage() can create a
    # replacement key if it disappears between the existence check and read.
    storage = ArchiveStorage(service.config, archiver._key_path.read_bytes())
    policy = service.knowledge().rules.read()
    if policy["revision"] != plan["rule_revision"]:
        raise ValueError("migration_revision_conflict")
    repaired = 0
    unavailable, oversized, unknown, deferred = [], [], [], []
    bytes_selected = 0
    with service.store.transaction():
        audit = _audit_rows(conn, plan)
        pending = [e for e in plan["cleaning_repairs"] if ("cleaning", e["source_key"]) not in audit]
        inventory = Inventory(conn, plan["rule_revision"], keys={e["source_key"] for e in pending})
        selected = []
        for entry in pending:
            identity = int(entry["source_key"].split(":")[1])
            metadata = inventory.storage.get(identity) or {}
            size = metadata.get("logical_bytes")
            if type(size) is not int or size < 0:
                unknown.append(entry["source_key"])
            elif size > max_archive_bytes:
                oversized.append({"source_key": entry["source_key"], "logical_bytes": size})
            elif len(selected) < limit:
                if bytes_selected + size > max_batch_bytes:
                    deferred.append(entry["source_key"])
                else:
                    selected.append(entry)
                    bytes_selected += size
        for entry in selected:
            live = inventory.entry(entry["source_key"])
            if (inventory.stamp(entry["source_key"]) != entry["expected"]
                    or not live["current_archive"] or live["raw"]["state"] != "available"
                    or live["reason"] not in {"source_body_unverifiable", "already_assigned_archive"}
                    or live["cleaning"]["body_state"] != "missing"):
                raise ValueError("migration_revision_conflict")
        for entry in selected:
            identity = int(entry["source_key"].split(":")[1])
            archive = inventory.archives[identity]
            target = (service.config.data_dir / archive["payload_path"]).resolve()
            if not target.is_relative_to(service.config.data_dir.resolve()):
                raise ValueError("archive_path_outside_data_dir")
            try:
                # A tiny logical size claim cannot permit an unbounded physical
                # allocation either. The separate streaming path handles it.
                if target.stat().st_size > max_archive_bytes:
                    oversized.append({"source_key": entry["source_key"],
                                      "logical_bytes": inventory.storage[identity]["logical_bytes"]})
                    continue
                blob = target.read_bytes()
                physical = inventory.storage.get(identity)
                expected = physical["manifest_sha256"] if physical else archive["payload_sha256"]
                if hashlib.sha256(blob).hexdigest() != expected:
                    raise ValueError("archive_manifest_changed")
                manifest = storage.manifest(blob)
                if physical and (manifest is None or manifest["sha256"] != physical["content_sha256"]
                                 or manifest["size"] != physical["logical_bytes"]):
                    raise ValueError("archive_metadata_mismatch")
                payload = storage.unpack(blob)
            except Exception:
                unavailable.append(entry["source_key"])
                continue
            if not save_clean(service.store, identity, payload, policy=policy):
                unavailable.append(entry["source_key"])
                continue
            _ensure_audit(conn)
            _record_audit(conn, plan, "cleaning", entry["source_key"], {"archive_id": identity, "result": "restored_missing_body"})
            repaired += 1
    return {"repaired": repaired, "unavailable": unavailable,
            "oversized_pending": oversized, "unknown_size_pending": unknown,
            "budget_deferred": deferred, "bytes_selected": bytes_selected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "apply", "rollback", "repair-cleaning"))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True, help="Rules and existing encrypted archive/key directory")
    parser.add_argument("--policy-data-dir", type=Path, help="Optional read-only rules directory, separate from raw archives")
    parser.add_argument("--plan", type=Path, help="Exact JSON plan for writes")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--max-archive-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--max-batch-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args()
    from evolvmem.knowledge_rules import KnowledgeRules
    read_only = args.operation == "prepare"
    conn = sqlite3.connect(args.db.resolve().as_uri() + ("?mode=ro" if read_only else "?mode=rw"), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if read_only:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            result = build_plan(conn, rule_revision=KnowledgeRules(args.policy_data_dir or args.data_dir).read()["revision"])
            conn.rollback()
        else:
            conn.execute("PRAGMA foreign_keys=ON")
            if not args.plan:
                parser.error("--plan is required for writes")
            from evolvmem.config import Config
            from evolvmem.context_store import ContextStore
            from evolvmem.knowledge import KnowledgeBase
            # Attach the explicitly opened existing database; no initialize(),
            # schema bootstrap, worker, vector engine or provider configuration.
            config = Config(data_dir=args.data_dir)
            store = ContextStore(config)
            store._conn = conn
            service = SimpleNamespace(store=store, config=config)
            knowledge = KnowledgeBase(service)
            knowledge.rules = KnowledgeRules(args.policy_data_dir or args.data_dir)
            service.knowledge = lambda: knowledge
            from evolvmem.memory_learning import MemoryLearning
            learning = MemoryLearning(service)
            service.learning = lambda: learning
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            if args.operation == "apply":
                result = apply_plan(service, plan, limit=args.limit)
            elif args.operation == "rollback":
                result = rollback_plan(service, plan)
            else:
                result = repair_cleaning(service, plan, limit=args.limit,
                    max_archive_bytes=args.max_archive_bytes, max_batch_bytes=args.max_batch_bytes)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            args.output.write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
