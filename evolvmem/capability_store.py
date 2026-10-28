"""Development-capability memories with source provenance and lexical search."""
from __future__ import annotations

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

    def upsert(self, draft: CapabilityMemoryDraft) -> int:
        if not isinstance(draft, CapabilityMemoryDraft):
            raise TypeError("capability_memory_draft_required")
        if not draft.title.strip() or not draft.content.strip():
            raise ValueError("invalid_capability_memory")
        if any(not isinstance(word, str) for word in draft.keywords):
            raise ValueError("invalid_capability_keywords")
        with self.context_store.transaction():
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
        return memory_id

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
        return self._public(row) if row is not None else None

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

    def search(self, query: str, categories=(), top_k: int = 10) -> list[dict]:
        query = str(query).strip()
        if not query:
            raise ValueError("invalid_query")
        top_k = max(1, min(int(top_k), 100))
        categories = tuple(categories)
        category_sql = ""
        params: list[object] = []
        if categories:
            category_sql = " AND m.category IN (" + ",".join("?" * len(categories)) + ")"
            params.extend(categories)
        hits: dict[int, dict] = {}
        match = self._match_query(query)
        if match:
            rows = self.conn.execute(
                "SELECT m.*,-rank AS score FROM capability_memories_fts f "
                "JOIN capability_memories m ON m.memory_id=f.rowid "
                "WHERE capability_memories_fts MATCH ? AND m.status='active'"
                + category_sql + " ORDER BY rank LIMIT ?",
                (match, *params, top_k * 3),
            ).fetchall()
            for row in rows:
                hits[int(row["memory_id"])] = row
        if not hits:
            terms = self._query_terms(query)
            rows = self.conn.execute(
                "SELECT m.*,0.0 AS score FROM capability_memories m WHERE m.status='active'"
                + category_sql,
                params,
            ).fetchall()
            for row in rows:
                text = (row["title"] + "\n" + row["content"] + "\n" + row["keywords"]).casefold()
                matched = sum(term in text for term in terms)
                if matched:
                    row = dict(row)
                    row["score"] = matched / max(1, len(terms))
                    hits[int(row["memory_id"])] = row
        ordered = sorted(
            hits.values(), key=lambda row: (-float(row["score"]), int(row["memory_id"]))
        )
        return [self._public(row) for row in ordered[:top_k]]

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
