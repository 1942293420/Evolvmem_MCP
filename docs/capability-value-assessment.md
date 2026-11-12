# Capability knowledge value and evidence

This change adds a deterministic, source-bound value gate to new capability extraction.
A useful procedure is not automatically a verified experience. Existing archived raw
records, unit text and historical active capabilities are retained.

## Extraction and independent support

The `capability:v3:assessment-policy:v1` receipt stores a structurally validated proposal.
A kept proposal supplies `kind` (`fact`, `decision`, `procedure`, `pitfall`), `trigger`,
`conditions[]`, `action_steps[]`, `result`, `evidence_quotes[]` and `value_reason`, alongside
the existing title/content/category/keywords/date. Unknown facts use empty fields; a
missing evidence list is malformed, while an empty list is a valid review candidate.
Quotes must occur verbatim inside the current unit. Malformed fields, fabricated quotes
and invalid dates are rejected before receipt completion, allowing a corrected retry.

`capability_assessment.evaluate` checks source evidence independently of extractor claims.
Extractor-supplied score, value, verified flag or support verdict are ignored. Role labels
come from the server's structured source messages and exact offsets, not from text such
as `用户：` embedded in an assistant message. Unavailable structural roles remain unknown.

Literal support requires each substantive content/trigger/condition/action/result claim
to occur inside an accepted quote. This is the fast path, not a requirement that every
useful summary copy the source verbatim. For paraphrases, `assess_unit` / pipeline
`process_task(..., support_reviewer=...)` accepts a separately injected support reviewer.
Its request contains current source text, structural roles, claims and hashes of the
source input and candidate. Acceptance requires `supported: true`, both matching hashes,
a nonempty reason, and nonempty exact source quotes. Missing, malformed, failed or stale
review results leave the proposal in review. The extraction provider's own verdict is
never accepted as this independent review. No remote reviewer is instantiated by this
module; integration may supply the existing configured review path.

## Transparent rubric

The stored rubric has five boolean components, each worth one point: source quote exists,
claims are supported, action has trigger/conditions/steps, content is specific rather
than a recognized slogan/progress log, and a result quote occurs in a real user/tool
span. The sum is an audit aid; the score alone never determines admission.

| Value | Deterministic criteria | New known-category record |
|---|---|---|
| `reusable` | Supported procedure or pitfall with trigger, conditions and action steps | Active knowledge |
| `reference` | Supported fact or decision | Active knowledge |
| `review` | Missing evidence, unsupported inference, or incomplete action context | Pending candidate |
| `ignore` | Recognized generic slogan or progress/execution-only content | Pending candidate, excluded from default recall |

Repeating a slogan in structured fields cannot turn it into a reusable procedure.
Failure modes and counterexamples can qualify as useful pitfalls; a positive outcome
is not required for knowledge value. The heuristic reason codes and original structured
proposal remain visible in the assessment details for review.

Every assessment has `verification_status = unverified`. Even a real user's result
quote records only a source-role/quote fact in the rubric; it does not prove that a
method succeeds under all stated conditions. This module never calls ExperienceService
outcome methods, increments success counters or creates a parallel verification system.
Independent verified experience remains the responsibility of the existing evidence
resolver and ExperienceService workflow.

## Persistence, versions and currentness

`capability_assessments` is an additive table. Each row binds a memory and source ID to
its source revision, observed binding/unit revisions, source-text digest, derived-content
digest, policy version, deterministic assessment key, value, score, detailed rubric and
source identity snapshot. Per-source revisions are append-only; identical replays reuse
the same assessment. Source IDs are logical references intentionally retained even if a
source binding is removed, so historical assessments become stale instead of disappearing.

The pipeline inserts/binds the capability, writes the assessment and adjusts a newly
created record's status inside one transaction. Assessment-write failure publishes neither
the memory nor its source. A supported new source can promote a pending record; an existing
ignored or archived record is not reactivated. A weak new extraction never downgrades
independent existing active knowledge. The first assessment of such a legacy active record
records `legacy_active_preserved` so unassessed old supporting sources remain distinguishable
from the newly assessed weak source.

Currentness binds source content/version, source key/ref/digest/range, unit text, derived
content and policy. Source withdrawal, text/range changes and policy changes invalidate
assessment use. Changing only project ownership, decision or associated revision counters
does not invalidate a content-value proof; project attribution is read live from provenance.
The free-text conditions remain attached to the knowledge itself, not inferred from a
mutable source-project label.

## Read APIs and default recall integration

- `assessments(store, memory_id)` returns historical assessments with details, `stale` and
  reason codes. It preserves original source identities after later changes.
- `assessment_status(store, ids=None)` is a batch API keyed by memory ID. It returns
  `assessment_eligible`, `legacy_unassessed`, `legacy_active_preserved`,
  `eligible_source_ids`, `assessed_source_ids`, `stale_count` and `assessments_count`.
  Only the latest assessment for each source participates in the eligible source set.
- `duplicate_candidates(store, content, exclude_memory_id=None, limit=5)` returns bounded
  lexical similarity suggestions from at most 500 recent records. It never merges,
  deletes, changes status or invents an independent source.

A caller must combine the value gate with current provenance **on the same source ID**:

1. With no assessment history, retain existing provenance behavior for legacy records.
2. With assessments, require a current valid source in `eligible_source_ids`.
3. If `legacy_active_preserved`, an old current valid source absent from
   `assessed_source_ids` may still support the record. The new weak source cannot inherit
   that exemption or contribute project labels through it.

The summary `assessment_eligible` is not a replacement for source validity or memory status.
An eligible assessment from one source cannot be combined with validity from an unrelated
weak source. Retrieval, web and vector integration remain separate consumers of this API.

Unknown categories continue to enter the existing pending intake. Their v3 extraction
receipt retains the structured candidate, but an intake has no capability source ID yet.
When assigning an intake category, consumers must bind/evaluate its new source (or retain
pending status); category selection alone must not manufacture a value/verification verdict.

## Validation

Tests use temporary SQLite databases and synthetic providers, without network model calls.
They cover useful-but-unverified knowledge, missing evidence, assistant completion claims,
unsupported generalization, independently reviewed paraphrases with current hashes,
recognized noise, role spoofing, receipt validation/retry, source-assessment idempotence,
source/content/range drift, ownership-only corrections, transaction rollback, promotion
of pending records, preservation of independent active sources and similarity without
automatic merges. Existing pipeline/source/store/primary ownership tests are also run.
