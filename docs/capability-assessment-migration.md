# Existing capability value assessment

This migration evaluates the existing memory ID and its existing title/body. It never re-extracts a replacement memory, changes its fingerprint, edits original archives or Context layers, resets manual ownership, or manufactures successful experience. Every assessment remains `unverified`.

## Prepare and apply

Deploy the capability source/assessment schema and complete the separately reviewed source-binding backfill first. Prepare is SELECT-only (SQLite `mode=ro`, `query_only=ON`, a read transaction); it does not initialize schema or call a model. The exported plan contains identifiers, reasons, hashes, counts and versions, never titles, bodies or quotes.

```sh
python -m evolvmem.capability_assessment_migration prepare \
  --db /path/to/memory.db --data-dir /path/to/data \
  --output /path/to/value-plan.json

python -m evolvmem.capability_assessment_migration apply \
  --db /path/to/memory.db --data-dir /path/to/data \
  --plan /path/to/value-plan.json --limit 5 \
  --output /path/to/value-receipt-001.json
```

`--policy-data-dir` is useful only for an isolated read-only policy reference. Apply must use the current service policy matching the plan. The CLI opens an existing writable DB without bootstrap; production schema deployment is a prerequisite. Prepare must be repeated after source backfill, source/manual changes, policy changes or other CAS conflicts. Do not apply the baseline plan to live data.

Default limit is five **source assessments**, not five memories. Maximum is 100. Repeat the same exact plan to resume, recording each receipt separately. `remaining=0` means its actionable entries are complete; `blocked>0` remains explicit unfinished coverage. Unbound invalid/historical origins cannot be assigned a synthetic source ID just to clear this count. A source needing review receives an auditable review assessment without a model call once a legitimate binding exists.

The Python APIs are `build_plan(conn, rule_revision=...)` and `apply_plan(service, plan, limit=5, provider=None, reviewer=None)`. Injected providers/reviewers support offline tests. Default apply uses the configured provider; production model execution is an explicit operational step, not performed by prepare.

## Evidence contract

- Unit originals use `unit_source_proof`: exact frozen source ranges, including disjoint ranges, must reassemble the stored unit. Live provenance separately checks the current source version. No generated unit summary is accepted as original. Migration conservatively leaves unit speaker roles unknown when trusted message spans are unavailable.
- Context generated L2 is never original dialogue. Native KnowledgeBase manual records require the manual source and knowledge-edit generator, a matching body hash and eligible current content. They have unknown speaker roles.
- A Context can instead use directly linked current archives with authenticated logical provenance and intact cleaned messages. The messages hash and rendered body must agree. Historical, purged, missing, deleted or superseded archives cannot support current value. This route uses stored cleaned dialogue, never decrypts the raw backup.
- Each input is limited to 24,000 characters and at most 16 directly linked archives. Over-budget inputs become explicit review; text is not truncated into an apparently complete proof.
- The model may return only kind, trigger, conditions, action_steps, result, evidence_quotes and value_reason. Category, title, content and memory identity are fixed. Invented fields/quotes are rejected.
- Policy `assessment-policy:v2` checks the title as well as content and structured claims. A short faithful label may pass a separate hash-bound semantic review. An enlarged title claim cannot hide behind a verbatim body quote. Support cache keys include the policy version, original input hash and candidate hash; v1 receipts cannot certify a v2 title.
- Value and execution verification remain separate. User/tool wording and model endorsement never create an ExperienceService success event.

## CAS, resume and preservation

Plans bind the entire existing memory row, target binding, complete current source set, original proof/state, previous assessment rows and policy hash/version. A model call runs outside SQLite locks; all bindings are checked again before commit. New sources attached during the call also cause a conflict. Manual ownership-only changes abort an in-flight plan, but do not invalidate a previously completed content assessment when the original is unchanged.

An apply-only audit table, `existing_capability_assessment_runs`, records plan/memory/source, lease, assessment ID and result hash. The transaction rechecks done state before claiming a lease. A completed receipt cannot become running/failed due to a stale worker list. Leases last 300 seconds. Transport/runtime/CAS failures stop the batch; committed assessments are idempotent. Any later operational retry must be bounded and use a still-current plan. A crash after a remote call but before commit may require that call again; this is not an exactly-once billing guarantee.

The first assessment of an old active memory freezes its independently valid older origins and their content/version stamps. Explicit origins retain the source ID; an implicit legacy mapping/ref may gain its first ID only for the same frozen original. Ownership labels are excluded from the stamp and read live. Same-origin aliases are not independent; new sources, changed original refs/content, and sources invalid at the freeze cannot acquire an exemption. Once a source is assessed weakly it cannot use its old exemption. Recall uses explicit allowed origin facts, never a boolean or a None-ID wildcard.

No rollback deletes assessment/output history. Stop scheduling and prepare a fresh plan after conflict; completed results remain auditable. Original records and memory identity remain unchanged.


## Bounded response correction

The extraction prompt includes a complete seven-field JSON example with exact field types, array limits, and the requirement that every evidence quote is a contiguous verbatim substring of the same proven original. The existing category/title/content are supplied as immutable assertions, not editable output fields.

Only recognized model-response validation failures permit one correction call: invalid JSON/object shape, an extra/missing field, an invalid structured value, or a quote absent from the original. The correction receives the same fixed memory and original plus a controlled error code. The rejected payload is neither echoed back nor normalized into acceptable evidence. Complete source/memory/policy CAS and the current lease token/expiry are checked before and after each call.

After two invalid extraction responses, the source receives an auditable technical review with reason `model_response_invalid`, a controlled error code, both error codes, and `attempts=2` under `details.model_response`. Its structure is empty except the required neutral `kind=fact`; evidence is empty, score is zero, and the valid original proof and input hashes remain attached. No rejected model text survives as a knowledge assertion. This completed review lets the batch advance to other sources, and an identical resumed plan does not call the model again. It does not make the source recall-eligible or verified.

A nonempty proven original followed by a completely empty extraction template (all fields except `kind` empty, including `value_reason`) is an incomplete response, not a low-value judgment. It shares the same two-attempt ceiling with all other extraction corrections. The correction must explain an evidence gap in `value_reason` if no quote supports the fixed memory; it must never invent quotes. Two empty responses finish as `review`, reason `extraction_incomplete`, error code `incomplete_capability_extraction`, with no evidence and no verification. An explained empty quote array remains an ordinary `missing_evidence` review after one extraction call. The UI explicitly labels incomplete extraction while keeping the memory visible.

Independent support prompts now state the exact existing response constraints: both fixed hashes, JSON boolean `supported`, a reason with 1–1000 characters after trimming, at most 20 nonblank exact contiguous quotes, and at least one quote for `supported=true`. Validation thresholds and policy version are unchanged. `ValueError("invalid_support_review")` remains the error-string contract; its subtype carries only server-generated field/code diagnostics and numeric indices, lengths or counts. Migration persists those diagnostics as one list per failed attempt under `details.model_response.diagnostics`; rejected values, field names supplied by the model, hashes and raw responses are never included. Extraction diagnostics similarly distinguish array shape/count, item type/length, and absent exact quotes without retaining rejected text.

A valid but unsupported old title/body still receives the distinct knowledge reason `unsupported_claim`. It is never retried to seek a positive judgment. A malformed independent-support response receives technical review with `stage=support` and one support attempt; support calls have no correction loop. Transport errors, unknown runtime errors, source/policy changes, lost or expired leases, and persistence failures stop processing. They are never converted into a statement that the knowledge lacks support, never trigger another extraction call after a write, and never weaken the original quote or source checks. At most three model calls occur per source: two extraction attempts plus one support judgment.

This change requires an operational release followed by a fresh plan. Keep prior frozen runs and their partial committed assessments; do not rewrite old run hashes or delete failed history. There are no schema changes and no changes to existing memory identities or originals.

## Read-only baseline coverage, 2026-10-31

Artifact: `/home/jiangli/evolvmem-governance-backups/20261031-capability-assessment-plan/plan.json`. Canonical baseline and encrypted files were not modified. Policy reference SHA256: `ce4b3c8bb64b07f031bf00fb87ceeb80f565207cef1e0e5ee5fd3a7526503c03`.

There are 2,352 existing memories; 2,148 currently eligible memories have complete coverage in the plan. Of those, 1,746 have at least one model-ready original, and 402 have only review-ready sources (two memories have both kinds). No eligible memory is omitted.

The baseline predates concrete source bindings, so actionable entries are zero until backfill. Its 2,160 valid source relations comprise:

| Source treatment | Relations |
| --- | ---: |
| Exact unit original | 1,691 |
| Direct current archive original | 46 |
| Native manual original | 15 |
| Generated Context with no original | 377 |
| Linked original archive not current | 29 |
| Original input over size budget | 1 |
| Original archive cleaned body unavailable | 1 |

Thus 1,752 sources can be sent to a model after binding and 408 require explicit review. A further 213 invalid unbound relations are separately reported: 204 historical-only units, six ineligible Contexts, two inactive Contexts, and one unverifiable source body. They do not become valid through this migration.

Preparation took about 10–13 seconds in the isolated baseline environment. No production models were called. Each model-ready source uses one extraction call, at most one correction call, and at most one independent support call: a ceiling of 5,256 calls for these 1,752 sources. Review-only sources make no model call. Measure representative model batches before scheduling throughput; subsecond review-only batches do not measure model throughput.

## Verification

Temporary SQLite tests cover metadata-only preparation; immutable IDs/title/body/fingerprint; exact quote and title support; source/ownership/policy and newly attached-source CAS; stale original links; generated versus native/manual/direct-archive input; multi-source default limits and resume; idempotent concurrent claims; frozen explicit/implicit legacy origins; no new-source or same-origin-alias bypass; and the unverified outcome boundary. All model responses are injected fakes.

## Deterministic worker partitions

Use `partition_plan(plan, shards=2, shard_index=0)` or the DB-free `partition` CLI to derive both worker plans from one exact, unapplied parent. Membership is `memory_id % shards == shard_index`, so sparse/legacy capability IDs work without assuming Context IDs or contiguous numbering. Every source of one memory stays together. Coverage and blocked rows are partitioned too, summaries are recomputed, and each child records its parent hash and a new child hash. Source/content/assessment/policy CAS fields are unchanged. Default `shards=1, shard_index=0` leaves the plan unchanged. Valid shard counts are 1–64; operational concurrency should initially be two.

```sh
python -m evolvmem.capability_assessment_migration partition \
  --plan /path/to/base.json --shards 2 --shard-index 0 --output /path/to/shard-0.json
python -m evolvmem.capability_assessment_migration partition \
  --plan /path/to/base.json --shards 2 --shard-index 1 --output /path/to/shard-1.json
```

Preparation also accepts `--shards/--shard-index`, but parallel workers should use the same saved parent so they do not observe different database snapshots. Apply accepts a frozen plan and does not dynamically select shards. Repartitioning an existing child is rejected.

Benchmark one bounded batch using the **final shard-0 plan**, then resume one serial worker per shard, each with its own SQLite connection. Do not apply the parent first and then partition that old plan: the parent's assessment receipts are not owned by its children, so CAS correctly rejects them. If the parent has already run, prepare a fresh current parent before partitioning. Do not run two workers on the same child; its active lease intentionally rejects duplicate claims. Stop an affected worker on CAS conflict and retain its receipts; do not automatically weaken/rewrite the plan.
