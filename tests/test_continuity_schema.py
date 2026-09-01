import sqlite3

import pytest

from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayer,
    ContextLayers,
    ContextScope,
    ContextStatus,
    ContextTier,
)
from evolvmem.context_retriever import ContextRetriever
from evolvmem.context_models import ContextSearchRequest
from evolvmem.context_store import ContextStore


@pytest.fixture
def store(test_config):
    with ContextStore(test_config) as instance:
        yield instance


def make_draft(
    identity_key: str,
    *,
    l0: str = "zebra fact summary",
    l1: str | None = None,
    l2: str | None = None,
    content_type: ContextContentType = ContextContentType.FACT,
    project: str = "proj",
    scope: ContextScope = ContextScope.PROJECT,
    status: ContextStatus = ContextStatus.ACTIVE,
    tier: ContextTier = ContextTier.NORMAL,
    importance: float = 5.0,
    confidence: float = 0.9,
    expires_at: str | None = None,
) -> ContextItemDraft:
    return ContextItemDraft(
        identity_key=identity_key,
        content_type=content_type,
        layers=ContextLayers(
            l0=l0,
            l1=l1 or f"detail for {identity_key}",
            l2=l2 or f"source for {identity_key}",
            generator="test-suite",
        ),
        project=project,
        scope=scope,
        status=status,
        tier=tier,
        importance=importance,
        confidence=confidence,
        expires_at=expires_at,
    )


@pytest.fixture(name="make_draft")
def make_draft_fixture():
    """Expose the module-level draft factory through the fixture namespace."""
    return make_draft


def test_new_tables_created_idempotently(test_config):
    from evolvmem.context_store import ContextStore

    expected = {
        "context_project_registry",
        "context_project_aliases",
        "context_project_workspace_bindings",
        "context_project_resolutions",
        "context_project_rollups",
        "session_archive_holds",
        "continuity_workstreams",
        "continuity_focus",
        "continuity_events",
    }
    with ContextStore(test_config) as store:
        store.initialize()
    with ContextStore(test_config) as store:  # second open: re-run must not fail
        store.initialize()
        tables = {
            row[0]
            for row in store._connection().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert expected <= tables


def test_new_content_types_round_trip(store, make_draft):
    item = store.create_item(
        make_draft(
            "project:proj:workstream:ws_1:checkpoint",
            content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
        )
    )
    assert store.get_item(item.id).content_type is ContextContentType.WORKSTREAM_CHECKPOINT


def test_checkpoint_excluded_from_default_retrieval(store, make_draft, test_config):
    store.create_item(
        make_draft(
            "project:proj:workstream:ws_1:checkpoint",
            l0="zebra checkpoint summary",
            content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
            status=ContextStatus.ACTIVE,
        )
    )
    retriever = ContextRetriever(test_config, store, None, None)
    results = retriever.search(ContextSearchRequest(query="checkpoint", project="proj"))
    assert all(r.content_type is not ContextContentType.WORKSTREAM_CHECKPOINT for r in results)
    explicit = retriever.search(
        ContextSearchRequest(
            query="checkpoint",
            project="proj",
            content_types=(ContextContentType.WORKSTREAM_CHECKPOINT,),
        )
    )
    assert any(r.content_type is ContextContentType.WORKSTREAM_CHECKPOINT for r in explicit)
