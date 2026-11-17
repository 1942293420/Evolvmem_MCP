"""Projection decisions and the existing outbox's exact-job transaction port."""
from dataclasses import dataclass
from .model import ModelContract,Embedding,VectorError,require,text_hash

ORDINARY_TYPES=frozenset(('decision','fact','constraint','preference','user_profile','reference','session_summary'))
SEARCHABLE_TYPES=ORDINARY_TYPES | frozenset(('experience','playbook','workflow_policy','project_summary','workstream_checkpoint'))


@dataclass(frozen=True,slots=True)
class Job:
    id:int
    library_id:str
    index_kind:str
    entity_id:int
    action:str
    revision:int
    content_hash:str
    model_contract:str
    project_id:str|None = None


@dataclass(frozen=True,slots=True)
class Prepared:
    job:Job
    embedding:Embedding|None


@dataclass(frozen=True,slots=True)
class PreparedPair:
    """One stale anchor plus the exact current job of the same native entity.

    ``decision`` is the current job's prepare-time decision. Apply re-derives it
    from the same locked current document before any effect.
    """
    anchor:Job
    current:Job
    decision:str
    embedding:Embedding|None


@dataclass(frozen=True,slots=True)
class PairApplied:
    """Native pair apply outcome plus the rows actually acknowledged."""
    outcome:str
    acknowledged_job_ids:tuple


@dataclass(frozen=True,slots=True)
class Document:
    library_id:str
    index_kind:str
    entity_id:int
    revision:int
    text:str
    status:str
    expires_at:str|None
    content_type:str|None=None
    source_state:str='none'
    projects:tuple=()
    personal_general:bool=False
    has_experience_payload:bool=False
    unsupported_sources:int=0
    project_id:str|None = None


def supported_context(document):
    # This is the actual current PgBusinessCore._supported_item boundary, not
    # a replacement for SQLite's richer unit/rollup/learning proof services.
    require(document.content_type in ORDINARY_TYPES and document.source_state=='none'
            and not document.projects and document.personal_general is False
            and document.has_experience_payload is False and document.unsupported_sources==0,
            'unsupported_context_evidence')


def decide(job,document,contract,now):
    require(isinstance(job,Job) and isinstance(contract,ModelContract),'typed_projection_input_required')
    require(job.index_kind in ('legacy','context') and job.action in ('upsert','remove'),
            'invalid_vector_job')
    require(job.model_contract==contract.key,'unsupported_model_contract')
    if document is None: return 'stale_missing'
    require(isinstance(document,Document) and (document.library_id,document.index_kind,document.entity_id)
            ==(job.library_id,job.index_kind,job.entity_id),'projection_scope_mismatch')
    if document.revision!=job.revision: return 'stale_revision'
    if text_hash(document.text)!=job.content_hash: return 'stale_content'
    require(document.project_id==job.project_id,'projection_project_mismatch')
    if document.index_kind=='context':
        # Indexing is not permission/source qualification. Publication requires
        # the native eligibility reader in the final authorized read session.
        require(document.content_type in SEARCHABLE_TYPES,'unsupported_context_type')
        present=(document.status in ('active','superseded') and (document.expires_at is None or document.expires_at>now))
    else:
        # The old index retains superseded/archived and expired legacy rows;
        # callers apply status/expiry after the original ranked budget.
        present=document.status!='deleted'
    # A scheduled successor can leave its superseded predecessor effective.
    # Refresh that predecessor's new revision rather than retaining an old
    # revision vector or deleting it. Final canonical time ranking decides use.
    if present and job.action=='remove' and not(document.index_kind=='context' and document.status=='superseded'):
        return 'stale_action'
    return 'upsert' if present else 'remove'
