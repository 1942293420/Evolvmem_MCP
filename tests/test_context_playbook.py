"""Behavioral contracts for LLM-driven playbook generation (P3).

Frozen rules pinned here: eligibility degradation, coverage dedup, candidate
supersession semantics, output gating, prompt assembly (L0/L1 only), and
transactional rollback. All LLM calls use fake callables; no real backend.
"""

import json

import pytest

from evolvmem.context_lifecycle import ContextLifecycle
from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextScope,
    ContextStatus,
    ContextTier,
    ContextValidationError,
)
from evolvmem.context_playbook import (
    PlaybookGenerationReport,
    PlaybookGenerator,
    PlaybookSkip,
)
from evolvmem.context_store import ContextStore


@pytest.fixture
def store(test_config):
    with ContextStore(test_config) as instance:
        yield instance


@pytest.fixture
def lifecycle(test_config, store):
    return ContextLifecycle(test_config, store)


_UNIT_X = [1.0, 0.0]


class _FakeEngine:
    """Deterministic stand-in: maps exact L0 texts to fixed vectors."""

    def __init__(self, vectors: dict[str, list[float]], *, loaded: bool = True):
        self._vectors = vectors
        self.is_loaded = loaded

    def encode_document(self, text: str) -> list[float]:
        return list(self._vectors[text])


class _SpyLlm:
    """Records every prompt and replays a canned response."""

    def __init__(self, response):
        self.response = response
        self.prompts: list[str] = []

    def __call__(self, prompt: str):
        self.prompts.append(prompt)
        return self.response


class _RaisingLlm:
    def __init__(self):
        self.prompts: list[str] = []

    def __call__(self, prompt: str):
        self.prompts.append(prompt)
        raise RuntimeError("backend exploded at /home/alice/secret.gguf")


def _ok_response(**overrides) -> str:
    payload = {
        "l0": "统一读取路径后必须用真实握手回归验证。",
        "l1": "步骤：先收敛 stdin 读取入口；再补回归用例；最后用真实握手验证。",
        "l2": "完整细节：症状为 initialize 无响应；假设为并发预读竞争；"
        "修改为单一读取路径；验证为真实握手回归；反例为单读取已满足的场景。",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _eligible_experience(
    lifecycle,
    store,
    identity_key: str,
    *,
    l0: str,
    confidence: float = 0.8,
    importance: float = 6.0,
    project: str = "proj",
) -> int:
    item = store.create_item(
        ContextItemDraft(
            identity_key=identity_key,
            content_type=ContextContentType.EXPERIENCE,
            layers=ContextLayers(
                l0=l0,
                l1=f"细节：{identity_key} 的步骤与适用条件。",
                l2=f"完整正文：{identity_key} 的症状、假设、修改、验证与反例。",
                generator="test-suite",
            ),
            project=project,
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
            tier=ContextTier.NORMAL,
            tags=("playbook",),
            importance=importance,
            confidence=confidence,
        )
    )
    for _ in range(2):
        lifecycle.record_outcome(item.id, "success")
    return item.id


def _make_cluster(lifecycle, store, *, prefix: str = "c"):
    """Three eligible active experiences with identical vectors → one cluster."""
    vectors = {}
    ids = []
    specs = (
        (f"{prefix}-alpha", 0.9, 5.0),
        (f"{prefix}-beta", 0.6, 9.0),
        (f"{prefix}-gamma", 0.8, 7.0),
    )
    for name, confidence, importance in specs:
        l0 = f"要点：{name} 的统一摘要。"
        vectors[l0] = list(_UNIT_X)
        ids.append(
            _eligible_experience(
                lifecycle,
                store,
                f"experience:test:{name}",
                l0=l0,
                confidence=confidence,
                importance=importance,
            )
        )
    return ids, _FakeEngine(vectors)


def _make_playbook(
    store,
    identity_key: str,
    *,
    status: ContextStatus = ContextStatus.CANDIDATE,
    source_ids=(),
    project: str = "proj",
):
    item = store.create_item(
        ContextItemDraft(
            identity_key=identity_key,
            content_type=ContextContentType.PLAYBOOK,
            layers=ContextLayers(
                l0="既有 playbook 的一句话要点。",
                l1="既有 playbook 的步骤与适用条件。",
                l2="既有 playbook 的完整正文与证据。",
                generator="test-suite",
            ),
            project=project,
            scope=ContextScope.PROJECT,
            status=status,
            tier=ContextTier.NORMAL,
            tags=("playbook",),
            importance=7.0,
            confidence=0.7,
        )
    )
    if source_ids:
        with store.transaction():
            for experience_id in source_ids:
                store.record_experience_source(
                    item.id, experience_id, extraction_version="test-v1"
                )
    return item


def _backdate(store, item_id: int, stamp: str = "2001-01-01 00:00:00") -> None:
    with store.transaction():
        store._connection().execute(
            "UPDATE context_items SET updated_at=? WHERE id=?", (stamp, item_id)
        )


def _status_snapshot(store, ids) -> dict:
    snapshot = {}
    for item_id in ids:
        item = store.get_item(item_id, include_layers=False)
        snapshot[item_id] = (item.status, item.updated_at)
    return snapshot


def _generator(test_config, store, lifecycle, *, llm, engine):
    return PlaybookGenerator(
        test_config, store, lifecycle, llm=llm, embedding_engine=engine
    )


# ---- constructor and report invariants ----


def test_generator_validates_constructor_types(test_config, store, lifecycle):
    with pytest.raises(ContextValidationError):
        PlaybookGenerator("not-a-config", store, lifecycle)
    with pytest.raises(ContextValidationError):
        PlaybookGenerator(test_config, "not-a-store", lifecycle)
    with pytest.raises(ContextValidationError):
        PlaybookGenerator(test_config, store, "not-a-lifecycle")
    with pytest.raises(ContextValidationError):
        PlaybookGenerator(test_config, store, lifecycle, llm="not-callable")


def test_report_dataclasses_validate_reason_codes():
    with pytest.raises(ContextValidationError):
        PlaybookSkip(item_ids=(1, 2, 3), reason="not-a-reason")
    with pytest.raises(ContextValidationError):
        PlaybookSkip(item_ids=(0, 2, 3), reason="already_covered")
    with pytest.raises(ContextValidationError):
        PlaybookGenerationReport(
            created_ids=(1,), skipped=(), reason="embedding_unavailable"
        )
    with pytest.raises(ContextValidationError):
        PlaybookGenerationReport(
            created_ids=(),
            skipped=(PlaybookSkip(item_ids=(1,), reason="already_covered"),),
            reason="llm_unavailable",
        )
    with pytest.raises(ContextValidationError):
        PlaybookGenerationReport(created_ids=(), skipped=(), reason="mystery")


# ---- degradation: no LLM / no embedding engine ----


def test_generate_without_llm_returns_llm_unavailable(test_config, store, lifecycle):
    ids, engine = _make_cluster(lifecycle, store)
    before = _status_snapshot(store, ids)

    report = _generator(test_config, store, lifecycle, llm=None, engine=engine).generate()

    assert isinstance(report, PlaybookGenerationReport)
    assert report.reason == "llm_unavailable"
    assert report.created_ids == ()
    assert report.skipped == ()
    assert _status_snapshot(store, ids) == before
    assert store.list_item_ids(content_type=ContextContentType.PLAYBOOK) == []


def test_generate_without_embedding_engine_returns_empty_report(
    test_config, store, lifecycle
):
    ids, _engine = _make_cluster(lifecycle, store)
    llm = _SpyLlm(_ok_response())

    report = _generator(
        test_config, store, lifecycle, llm=llm, engine=None
    ).generate()

    assert report.reason == "embedding_unavailable"
    assert report.created_ids == ()
    assert report.skipped == ()
    assert llm.prompts == []
    assert store.list_item_ids(content_type=ContextContentType.PLAYBOOK) == []


# ---- success path ----


def test_generate_creates_candidate_playbook_from_cluster(
    test_config, store, lifecycle
):
    ids, engine = _make_cluster(lifecycle, store)
    llm = _SpyLlm(_ok_response())

    report = _generator(test_config, store, lifecycle, llm=llm, engine=engine).generate()

    assert report.reason == "ok"
    assert report.skipped == ()
    assert len(report.created_ids) == 1

    created = store.get_item(report.created_ids[0])
    assert created.content_type is ContextContentType.PLAYBOOK
    assert created.status is ContextStatus.CANDIDATE
    assert created.project == "proj"
    assert created.scope is ContextScope.PROJECT
    # Frozen metadata: confidence is the cluster minimum, importance the maximum.
    assert created.confidence == pytest.approx(0.6)
    assert created.importance == pytest.approx(9.0)
    assert created.identity_key == "playbook:proj:exp-" + "-".join(
        str(item_id) for item_id in sorted(ids)
    )
    assert created.supersedes is None

    payload = json.loads(_ok_response())
    assert created.layers.l0 == payload["l0"]
    assert created.layers.l1 == payload["l1"]
    assert created.layers.l2 == payload["l2"]
    assert len(created.layers.l0) <= test_config.context_l0_max_chars
    assert len(created.layers.l1) <= test_config.context_l1_max_chars
    assert len(created.layers.l2) <= test_config.context_l2_max_chars

    # Every cluster member is linked through the experience dependency chain.
    sources = store.list_item_sources(created.id)
    assert len(sources) == len(ids) >= 3
    assert {row["source_kind"] for row in sources} == {"experience"}
    assert {row["source_ref"] for row in sources} == {
        str(item_id) for item_id in ids
    }
    assert all(row["archive_id"] is None for row in sources)
    assert created.source_count == len(ids)

    # The original experiences keep their state and their L2 untouched.
    for item_id in ids:
        member = store.get_item(item_id)
        assert member.status is ContextStatus.ACTIVE
        assert "完整正文" in member.layers.l2


def test_generate_prompt_contains_only_member_l0_and_l1(
    test_config, store, lifecycle
):
    ids, engine = _make_cluster(lifecycle, store)
    llm = _SpyLlm(_ok_response())

    _generator(test_config, store, lifecycle, llm=llm, engine=engine).generate()

    assert len(llm.prompts) == 1
    prompt = llm.prompts[0]
    for item_id in ids:
        member = store.get_item(item_id)
        assert member.layers.l0 in prompt
        assert member.layers.l1 in prompt
        # The prompt never carries L2 text or raw session material.
        assert member.layers.l2 not in prompt
    assert "完整正文" not in prompt
    assert "原始会话归档正文绝不外发" not in prompt


# ---- dedup: already covered ----


def test_generate_skips_cluster_covered_by_existing_candidate(
    test_config, store, lifecycle
):
    ids, engine = _make_cluster(lifecycle, store)
    _make_playbook(store, "playbook:test:existing", source_ids=ids)
    llm = _SpyLlm(_ok_response())
    before = _status_snapshot(store, ids)

    report = _generator(test_config, store, lifecycle, llm=llm, engine=engine).generate()

    assert report.reason == "ok"
    assert report.created_ids == ()
    assert [(skip.item_ids, skip.reason) for skip in report.skipped] == [
        (tuple(sorted(ids)), "already_covered")
    ]
    assert llm.prompts == []
    assert _status_snapshot(store, ids) == before


def test_generate_skips_cluster_covered_by_active_superset(
    test_config, store, lifecycle
):
    ids, engine = _make_cluster(lifecycle, store)
    _make_playbook(
        store,
        "playbook:test:active-cover",
        status=ContextStatus.ACTIVE,
        source_ids=[*ids, 9999],
    )
    llm = _SpyLlm(_ok_response())

    report = _generator(test_config, store, lifecycle, llm=llm, engine=engine).generate()

    assert report.created_ids == ()
    assert [skip.reason for skip in report.skipped] == ["already_covered"]
    assert llm.prompts == []


def test_generate_ignores_coverage_from_deleted_playbook(
    test_config, store, lifecycle
):
    ids, engine = _make_cluster(lifecycle, store)
    deleted = _make_playbook(store, "playbook:test:deleted", source_ids=ids)
    with store.transaction():
        store.set_item_status(deleted.id, ContextStatus.DELETED)
    llm = _SpyLlm(_ok_response())

    report = _generator(test_config, store, lifecycle, llm=llm, engine=engine).generate()

    assert len(report.created_ids) == 1
    assert report.skipped == ()


def test_generate_second_run_is_idempotent(test_config, store, lifecycle):
    ids, engine = _make_cluster(lifecycle, store)
    llm = _SpyLlm(_ok_response())
    generator = _generator(test_config, store, lifecycle, llm=llm, engine=engine)

    first = generator.generate()
    second = generator.generate()

    assert len(first.created_ids) == 1
    assert second.created_ids == ()
    assert [skip.reason for skip in second.skipped] == ["already_covered"]
    assert len(llm.prompts) == 1


# ---- update semantics: candidate supersession, active protection ----


def test_generate_supersedes_partially_overlapping_candidate(
    test_config, store, lifecycle
):
    ids, engine = _make_cluster(lifecycle, store)
    old = _make_playbook(
        store, "playbook:test:stale", source_ids=sorted(ids)[:2]
    )
    archived = _make_playbook(
        store,
        "playbook:test:archived-partial",
        status=ContextStatus.ARCHIVED,
        source_ids=sorted(ids)[:1],
    )
    _backdate(store, archived.id)
    llm = _SpyLlm(_ok_response())

    report = _generator(test_config, store, lifecycle, llm=llm, engine=engine).generate()

    assert len(report.created_ids) == 1
    created = store.get_item(report.created_ids[0])
    reloaded_old = store.get_item(old.id)
    # Bidirectional links between the stale candidate and its successor.
    assert reloaded_old.status is ContextStatus.SUPERSEDED
    assert reloaded_old.superseded_by == created.id
    assert created.supersedes == old.id
    assert {row["source_ref"] for row in store.list_item_sources(created.id)} == {
        str(item_id) for item_id in ids
    }
    # Archived playbooks with partial overlap are never auto-superseded.
    reloaded_archived = store.get_item(archived.id, include_layers=False)
    assert reloaded_archived.status is ContextStatus.ARCHIVED
    assert reloaded_archived.superseded_by is None
    assert reloaded_archived.updated_at == "2001-01-01 00:00:00"


def test_generate_never_supersedes_active_playbook(test_config, store, lifecycle):
    ids, engine = _make_cluster(lifecycle, store)
    active = _make_playbook(
        store,
        "playbook:test:active-partial",
        status=ContextStatus.ACTIVE,
        source_ids=sorted(ids)[:2],
    )
    _backdate(store, active.id)
    llm = _SpyLlm(_ok_response())

    report = _generator(test_config, store, lifecycle, llm=llm, engine=engine).generate()

    assert len(report.created_ids) == 1
    created = store.get_item(report.created_ids[0])
    assert created.supersedes is None
    reloaded = store.get_item(active.id, include_layers=False)
    assert reloaded.status is ContextStatus.ACTIVE
    assert reloaded.superseded_by is None
    assert reloaded.updated_at == "2001-01-01 00:00:00"


# ---- LLM failure and output gates: cluster skipped, experiences untouched ----


def test_generate_skips_when_llm_returns_none(test_config, store, lifecycle):
    ids, engine = _make_cluster(lifecycle, store)
    llm = _SpyLlm(None)
    before = _status_snapshot(store, ids)

    report = _generator(test_config, store, lifecycle, llm=llm, engine=engine).generate()

    assert report.created_ids == ()
    assert [skip.reason for skip in report.skipped] == ["llm_no_response"]
    assert _status_snapshot(store, ids) == before
    assert store.list_item_ids(content_type=ContextContentType.PLAYBOOK) == []


def test_generate_skips_when_llm_raises(test_config, store, lifecycle):
    ids, engine = _make_cluster(lifecycle, store)
    llm = _RaisingLlm()
    before = _status_snapshot(store, ids)

    report = _generator(test_config, store, lifecycle, llm=llm, engine=engine).generate()

    assert [skip.reason for skip in report.skipped] == ["llm_no_response"]
    assert len(llm.prompts) == 1
    assert _status_snapshot(store, ids) == before
    assert store.list_item_ids(content_type=ContextContentType.PLAYBOOK) == []


def test_generate_skips_malformed_json(test_config, store, lifecycle):
    ids, engine = _make_cluster(lifecycle, store)
    llm = _SpyLlm("这不是 JSON，无法解析。")
    before = _status_snapshot(store, ids)

    report = _generator(test_config, store, lifecycle, llm=llm, engine=engine).generate()

    assert [skip.reason for skip in report.skipped] == ["invalid_json"]
    assert _status_snapshot(store, ids) == before
    assert store.list_item_ids(content_type=ContextContentType.PLAYBOOK) == []


def test_generate_skips_missing_or_non_string_layers(test_config, store, lifecycle):
    ids, engine = _make_cluster(lifecycle, store)
    llm = _SpyLlm(json.dumps({"l0": "要点。", "l1": "细节。"}, ensure_ascii=False))
    before = _status_snapshot(store, ids)
    generator = _generator(test_config, store, lifecycle, llm=llm, engine=engine)

    assert [s.reason for s in generator.generate().skipped] == ["invalid_json"]

    llm.response = json.dumps(
        {"l0": "要点。", "l1": "细节。", "l2": 42}, ensure_ascii=False
    )
    assert [s.reason for s in generator.generate().skipped] == ["invalid_json"]

    llm.response = _ok_response(l1="   ")
    assert [s.reason for s in generator.generate().skipped] == ["invalid_json"]
    assert _status_snapshot(store, ids) == before
    assert store.list_item_ids(content_type=ContextContentType.PLAYBOOK) == []


def test_generate_skips_sensitive_output(test_config, store, lifecycle):
    ids, engine = _make_cluster(lifecycle, store)
    llm = _SpyLlm(
        _ok_response(l1="步骤：先读取配置，其中 api_key=sk-live-abcdef123456 直接使用。")
    )
    before = _status_snapshot(store, ids)

    report = _generator(test_config, store, lifecycle, llm=llm, engine=engine).generate()

    assert [skip.reason for skip in report.skipped] == ["sensitive_content"]
    assert _status_snapshot(store, ids) == before
    assert store.list_item_ids(content_type=ContextContentType.PLAYBOOK) == []


def test_generate_skips_low_information_output(test_config, store, lifecycle):
    ids, engine = _make_cluster(lifecycle, store)
    llm = _SpyLlm(_ok_response(l0="ok"))
    before = _status_snapshot(store, ids)
    generator = _generator(test_config, store, lifecycle, llm=llm, engine=engine)

    assert [s.reason for s in generator.generate().skipped] == ["low_information"]

    llm.response = _ok_response(l2="。。。")
    assert [s.reason for s in generator.generate().skipped] == ["low_information"]
    assert _status_snapshot(store, ids) == before
    assert store.list_item_ids(content_type=ContextContentType.PLAYBOOK) == []


def test_generate_skips_overlong_layers(test_config, store, lifecycle):
    ids, engine = _make_cluster(lifecycle, store)
    llm = _SpyLlm(_ok_response(l0="验证路径" * 61))
    before = _status_snapshot(store, ids)
    generator = _generator(test_config, store, lifecycle, llm=llm, engine=engine)

    assert [s.reason for s in generator.generate().skipped] == ["layer_too_long"]

    llm.response = _ok_response(l1="回归验证" * 310)
    assert [s.reason for s in generator.generate().skipped] == ["layer_too_long"]

    llm.response = _ok_response(l2="完整细节" * 1510)
    assert [s.reason for s in generator.generate().skipped] == ["layer_too_long"]
    assert _status_snapshot(store, ids) == before
    assert store.list_item_ids(content_type=ContextContentType.PLAYBOOK) == []


# ---- transactional rollback ----


def test_generate_rolls_back_when_source_linking_fails(
    test_config, store, lifecycle, monkeypatch
):
    ids, engine = _make_cluster(lifecycle, store)
    old = _make_playbook(
        store, "playbook:test:rollback-partial", source_ids=sorted(ids)[:2]
    )
    _backdate(store, old.id)
    before_experiences = _status_snapshot(store, ids)
    llm = _SpyLlm(_ok_response())

    def boom(item_id, experience_id, *, extraction_version):
        raise RuntimeError("disk full at /home/alice/private")

    monkeypatch.setattr(store, "record_experience_source", boom)
    generator = _generator(test_config, store, lifecycle, llm=llm, engine=engine)

    with pytest.raises(RuntimeError):
        generator.generate()

    # No partial writes: the new playbook is gone, the stale candidate is
    # untouched, and every experience keeps its exact prior state.
    assert store.list_item_ids(content_type=ContextContentType.PLAYBOOK) == [old.id]
    reloaded_old = store.get_item(old.id, include_layers=False)
    assert reloaded_old.status is ContextStatus.CANDIDATE
    assert reloaded_old.superseded_by is None
    assert reloaded_old.updated_at == "2001-01-01 00:00:00"
    assert _status_snapshot(store, ids) == before_experiences


# ---- report hygiene ----


def test_generation_report_carries_no_content(test_config, store, lifecycle):
    ids, engine = _make_cluster(lifecycle, store)
    llm = _SpyLlm(
        _ok_response(l1="生成的细节绝密字样绝不进报告")
    )

    report = _generator(test_config, store, lifecycle, llm=llm, engine=engine).generate()
    rendered = repr(report)

    assert "绝密字样" not in rendered
    assert "完整正文" not in rendered
    assert "要点：" not in rendered
    assert llm.prompts[0] not in rendered
    assert len(report.created_ids) == 1
