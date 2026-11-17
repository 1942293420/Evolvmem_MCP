"""Combine current origin and value assessment without mixing evidence sources."""
from evolvmem.capability_provenance import annotate
from evolvmem.capability_assessment import assessment_status, _source_match


def recall_facts(store, ids=None):
    origins = annotate(store, ids)
    assessments = assessment_status(store, list(origins))
    for identity, fact in origins.items():
        value = assessments[identity]
        preserved = value.get("legacy_preserved_origins", ())
        approved = set(value["eligible_source_ids"])
        allowed = [source for source in fact["sources"] if source["valid"] and (
            value["legacy_unassessed"] or source.get("source_id") in approved or
            _source_match(source) in preserved)]
        fact["eligible_sources"] = allowed
        fact["eligible_source_ids"] = [source.get("source_id") for source in allowed]
        fact["recall_eligible"] = fact["eligible"] and bool(allowed)
        fact["source_projects"] = sorted({p for source in allowed for p in source["projects"]})
        fact["ownership_confirmed"] = bool(fact["source_projects"])
        fact["assessment"] = value
    return origins


def eligible_ids(store, ids=None):
    return {identity for identity, fact in recall_facts(store, ids).items() if fact["recall_eligible"]}
