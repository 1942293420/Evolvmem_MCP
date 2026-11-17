"""Temporal validity primitives for context items (decisions and events).

The contract is deliberately narrow and content-free:

* every timestamp is normalized to a naive UTC ``YYYY-MM-DD HH:MM:SS`` string
  so lexical order matches chronological order and old rows stay comparable;
* an absent timestamp stays ``UNKNOWN`` (``None``) — it is never derived from
  ``created_at`` and never guessed from surrounding text;
* ``effective_until`` is exclusive: an item is valid for
  ``effective_from <= moment < effective_until``;
* the *temporal rank* of a record is its ``effective_from`` when known, else
  its ``occurred_at``; a record with neither has no rank and can never
  outrank a record that has one.

This module has no database or model dependencies so both the store and the
retrieval layer can share exactly one normalization rule.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# Callers may say "unknown" explicitly; it means the same as leaving the field
# empty. No other magic values are accepted.
_UNKNOWN_TOKENS = frozenset({"unknown"})


class TemporalValidationError(ValueError):
    """Raised when a caller-provided timestamp is not a valid ISO 8601 value."""


def normalize_timestamp(value: object, field: str) -> str | None:
    """Normalize one optional timestamp to naive UTC, or ``None`` for UNKNOWN.

    Accepts ``None``/``""``/``"unknown"`` as UNKNOWN, a calendar date, or an
    ISO 8601 date-time with or without an offset (``Z`` included). A naive
    date-time is interpreted as UTC — the caller's clock convention is already
    UTC everywhere in this codebase.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.casefold() in _UNKNOWN_TOKENS:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed_day = date.fromisoformat(text)
            except ValueError:
                raise TemporalValidationError(f"invalid_{field}") from None
            parsed = datetime(
                parsed_day.year, parsed_day.month, parsed_day.day
            )
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        raise TemporalValidationError(f"invalid_{field}")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime(TIMESTAMP_FORMAT)


def temporal_rank(
    effective_from: str | None, occurred_at: str | None
) -> str | None:
    """The ordering rank of one record, or ``None`` when UNKNOWN."""
    return effective_from or occurred_at or None


def window_contains(
    effective_from: str | None,
    effective_until: str | None,
    moment: str,
) -> bool:
    """True when ``moment`` falls inside the known window (until exclusive)."""
    if effective_from is not None and moment < effective_from:
        return False
    if effective_until is not None and moment >= effective_until:
        return False
    return True


def successor_takes_precedence(implicit_end: str | None, moment: str) -> bool:
    """True when a superseded record's successor is already effective.

    A same-identity successor supplies an implicit end for its predecessor. When
    that boundary is still in the future the successor is only *scheduled*: the
    predecessor remains the currently effective record until the boundary. When
    the boundary has arrived (or the successor's rank is UNKNOWN, which proves
    nothing) the successor takes precedence and the predecessor is not current.
    """
    if implicit_end is None:
        return False
    return implicit_end <= moment


def applicability_preference(
    *, active: bool, effective_from: str | None, occurred_at: str | None, item_id: int
) -> tuple[int, str, int]:
    """Ordering key for the applicable winner of one identity family.

    Supersession is resolved over the actual family (same identity key, project
    and scope), so the winner never depends on which rows happened to match a
    query: an active record beats a superseded predecessor, known effective
    ranks decide between same-status records, and UNKNOWN rank sorts lowest.
    The id is only a deterministic tie-break.
    """
    return (
        1 if active else 0,
        temporal_rank(effective_from, occurred_at) or "",
        int(item_id),
    )


def temporal_state(
    effective_from: str | None,
    effective_until: str | None,
    occurred_at: str | None,
    mentioned_at: str | None,
) -> str:
    """``known`` when any temporal field is set, else ``unknown``."""
    if any(
        value is not None
        for value in (effective_from, effective_until, occurred_at, mentioned_at)
    ):
        return "known"
    return "unknown"


def validate_window(
    effective_from: str | None, effective_until: str | None, *, field: str = "effective"
) -> None:
    """Reject an inverted or empty known window (``from >= until``)."""
    if (
        effective_from is not None
        and effective_until is not None
        and effective_from >= effective_until
    ):
        raise TemporalValidationError(f"invalid_{field}_window")
