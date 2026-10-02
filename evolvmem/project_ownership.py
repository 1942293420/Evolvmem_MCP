"""Project-ownership trust classification for default recall surfaces.

An item's ``project`` column is only a claim. The trustworthy record is the
existing resolution/review row in ``context_project_resolutions``:

* ``confirmed``  — a resolved decision, or a human-accepted correction. Typed
  service writes that carry an explicit project (continuity checkpoints,
  rollups, experiences, playbooks) record this provenance at write time;
* ``excluded``   — pending review, conflict, unresolved, rejected review, or
  no resolution row at all: uncertain ownership is held out of default
  injection, with ``project_ownership_unverified`` naming the missing-row case;
* ``unverified`` — a ``global``/``ignored`` decision row: there is no project
  ownership claim to distrust, so the item stays injectable but must never be
  presented as confirmed.

Nothing here infers a project from free text, rewrites business data, or
touches the registry; it only reads the decision the resolver/reviewer already
recorded. Historical no-resolution rows are therefore *held* (not injected by
default) until a human confirms them through the review queue, which lists them
via ``GET /api/resolutions?state=unreviewed``.

All returned structure is bounded and content-free: stable item ids plus
stable reason codes, never memory text, evidence payloads, or paths.
"""

from __future__ import annotations

from dataclasses import dataclass

CONFIRMED = "confirmed"
UNVERIFIED = "unverified"
EXCLUDED = "excluded"

REASON_NONE = ""
REASON_PENDING = "project_ownership_pending"
REASON_CONFLICT = "project_ownership_conflict"
REASON_REJECTED = "project_ownership_rejected"
REASON_UNVERIFIED = "project_ownership_unverified"

# The only reasons a caller may see for a withheld item.
EXCLUSION_REASONS = frozenset(
    {REASON_PENDING, REASON_CONFLICT, REASON_REJECTED, REASON_UNVERIFIED}
)

MAX_DIAGNOSTIC_ITEMS = 20


@dataclass(frozen=True, slots=True)
class OwnershipFact:
    """One item's ownership trust, derived only from the resolution row."""

    state: str
    reason: str = REASON_NONE
    resolution_state: str = "none"
    review_state: str = "none"
    decision_source: str = "none"
    method: str = ""

    @property
    def confirmed(self) -> bool:
        return self.state == CONFIRMED

    @property
    def excluded(self) -> bool:
        return self.state == EXCLUDED

    def public(self) -> dict[str, str]:
        """Bounded public projection: states and codes, never evidence text."""
        return {
            "state": self.state,
            "reason": self.reason,
            "resolution_state": self.resolution_state,
            "review_state": self.review_state,
            "decision_source": self.decision_source,
            "method": self.method,
        }


UNVERIFIED_FACT = OwnershipFact(state=UNVERIFIED)

# No resolution row: ownership is uncertain, so the record is held out of the
# default injection surface until a human confirms it through the review queue.
UNREVIEWED_FACT = OwnershipFact(
    state=EXCLUDED, reason=REASON_UNVERIFIED
)


def classify(
    resolution_state: str | None,
    review_state: str | None,
    decision_source: str | None,
    method: str | None,
) -> OwnershipFact:
    """Map one resolution row (or its absence) to an ownership fact."""
    if resolution_state is None and review_state is None:
        return UNREVIEWED_FACT
    state = resolution_state or "none"
    review = review_state or "none"
    source = decision_source or "none"
    if review == "rejected":
        return OwnershipFact(
            EXCLUDED, REASON_REJECTED, state, review, source, method or ""
        )
    if review == "pending":
        reason = REASON_CONFLICT if state == "conflict" else REASON_PENDING
        return OwnershipFact(EXCLUDED, reason, state, review, source, method or "")
    if state == "conflict":
        return OwnershipFact(
            EXCLUDED, REASON_CONFLICT, state, review, source, method or ""
        )
    if state == "unresolved":
        return OwnershipFact(
            EXCLUDED, REASON_PENDING, state, review, source, method or ""
        )
    if state == "resolved":
        return OwnershipFact(CONFIRMED, REASON_NONE, state, review, source, method or "")
    # global/ignored: no project ownership claim — usable but never confirmed.
    return OwnershipFact(UNVERIFIED, REASON_NONE, state, review, source, method or "")


def load_ownership(store, item_ids) -> dict[int, OwnershipFact]:
    """Load ownership facts for many items in one read.

    An unavailable store or a database without the resolution table means
    "no facts known": the caller treats that as UNREVIEWED (held), the safe
    direction for a default injection surface — never as confirmed.
    """
    unique = [int(item_id) for item_id in dict.fromkeys(item_ids)]
    if not unique:
        return {}
    marks = ",".join("?" for _ in unique)
    try:
        rows = store._connection().execute(
            "SELECT item_id, resolution_state, review_state, decision_source,"
            " method FROM context_project_resolutions "
            f"WHERE item_id IN ({marks})",
            tuple(unique),
        ).fetchall()
    except Exception:
        return {}
    return {
        int(row["item_id"]): classify(
            row["resolution_state"],
            row["review_state"],
            row["decision_source"],
            row["method"],
        )
        for row in rows
    }


def partition(store, item_ids) -> tuple[list[int], list[tuple[int, str]]]:
    """Split ids into (kept, excluded) where excluded carries a stable reason."""
    unique = [int(item_id) for item_id in dict.fromkeys(item_ids)]
    facts = load_ownership(store, unique)
    kept: list[int] = []
    excluded: list[tuple[int, str]] = []
    for item_id in unique:
        fact = facts.get(item_id, UNREVIEWED_FACT)
        if fact.excluded:
            excluded.append((item_id, fact.reason))
        else:
            kept.append(item_id)
    return kept, excluded


def excluded_diagnostics(
    excluded: list[tuple[int, str]], *, limit: int = MAX_DIAGNOSTIC_ITEMS
) -> list[dict]:
    """Bounded ``[{"id", "reason"}]`` projection, deterministic order."""
    ordered = sorted(dict(excluded).items())
    return [{"id": item_id, "reason": reason} for item_id, reason in ordered[:limit]]


def unverified_ids(store, item_ids, *, limit: int = MAX_DIAGNOSTIC_ITEMS) -> list[int]:
    """Ids that are kept but carry no resolved/reviewed ownership record."""
    unique = [int(item_id) for item_id in dict.fromkeys(item_ids)]
    facts = load_ownership(store, unique)
    return [
        item_id
        for item_id in unique
        if facts.get(item_id, UNREVIEWED_FACT).state == UNVERIFIED
    ][:limit]
