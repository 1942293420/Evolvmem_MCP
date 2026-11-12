"""Read-only acceptance entry for a trusted, already-owned synthetic PG fixture.

No endpoint/secret loader, model creation, projection writes, cleanup or routes.
Root must first seed and project the two fixed keys with the existing supported
business/outbox paths. Missing seeds/model/schema is refusal, never auto-repair.
This same function is exercised by the explicit local external-driver port.
"""
from evolvmem.context_models import ContextContentType, ContextMatchType, ContextSearchRequest
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.pg_business_core.vector_recall import PgVectorContextRecall
from evolvmem.pg_vector_port.runtime import VectorPort

KEYS = ('synthetic-pg-vector-read-20261101-a','synthetic-pg-vector-read-20261101-b')
QUERY = 'pgvectorfixture ordinary reference evidence'


def exercise(fixture, *, contract, embedding_provider, clock=None):
    if not isinstance(fixture.deadline_guard,SyncRequestDeadlineGuard):
        raise RuntimeError('actual_request_guard_required')
    fixture.guard()  # root-owned capacity/target admission, outside request budget
    with fixture.new_request():
        fixture.deadline_guard.begin_authority()
        route=fixture.route('context_read')
        def witness(session):
            rows=session.execute('''SELECT m.key,g.context_item_id FROM em_content.memories m
                JOIN em_content.legacy_memory_migrations g
                 ON g.library_id=m.library_id AND g.legacy_memory_id=m.id
                WHERE m.library_id=%s AND m.key IN (%s,%s) ORDER BY m.key''',
                (route.library.library_id,*KEYS))
            if len(rows)!=2 or tuple(r[0] for r in rows)!=KEYS:
                raise RuntimeError('fixed_synthetic_vector_seeds_required')
            return {r[1] for r in rows}
        expected=fixture.manager.run(route,witness)
        port=VectorPort(fixture.manager,(route,),contract,embedding_provider=embedding_provider)
        request=ContextSearchRequest(QUERY,top_k=2,cross_project=True,
            content_types=(ContextContentType.REFERENCE,ContextContentType.FACT))
        publications=[]
        result=PgVectorContextRecall(port,fixture.config,clock=clock).search(
            route.library.library_id,request,candidate_limit=240,publish=publications.append)
        if (len(result)!=2 or {r.id for r in result}!=expected or publications!=[result]
                or any(r.match_types!=(ContextMatchType.VECTOR,) for r in result)):
            raise RuntimeError('fixed_vector_context_witness_failed')
    fixture.guard()
    return dict(synthetic_items=2,generated_candidates=True,typed_context_dto=True,
        two_snapshot_projection_and_body_proof=True,published_once=True,
        postgres_fts_tested=False,complete_hybrid=False,production_registered=False)
