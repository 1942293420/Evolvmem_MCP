"""Audited, resumable projection repair; never modifies authored revisions."""
import argparse
import hashlib
import json
from .embedding_client import EmbeddingClient
from .pg_experience_store import load_pg_experience_settings, open_experience_conn
from .writer import ExperienceWriter, Op, TicketIssuer

_SHA = "encode(sha256(convert_to(category || E'\\n' || problem || E'\\n' || solution || E'\\n' || reusable_experience,'UTF8')),'hex')"
_INVALID = ("embedding IS NULL OR retrieval_embedding IS NULL OR "
    "public.vector_norm(embedding)<=0 OR public.vector_norm(retrieval_embedding)<=0 OR "
    "retrieval_embedding_contract IS DISTINCT FROM %s OR "
    "retrieval_embedding_content_sha256 IS DISTINCT FROM " + _SHA)
_ACTIVE = "status='active' AND deleted_at IS NULL"


def projection_health(conn, client):
    row=conn.execute(f"SELECT count(*),count(*) FILTER(WHERE embedding IS NULL),"
        f"count(*) FILTER(WHERE retrieval_embedding IS NULL),"
        f"count(*) FILTER(WHERE {_INVALID}) FROM experience_notes WHERE {_ACTIVE}",
        (client.retrieval_contract_id,)).fetchone()
    return dict(zip(('active','legacy_missing','retrieval_missing','invalid'),map(int,row)))


def repair_projections(conn, *, secret, client):
    writer=ExperienceWriter(secret,embedding_client=client)
    issuer=TicketIssuer(secret,gate_id='projection-maintenance')
    rows=conn.execute(f"SELECT id,owner_id,revision,{_SHA} FROM experience_notes "
        f"WHERE {_ACTIVE} AND ({_INVALID}) ORDER BY owner_id,id",(client.retrieval_contract_id,)).fetchall()
    # Release the read transaction; each item then commits its own CAS-protected repair.
    conn.commit()
    repaired=0
    for nid,owner,revision,digest in rows:
        request_id='reembed:'+hashlib.sha256(f'{owner}:{nid}:{revision}:{digest}:{client.retrieval_contract_id}'.encode()).hexdigest()
        ticket=issuer.issue(proposal_uid='projection:'+nid,decision_id=request_id,lane='user_fast')
        with conn.transaction():
            writer.apply(conn,ticket,Op(action='reembed',owner_id=owner,actor_id=owner,
                request_id=request_id,note_id=nid,expected_revision=revision,
                expected_content_sha256=digest))
        repaired+=1
    return {'repaired':repaired,'health':projection_health(conn,client)}


def activate_policy(conn, client):
    # Block writers only during the final completeness check and policy switch.
    with conn.transaction():
        conn.execute('LOCK TABLE experience_notes IN SHARE ROW EXCLUSIVE MODE')
        health=projection_health(conn,client)
        if health['invalid']:
            raise ValueError('projection_backfill_incomplete')
        conn.execute("INSERT INTO experience_embedding_policy(singleton,required,retrieval_contract_id,legacy_contract_id) "
            "VALUES(true,true,%s,%s) ON CONFLICT(singleton) DO UPDATE SET required=true,"
            "retrieval_contract_id=EXCLUDED.retrieval_contract_id,legacy_contract_id=EXCLUDED.legacy_contract_id,activated_at=now()",
            (client.retrieval_contract_id,client.legacy_contract_id))
    return health


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Repair after making an operator backup; default read-only')
    args=parser.parse_args(argv)
    client=EmbeddingClient.from_environment()
    if client is None:raise ValueError('embedding_unconfigured')
    settings=load_pg_experience_settings()
    with open_experience_conn(settings) as conn:
        out=repair_projections(conn,secret=settings.ticket_secret,client=client) if args.apply else projection_health(conn,client)
        print(json.dumps(out,ensure_ascii=False))

if __name__=='__main__':main()
