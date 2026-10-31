"""Development-capability memories with source provenance and lexical search."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json

from evolvmem.capability_models import CAPABILITY_CATEGORIES, CAPABILITY_CATEGORY_LABELS
from evolvmem.context_store import _now_iso


_STATUSES = {"active", "pending", "ignored", "archived"}


@dataclass(frozen=True, slots=True)
class CapabilityMemoryDraft:
    category: str
    title: str
    content: str
    keywords: tuple[str, ...] = ()
    source_project: str = ""
    source_ref: str = ""
    occurred_at: str | None = None

    def fingerprint(self) -> str:
        raw = json.dumps(
            [self.category, self.title.strip(), self.content.strip()],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilitySourceDraft:
    """A caller-supplied provenance claim, not independent success evidence.

    ``confirmed`` describes the caller's ownership decision only. Source versions
    are opaque strings; callers remain responsible for source freshness checks.
    """

    source_kind: str
    source_key: str
    source_ref: str
    source_revision: str = ""
    context_item_id: int | None = None
    task_id: int | None = None
    unit_digest: str = ""
    unit_revision: int | None = None
    projects: tuple[str, ...] = ()
    knowledge_scope: str = "project"
    evidence_status: str = "unverified"

    def validate(self) -> None:
        for name in ("source_kind", "source_key", "source_ref", "source_revision",
                     "unit_digest", "knowledge_scope", "evidence_status"):
            if not isinstance(getattr(self, name), str):
                raise ValueError("invalid_capability_source_" + name)
        if self.source_kind not in {"context", "unit", "reference", "manual"}:
            raise ValueError("invalid_capability_source_kind")
        if not self.source_key.strip():
            raise ValueError("invalid_capability_source_key")
        if self.knowledge_scope not in {"project", "global"}:
            raise ValueError("invalid_capability_source_scope")
        if self.evidence_status not in {"unverified", "confirmed"}:
            raise ValueError("invalid_capability_source_evidence_status")
        if not isinstance(self.projects, tuple) or any(
            not isinstance(project, str) or not project.strip() for project in self.projects
        ):
            raise ValueError("invalid_capability_source_projects")
        if self.knowledge_scope == "global" and self.projects:
            raise ValueError("global_project_conflict")
        for name in ("context_item_id", "task_id", "unit_revision"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("invalid_capability_source_" + name)
        if self.source_kind == "context" and self.context_item_id is None:
            raise ValueError("capability_source_context_item_required")
        if self.source_kind == "unit" and (self.task_id is None or not self.unit_digest.strip()):
            raise ValueError("capability_source_unit_identity_required")


class CapabilityStore:
    def __init__(self, context_store):
        self.context_store = context_store

    @property
    def conn(self):
        return self.context_store._connection()

    def _ensure_categories(self) -> None:
        now = _now_iso()
        self.conn.executemany(
            "INSERT OR IGNORE INTO capability_categories"
            "(id,label,created_at,updated_at) VALUES(?,?,?,?)",
            [(key, CAPABILITY_CATEGORY_LABELS[key], now, now) for key in CAPABILITY_CATEGORIES],
        )

    def categories(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM capability_categories ORDER BY rowid"
        )]

    @contextmanager
    def _source_write(self):
        # ContextStore joins nested transactions. A local savepoint also makes
        # this operation atomic if a batch caller catches its write error.
        with self.context_store.transaction():
            self.conn.execute("SAVEPOINT capability_source_write")
            try:
                yield
            except Exception:
                self.conn.execute("ROLLBACK TO capability_source_write")
                self.conn.execute("RELEASE capability_source_write")
                raise
            else:
                self.conn.execute("RELEASE capability_source_write")

    def upsert(
        self, draft: CapabilityMemoryDraft, *, source: CapabilitySourceDraft | None = None
    ) -> int:
        if not isinstance(draft, CapabilityMemoryDraft):
            raise TypeError("capability_memory_draft_required")
        if not draft.title.strip() or not draft.content.strip():
            raise ValueError("invalid_capability_memory")
        if any(not isinstance(word, str) for word in draft.keywords):
            raise ValueError("invalid_capability_keywords")
        if source is not None:
            self._validate_source(source)
        with self._source_write():
            self._ensure_categories()
            category = self.conn.execute(
                "SELECT enabled FROM capability_categories WHERE id=?",
                (draft.category,),
            ).fetchone()
            if category is None or not category["enabled"]:
                raise ValueError("capability_category_not_found")
            now = _now_iso()
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO capability_memories(category,title,content,keywords,"
                "source_project,source_ref,occurred_at,fingerprint,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    draft.category,
                    draft.title.strip(),
                    draft.content.strip(),
                    json.dumps(draft.keywords, ensure_ascii=False),
                    draft.source_project,
                    draft.source_ref,
                    draft.occurred_at,
                    draft.fingerprint(),
                    now,
                    now,
                ),
            )
            row = self.conn.execute(
                "SELECT memory_id FROM capability_memories WHERE fingerprint=?",
                (draft.fingerprint(),),
            ).fetchone()
            memory_id = int(row[0])
            if cursor.rowcount == 1:
                self._insert_fts(memory_id)
            if source is not None:
                self._bind_source(memory_id, source)
        return memory_id

    @staticmethod
    def _validate_source(source: CapabilitySourceDraft) -> None:
        if not isinstance(source, CapabilitySourceDraft):
            raise TypeError("capability_source_draft_required")
        source.validate()

    def bind_source(self, memory_id: int, source: CapabilitySourceDraft) -> int:
        """Attach or update a source claim without promoting the memory's status."""
        self._validate_source(source)
        with self._source_write():
            if self.conn.execute(
                "SELECT 1 FROM capability_memories WHERE memory_id=?", (memory_id,)
            ).fetchone() is None:
                raise ValueError("capability_memory_not_found")
            return self._bind_source(memory_id, source)

    def _bind_source(self, memory_id: int, source: CapabilitySourceDraft) -> int:
        # Called only with validated input inside the caller's write transaction.
        fields = ("source_ref", "source_revision", "context_item_id", "task_id",
                  "unit_digest", "unit_revision", "projects", "knowledge_scope", "evidence_status")
        values = (source.source_ref, source.source_revision, source.context_item_id, source.task_id,
                  source.unit_digest, source.unit_revision,
                  json.dumps(source.projects, ensure_ascii=False),
                  source.knowledge_scope, source.evidence_status)
        existing = self.conn.execute(
            "SELECT * FROM capability_sources WHERE memory_id=? AND source_kind=? AND source_key=?",
            (memory_id, source.source_kind, source.source_key),
        ).fetchone()
        now = _now_iso()
        if existing is None:
            cursor = self.conn.execute(
                "INSERT INTO capability_sources(memory_id,source_kind,source_key,"
                + ",".join(fields) + ",created_at,updated_at) VALUES("
                + ",".join("?" for _ in range(len(fields) + 5)) + ")",
                (memory_id, source.source_kind, source.source_key, *values, now, now),
            )
            source_id = int(cursor.lastrowid)
        else:
            source_id = int(existing["source_id"])
            if tuple(existing[field] for field in fields) != values:
                self.conn.execute(
                    "UPDATE capability_sources SET " + ",".join(field + "=?" for field in fields)
                    + ",updated_at=?,revision=revision+1 WHERE source_id=?",
                    (*values, now, source_id),
                )
        projects = tuple(dict.fromkeys(source.projects))
        if source.evidence_status == "confirmed" and len(projects) == 1:
            self.conn.execute(
                "UPDATE capability_memories SET source_project=?,updated_at=?,revision=revision+1 "
                "WHERE memory_id=? AND source_project=''",
                (projects[0], now, memory_id),
            )
        return source_id

    def sources(self, memory_id: int) -> list[dict]:
        """Return persisted claims; freshness and eligibility are checked elsewhere."""
        rows = self.conn.execute(
            "SELECT * FROM capability_sources WHERE memory_id=? ORDER BY source_id", (memory_id,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["projects"] = json.loads(item["projects"])
            result.append(item)
        return result

    def _insert_fts(self, memory_id: int) -> None:
        row = self.conn.execute(
            "SELECT title,content,keywords FROM capability_memories WHERE memory_id=?",
            (memory_id,),
        ).fetchone()
        self.conn.execute(
            "INSERT INTO capability_memories_fts(rowid,title,content,keywords) VALUES(?,?,?,?)",
            (memory_id, row["title"], row["content"], row["keywords"]),
        )

    @staticmethod
    def _public(row) -> dict:
        result = dict(row)
        result.pop("score", None)
        result["keywords"] = json.loads(result["keywords"] or "[]")
        return result

    def get(self, memory_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM capability_memories WHERE memory_id=?", (memory_id,)
        ).fetchone()
        if row is None:
            return None
        result = self._public(row)
        sources = self.sources(memory_id)
        result["source_projects"] = sorted({
            project for source in sources for project in source["projects"]
        })
        result["sources_count"] = len(sources)
        return result

    @staticmethod
    def _match_query(query: str) -> str:
        terms = [term.strip() for term in query.split() if term.strip()]
        return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)

    @staticmethod
    def _query_terms(query: str) -> tuple[str, ...]:
        import re
        terms = re.findall(r"[A-Za-z][A-Za-z0-9_+.-]*", query)
        for sequence in re.findall(r"[\u4e00-\u9fff]+", query):
            if len(sequence) <= 4:
                terms.append(sequence)
            else:
                terms.extend(sequence[index:index + 2] for index in range(len(sequence) - 1))
        return tuple(dict.fromkeys(term.casefold() for term in terms if len(term) >= 2))

    def search(self, query: str, categories=(), top_k: int = 10, *, offset: int = 0) -> list[dict]:
        query = str(query).strip()
        if not query:
            raise ValueError("invalid_query")
        if type(offset) is not int or offset < 0:
            raise ValueError("invalid_offset")
        top_k = max(1, min(int(top_k), 100))
        categories = tuple(categories)
        category_sql = ""
        params: list[object] = []
        if categories:
            category_sql = " AND m.category IN (" + ",".join("?" * len(categories)) + ")"
            params.extend(categories)
        match = self._match_query(query)
        if match:
            rows = self.conn.execute(
                "SELECT m.*,-rank AS score FROM capability_memories_fts f "
                "JOIN capability_memories m ON m.memory_id=f.rowid "
                "WHERE capability_memories_fts MATCH ? AND m.status='active'"
                + category_sql + " ORDER BY rank,m.memory_id LIMIT ? OFFSET ?",
                (match, *params, top_k, offset),
            ).fetchall()
            if rows:
                return [self._public(row) for row in rows]
            # Distinguish exhausted FTS from a query that needs CJK fallback.
            if offset and self.conn.execute(
                "SELECT 1 FROM capability_memories_fts f JOIN capability_memories m ON m.memory_id=f.rowid "
                "WHERE capability_memories_fts MATCH ? AND m.status='active'" + category_sql + " LIMIT 1",
                (match, *params)).fetchone():
                return []
        terms = self._query_terms(query)[:32]
        if not terms:
            return []
        # Rank in SQLite and materialize one bounded page, not every body in
        # Python. The fallback remains deterministic across pages.
        score = " + ".join("(instr(lower(m.title || char(10) || m.content || char(10) || m.keywords),?)>0)" for _ in terms)
        rows = self.conn.execute(
            "SELECT * FROM (SELECT m.*,(" + score + ") AS score FROM capability_memories m "
            "WHERE m.status='active'" + category_sql + ") WHERE score>0 ORDER BY score DESC,memory_id LIMIT ? OFFSET ?",
            (*terms, *params, top_k, offset)).fetchall()
        return [self._public(row) for row in rows]

    def set_status(self, memory_id: int, status: str, expected_revision=None) -> bool:
        if status not in _STATUSES:
            raise ValueError("invalid_capability_status")
        with self.context_store.transaction():
            if expected_revision is None:
                cursor = self.conn.execute(
                    "UPDATE capability_memories SET status=?,revision=revision+1,updated_at=? "
                    "WHERE memory_id=?",
                    (status, _now_iso(), memory_id),
                )
            else:
                cursor = self.conn.execute(
                    "UPDATE capability_memories SET status=?,revision=revision+1,updated_at=? "
                    "WHERE memory_id=? AND revision=?",
                    (status, _now_iso(), memory_id, expected_revision),
                )
        return cursor.rowcount == 1

    def suggest_category(self, label: str, reason: str, source_memory_id=None) -> int:
        label = label.strip()
        reason = reason.strip()
        if not label or not reason:
            raise ValueError("invalid_category_suggestion")
        if source_memory_id is not None and self.get(int(source_memory_id)) is None:
            raise ValueError("capability_memory_not_found")
        with self.context_store.transaction():
            cursor = self.conn.execute(
                "INSERT INTO capability_category_suggestions(label,reason,source_memory_id,created_at) "
                "VALUES(?,?,?,?)",
                (label, reason, source_memory_id, _now_iso()),
            )
        return int(cursor.lastrowid)
