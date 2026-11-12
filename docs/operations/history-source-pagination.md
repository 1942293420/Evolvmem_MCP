# Historical source pagination (candidate, SQL013)

## Contract

A normal historical item read now calls
`em_content.history_item_inputs_first_page(library, project, ids)` once.  Each
returned row is `(id, payload, source_after, source_more)`:

* every non-source payload field is the SQL005 payload unchanged;
* `payload.sources` contains at most the first 64 sources ordered by source id;
* `source_after` is the last returned source id (NULL only for zero sources);
* `source_more` is true only when a 65th source exists in the same read
  snapshot.  It is a real `EXISTS` check, never `item.source_count`.

Only when `source_more` is true does Python call
`em_content.history_item_sources_page(library, project, item, after)`.  That
function returns `(source_id, payload, has_more)` for at most 64 strictly
greater source ids.  It reads a 65th row as a sentinel and does not return it.
The cursor advances to the last returned source id; every page rechecks
`require_history_scope` and `history_member`.

`evolvmem.pg_business_core.history_source_pages.assemble_sources` validates the
metadata cursor, page size, ordering, duplicate/cross-item/cross-library IDs,
the `has_more` marker, and the assembled count.  It returns a complete copy.
`HistoryEligibilityRead._load` retains the metadata page and each continuation
page against the existing 8 MiB budget, then publishes `self.rows[item]` only
after the complete payload validates.  The qualification/currentness rules remain unchanged. Summary references now
use the existing 512 context-ID total dependency budget, loading at most 240
context IDs per batch.

## Authorization and installation

Apply `evolvmem/pg_business_core/sql/013_history_sources_paged.sql` as the
database owner after SQL012, using the same trusted psql variables as the
existing migrations:

```bash
psql -v ON_ERROR_STOP=1 \
  -v database_name="$DB" -v owner_role="$OWNER" \
  -f evolvmem/pg_business_core/sql/013_history_sources_paged.sql
```

The migration is additive and runs in one transaction.  It checks exact
preimages for SQL005 `history_item_inputs` and the SQL012 provisioner, creates
the two read entrypoints plus one internal payload helper, revokes PUBLIC, and
copies only the old input function's explicit EXECUTE recipients.  It recreates
`provision_history_reader` with its SQL012 body and adds only the two new
EXECUTE grants.  It does not alter tables, rows, indexes, readiness, epochs, or
the existing 37-table revocations.  Existing readers are not re-provisioned by
this migration; run the existing provisioner for a new role as before.

## Limits and rollback

This package solves source-list cardinality only.  Every other section keeps
its prior bound, and a single source with more than 64 `event_proofs` is still
rejected explicitly by Python and by the first-page SQL guard.  It is not a
claim that all nested dependency types are unbounded.

The migration adds no data and no destructive DDL.  To roll back application
code, restore the prior `history_qualification.py` and
`history_qualification_sql.py`; the added SQL functions may remain installed
without being called.  Do not drop or rewrite original tables as a rollback.
Keep the existing recovery set; this candidate creates no database backup.
