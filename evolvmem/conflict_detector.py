"""Conflict detection — compares new memories against existing active ones before writing."""

import re
from dataclasses import dataclass
from evolvmem.memory_store import MemoryStore


@dataclass
class ConflictDecision:
    """Conflict detection result."""
    action: str   # "add" | "skip" | "replace" | "conflict"
    reason: str
    existing_id: int | None = None


class ConflictDetector:
    """Conflict detector for candidate memories before writing.

    Decision tree:
    1. key doesn't exist → add
    2. value is identical → skip (duplicate)
    3. user explicitly says abandon old approach → replace
    4. new info is more specific (from current session and significantly more detailed) → replace
    5. cannot determine → conflict (don't write, keep old value)
    """

    def __init__(self, memory_store: MemoryStore):
        self.store = memory_store

    def check(self, candidate_key: str, candidate_value: str,
              user_override: bool = False) -> ConflictDecision:
        """Check the candidate memory against existing memories for conflicts."""
        existing = self._get_active(candidate_key)

        # 1. key doesn't exist → add directly
        if existing is None:
            return ConflictDecision(
                action="add",
                reason=f"New key '{candidate_key}', adding directly",
            )

        # 2. value is identical → skip
        if existing["value"].strip() == candidate_value.strip():
            return ConflictDecision(
                action="skip",
                reason=f"Value for key '{candidate_key}' unchanged",
                existing_id=existing["id"],
            )

        # 3. user explicitly overrides → replace
        if user_override:
            return ConflictDecision(
                action="replace",
                reason="User explicitly indicated to abandon old approach",
                existing_id=existing["id"],
            )

        # 4. new info is significantly more specific → replace
        if self._is_significantly_more_specific(
            existing["value"], candidate_value
        ):
            return ConflictDecision(
                action="replace",
                reason="New info is significantly more specific, replacing old value",
                existing_id=existing["id"],
            )

        # 5. cannot determine → conflict
        return ConflictDecision(
            action="conflict",
            reason=f"Key '{candidate_key}' already exists with a different value, "
                   f"cannot automatically determine which is more trustworthy",
            existing_id=existing["id"],
        )

    def _get_active(self, key: str) -> dict | None:
        results = self.store.get_by_key(key)
        for r in results:
            if r["status"] == "active":
                return r
        return None

    def _is_significantly_more_specific(self, old_value: str,
                                        new_value: str) -> bool:
        """Determine if new value is significantly more specific than old value."""
        if len(new_value) >= len(old_value) * 1.5:
            return True
        # New value contains specific data (numbers, dates, names, etc.)
        specificity_markers = [
            r'\d+',             # numbers
            r'\d{4}-\d{2}',     # dates
            r'http|https|\.com', # links
            r'具体|明确|确认',    # Chinese certainty markers
        ]
        old_specificity = sum(
            1 for p in specificity_markers
            if re.search(p, old_value)
        )
        new_specificity = sum(
            1 for p in specificity_markers
            if re.search(p, new_value)
        )
        return new_specificity > old_specificity
