"""Behavioral contracts for the rolling project summary generator (B2).

Pinned here: source-set dedup (an unchanged source set never reaches the
LLM), a failed generation keeps the old active summary and flips the rollup
row to ``failed``, one active ``project:{p}:knowledge:current`` identity per
project, degradation without an LLM writes nothing, the relational source
closure is recorded as ``context_reference`` rows, and the full output gate
set matches the playbook generator's. All LLM calls are fake callables; no
real backend is ever contacted.
"""

import json
import logging

import pytest

from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextMode,
    ContextScope,
    ContextStatus,
    ContextValidationError,
)
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.legacy_models import LegacyExtractionItem, LegacyExtractionRequest
from evolvmem.memory_store import MemoryStore
from evolvmem.project_rollup import ProjectRollupGenerator, ProjectRollupReport
from evolvmem.project_store import ProjectStore


@pytest.fixture
def store(test_config):
    with ContextStore(test_config) as instance:
        yield instance


@pytest.fixture
def dual_store(test_config):
    with MemoryStore(test_config):
        pass  # legacy projection schema, mirroring a pre-cutover database
    with ContextStore(test_config) as instance:
        yield instance


@pytest.fixture
def service(test_config, dual_store):
    instance = ContextService(test_config, store=dual_store)
    instance.initialize(mode=ContextMode.SHADOW, adapter="kimi")
    yield instance
    instance.close()


class _SpyLlm:
    """Records every prompt and replays a canned response."""

    def __init__(self, response):
        self.response = response
        self.prompts: list[str] = []

    def __call__(self, prompt: str):
        self.prompts.append(prompt)
        return self.response


class _RaisingLlm:
    def __call__(self, prompt: str):
        raise RuntimeError("backend exploded at /home/alice/secret.gguf")


def _ok_response(**overrides) -> str:
    payload = {
        "l0": "项目当前聚焦滚动摘要链路收敛。",
        "l1": "进展：已完成项目归属接线；待办：滚动摘要按来源集滚动更新。",
        "l2": "完整细节：来源为项目会话摘要；旧摘要携带历史脉络；验证为来源闭包一致。",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _make_summary(store, project: str, tag: str) -> int:
    item = store.create_item(
        ContextItemDraft(
            identity_key=f"project:{project}:progress:log:{tag}",
            content_type=ContextContentType.SESSION_SUMMARY,
            layers=ContextLayers(
                l0=f"会话摘要 {tag} 要点。",
                l1=f"细节：{tag} 的进展与决定。",
                l2=f"完整正文：{tag} 的症状、假设、修改与验证。",
                generator="test-suite",
            ),
            project=project,
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
        )
    )
    return item.id


def _make_atomic(
    store, project: str, tag: str, content_type=ContextContentType.DECISION
) -> int:
    item = store.create_item(
        ContextItemDraft(
            identity_key=f"project:{project}:atomic:{tag}",
            content_type=content_type,
            layers=ContextLayers(
                l0=f"原子条目 {tag} 要点。",
                l1=f"细节：{tag} 的原子内容。",
                l2=f"完整正文：{tag} 的原子证据。",
                generator="test-suite",
            ),
            project=project,
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
        )
    )
    return item.id


def _rollup_row(store, project: str):
    return store._connection().execute(
        "SELECT project, current_context_id, source_set_hash, covered_through,"
        " generator_version, status, revision FROM context_project_rollups"
        " WHERE project=?",
        (project,),
    ).fetchone()


def _register(store, *projects: str) -> None:
    ps = ProjectStore(store._connection(), store._require_transaction, generic_names=())
    with store.transaction():
        for project in projects:
            ps.register_project(project)


def _extraction_request(project: str, tag: str) -> LegacyExtractionRequest:
    return LegacyExtractionRequest(
        summary=LegacyExtractionItem(
            key=f"project:{project}:progress:log:{tag}",
            value="本次完成滚动摘要接线并补齐回归验证。",
            attribute="fact",
            tags=("日志", f"分类:{project}"),
            confidence=1.0,
        ),
        candidates=(),
        max_writes=8,
        source_session=f"session-{tag}",
    )


# ---- the brief's pinned tests ----


def test_same_source_set_skips_llm(test_config, store):
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return _ok_response()

    _make_summary(store, "eva", "a")
    _make_summary(store, "eva", "b")
    gen = ProjectRollupGenerator(test_config, store, llm=llm)
    first = gen.rollup_project("eva")
    second = gen.rollup_project("eva")
    assert first.status == "ready" and second.status == "skipped"
    assert second.reason == "unchanged" and len(calls) == 1
    assert second.context_id == first.context_id
    assert second.covered_through == first.covered_through
    row = _rollup_row(store, "eva")
    assert row["status"] == "ready"
    assert row["current_context_id"] == first.context_id
    assert row["generator_version"] == ProjectRollupGenerator.VERSION
    assert row["revision"] == 1


def test_failed_generation_keeps_old_summary(test_config, store):
    _make_summary(store, "eva", "a")
    gen = ProjectRollupGenerator(test_config, store, llm=_SpyLlm(_ok_response()))
    first = gen.rollup_project("eva")
    assert first.status == "ready"

    # a changed source set consults the LLM again; bad JSON must not
    # dethrone the still-authoritative old summary
    _make_summary(store, "eva", "b")
    failing = ProjectRollupGenerator(test_config, store, llm=_SpyLlm("这不是 JSON。"))
    second = failing.rollup_project("eva")
    assert second.status == "failed" and second.reason == "invalid_json"

    old = store.get_item(first.context_id)
    assert old.status is ContextStatus.ACTIVE
    row = _rollup_row(store, "eva")
    assert row["status"] == "failed"
    assert row["current_context_id"] == first.context_id
    assert row["covered_through"] == first.covered_through
    assert row["revision"] == 2


# ---- identity uniqueness and supersession ----


def test_ready_rollups_supersede_previous_summary(test_config, store):
    _make_summary(store, "eva", "a")
    gen = ProjectRollupGenerator(test_config, store, llm=_SpyLlm(_ok_response()))
    first = gen.rollup_project("eva")
    _make_summary(store, "eva", "b")
    second = gen.rollup_project("eva")
    assert second.status == "ready" and second.context_id != first.context_id

    old = store.get_item(first.context_id)
    new = store.get_item(second.context_id)
    assert old.status is ContextStatus.SUPERSEDED
    assert old.superseded_by == second.context_id
    assert new.status is ContextStatus.ACTIVE
    assert new.supersedes == first.context_id
    assert new.identity_key == "project:eva:knowledge:current"
    assert new.content_type is ContextContentType.PROJECT_SUMMARY
    assert new.scope is ContextScope.PROJECT

    identity_rows = store.get_by_identity(
        "project:eva:knowledge:current", project="eva", scope=ContextScope.PROJECT
    )
    active_ids = [
        item.id for item in identity_rows if item.status is ContextStatus.ACTIVE
    ]
    assert active_ids == [second.context_id]
    row = _rollup_row(store, "eva")
    assert row["status"] == "ready" and row["current_context_id"] == second.context_id


# ---- degradation and skip paths ----


def test_missing_llm_writes_nothing(test_config, store):
    _make_summary(store, "eva", "a")
    gen = ProjectRollupGenerator(test_config, store)
    report = gen.rollup_project("eva")
    assert report.status == "skipped" and report.reason == "llm_unavailable"
    assert report.context_id is None
    assert store.list_item_ids(content_type=ContextContentType.PROJECT_SUMMARY) == []
    assert _rollup_row(store, "eva") is None
    referenced = store._connection().execute(
        "SELECT COUNT(*) AS n FROM context_sources"
        " WHERE source_kind='context_reference'"
    ).fetchone()
    assert referenced["n"] == 0


def test_project_without_sources_skips(test_config, store):
    gen = ProjectRollupGenerator(test_config, store, llm=_SpyLlm(_ok_response()))
    report = gen.rollup_project("ghost")
    assert report.status == "skipped" and report.reason == "no_sources"
    assert _rollup_row(store, "ghost") is None


# ---- relational source closure ----


def test_source_closure_records_context_reference_sources(test_config, store):
    first_id = _make_summary(store, "eva", "a")
    second_id = _make_summary(store, "eva", "b")
    gen = ProjectRollupGenerator(test_config, store, llm=_SpyLlm(_ok_response()))
    report = gen.rollup_project("eva")
    assert report.status == "ready"

    rows = [
        row
        for row in store.list_item_sources(report.context_id)
        if row["source_kind"] == "context_reference"
    ]
    assert {row["source_ref"] for row in rows} == {str(first_id), str(second_id)}
    assert all(row["archive_id"] is None for row in rows)
    assert all(
        row["extraction_version"] == ProjectRollupGenerator.VERSION for row in rows
    )
    assert store.get_item(report.context_id).source_count == 2
    assert gen.covered_source_ids("eva") == frozenset({first_id, second_id})
    assert gen.covered_source_ids("ghost") == frozenset()


def test_atomic_items_after_covered_through_join_the_source_set(test_config, store):
    summary_id = _make_summary(store, "eva", "a")
    llm = _SpyLlm(_ok_response())
    gen = ProjectRollupGenerator(test_config, store, llm=llm)
    first = gen.rollup_project("eva")
    assert first.status == "ready"
    assert gen.covered_source_ids("eva") == frozenset({summary_id})

    atomic_id = _make_atomic(store, "eva", "d1")
    # second-granularity timestamps tie inside a fast test; move the atomic
    # strictly past the recorded watermark through the store's transaction
    with store.transaction():
        store._connection().execute(
            "UPDATE context_items SET created_at='2999-01-01 00:00:00' WHERE id=?",
            (atomic_id,),
        )
    second = gen.rollup_project("eva")
    assert second.status == "ready"
    assert len(llm.prompts) == 2
    assert gen.covered_source_ids("eva") == frozenset({summary_id, atomic_id})
    row = _rollup_row(store, "eva")
    assert row["covered_through"] == "2999-01-01 00:00:00"


def test_atomic_items_at_or_before_covered_through_are_excluded(test_config, store):
    _make_summary(store, "eva", "a")
    llm = _SpyLlm(_ok_response())
    gen = ProjectRollupGenerator(test_config, store, llm=llm)
    assert gen.rollup_project("eva").status == "ready"

    # an atomic created in the same second as the watermark stays outside the
    # window (spec: strictly after covered_through), so the set is unchanged
    _make_atomic(store, "eva", "d1")
    report = gen.rollup_project("eva")
    assert report.status == "skipped" and report.reason == "unchanged"
    assert len(llm.prompts) == 1


# ---- output gates (same reason set as the playbook generator) ----


@pytest.mark.parametrize(
    "response, reason",
    [
        ("这不是 JSON，无法解析。", "invalid_json"),
        (
            json.dumps({"l0": "要点。", "l1": "细节。"}, ensure_ascii=False),
            "invalid_json",
        ),
        (
            json.dumps({"l0": "要点。", "l1": "细节。", "l2": 42}, ensure_ascii=False),
            "invalid_json",
        ),
        (_ok_response(l1="   "), "invalid_json"),
        (
            _ok_response(l1="步骤：读取配置，其中 api_key=sk-live-abcdef123456 直接用。"),
            "sensitive_content",
        ),
        (_ok_response(l0="ok"), "low_information"),
        (_ok_response(l2="。。。"), "low_information"),
        (_ok_response(l0="验证路径" * 61), "layer_too_long"),
        (_ok_response(l1="回归验证" * 310), "layer_too_long"),
        (_ok_response(l2="完整细节" * 1510), "layer_too_long"),
    ],
)
def test_output_gates(test_config, store, response, reason):
    _make_summary(store, "eva", "a")
    gen = ProjectRollupGenerator(test_config, store, llm=_SpyLlm(response))
    report = gen.rollup_project("eva")
    assert report.status == "failed" and report.reason == reason
    assert store.list_item_ids(content_type=ContextContentType.PROJECT_SUMMARY) == []
    row = _rollup_row(store, "eva")
    assert row["status"] == "failed"
    assert row["current_context_id"] is None


@pytest.mark.parametrize("response", [None, 42])
def test_llm_no_response(test_config, store, response):
    _make_summary(store, "eva", "a")
    gen = ProjectRollupGenerator(test_config, store, llm=_SpyLlm(response))
    report = gen.rollup_project("eva")
    assert report.status == "failed" and report.reason == "llm_no_response"
    assert _rollup_row(store, "eva")["status"] == "failed"


def test_raising_llm_is_llm_no_response(test_config, store):
    _make_summary(store, "eva", "a")
    gen = ProjectRollupGenerator(test_config, store, llm=_RaisingLlm())
    report = gen.rollup_project("eva")
    assert report.status == "failed" and report.reason == "llm_no_response"
    assert store.list_item_ids(content_type=ContextContentType.PROJECT_SUMMARY) == []


def test_failed_rollup_is_retried_not_skipped(test_config, store):
    _make_summary(store, "eva", "a")
    bad = ProjectRollupGenerator(test_config, store, llm=_SpyLlm("这不是 JSON。"))
    assert bad.rollup_project("eva").status == "failed"
    good = ProjectRollupGenerator(test_config, store, llm=_SpyLlm(_ok_response()))
    report = good.rollup_project("eva")
    assert report.status == "ready"
    assert _rollup_row(store, "eva")["status"] == "ready"


# ---- prompt hygiene ----


def test_prompt_redacts_source_and_old_summary_without_mutating_store(
    test_config, store
):
    source_secret = "source-secret-value"
    old_secret = "old-secret-value"
    source_id = store.create_item(
        ContextItemDraft(
            identity_key="project:eva:progress:log:sensitive",
            content_type=ContextContentType.SESSION_SUMMARY,
            layers=ContextLayers(
                l0="会话摘要记录联调结果。",
                l1=f"进展：使用 token={source_secret} 完成联调。",
                l2="完整正文保留在本地库。",
                generator="test-suite",
            ),
            project="eva",
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
        )
    ).id
    old_id = store.create_item(
        ContextItemDraft(
            identity_key="project:eva:knowledge:current",
            content_type=ContextContentType.PROJECT_SUMMARY,
            layers=ContextLayers(
                l0="项目已有滚动摘要。",
                l1=f"旧摘要记录 token={old_secret} 需要轮换。",
                l2="旧摘要完整细节保留在本地库。",
                generator="test-suite",
            ),
            project="eva",
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
        )
    ).id
    llm = _SpyLlm(_ok_response())

    report = ProjectRollupGenerator(test_config, store, llm=llm).rollup_project(
        "eva"
    )

    assert report.status == "ready"
    prompt = llm.prompts[0]
    assert source_secret not in prompt
    assert old_secret not in prompt
    assert prompt.count("token=[已脱敏:token]") == 2
    assert store.get_item(source_id).layers.l1 == (
        f"进展：使用 token={source_secret} 完成联调。"
    )
    assert store.get_item(old_id).layers.l1 == (
        f"旧摘要记录 token={old_secret} 需要轮换。"
    )


def test_prompt_uses_conservative_generation_targets(test_config, store):
    _make_summary(store, "eva", "a")
    llm = _SpyLlm(_ok_response())

    report = ProjectRollupGenerator(test_config, store, llm=llm).rollup_project(
        "eva"
    )

    assert report.status == "ready"
    prompt = llm.prompts[0]
    assert "l0 为一句话项目状态要点，不超过 160 字" in prompt
    assert "l1 为当前进展、关键决定与待办，不超过 800 字" in prompt
    assert "l2 为完整细节与来源脉络，不超过 3000 字" in prompt
    assert "不超过 6000 字" not in prompt


def test_prompt_carries_only_l1_layers_and_old_summary(test_config, store):
    _make_summary(store, "eva", "a")
    llm = _SpyLlm(_ok_response())
    gen = ProjectRollupGenerator(test_config, store, llm=llm)
    first = gen.rollup_project("eva")
    prompt_one = llm.prompts[0]
    assert "细节：a 的进展与决定。" in prompt_one
    assert "完整正文：a" not in prompt_one  # L2 原文从不外发

    _make_summary(store, "eva", "b")
    second = gen.rollup_project("eva")
    assert second.status == "ready"
    old_l1 = store.get_item(first.context_id).layers.l1
    prompt_two = llm.prompts[1]
    assert old_l1 in prompt_two  # 旧摘要的 L1 进入下一轮 prompt
    assert "细节：b 的进展与决定。" in prompt_two


# ---- vector sync handoff ----


def test_vector_sync_failure_marks_rollup_vector_dirty(test_config, store):
    _make_summary(store, "eva", "a")
    calls = []

    def vector_sync(context_id, superseded_id, l0):
        calls.append((context_id, superseded_id))
        return False

    gen = ProjectRollupGenerator(
        test_config, store, llm=_SpyLlm(_ok_response()), vector_sync=vector_sync
    )
    report = gen.rollup_project("eva")
    assert report.status == "vector_dirty" and report.reason == ""
    assert report.context_id is not None
    # the summary stays authoritative; only the derived cache is stale
    assert store.get_item(report.context_id).status is ContextStatus.ACTIVE
    row = _rollup_row(store, "eva")
    assert row["status"] == "vector_dirty"
    assert row["current_context_id"] == report.context_id
    assert calls == [(report.context_id, None)]


def test_vector_sync_exception_marks_rollup_vector_dirty(test_config, store):
    _make_summary(store, "eva", "a")

    def vector_sync(context_id, superseded_id, l0):
        raise RuntimeError("index at /home/alice/secret.idx exploded")

    gen = ProjectRollupGenerator(
        test_config, store, llm=_SpyLlm(_ok_response()), vector_sync=vector_sync
    )
    report = gen.rollup_project("eva")
    assert report.status == "vector_dirty"
    assert _rollup_row(store, "eva")["status"] == "vector_dirty"


def test_vector_sync_receives_superseded_id_on_second_rollup(test_config, store):
    _make_summary(store, "eva", "a")
    calls = []

    def vector_sync(context_id, superseded_id, l0):
        calls.append((context_id, superseded_id))
        return True

    gen = ProjectRollupGenerator(
        test_config, store, llm=_SpyLlm(_ok_response()), vector_sync=vector_sync
    )
    first = gen.rollup_project("eva")
    _make_summary(store, "eva", "b")
    second = gen.rollup_project("eva")
    assert second.status == "ready"
    assert calls == [(first.context_id, None), (second.context_id, first.context_id)]


# ---- rollup_all ----


def test_rollup_all_covers_every_project_with_sources(test_config, store):
    _make_summary(store, "eva", "a")
    _make_summary(store, "hermes", "b")
    _make_summary(store, "", "orphan")  # unresolved project never rolls up
    gen = ProjectRollupGenerator(test_config, store, llm=_SpyLlm(_ok_response()))
    reports = gen.rollup_all()
    assert [report.project for report in reports] == ["eva", "hermes"]
    assert all(report.status == "ready" for report in reports)


def test_rollup_all_on_empty_store_returns_nothing(test_config, store):
    gen = ProjectRollupGenerator(test_config, store, llm=_SpyLlm(_ok_response()))
    assert gen.rollup_all() == ()


# ---- report and argument validation ----


def test_report_validates_status_reason_and_consistency():
    with pytest.raises(ContextValidationError):
        ProjectRollupReport("eva", "bogus", "", None, None)
    with pytest.raises(ContextValidationError):
        ProjectRollupReport("eva", "ready", "", None, None)
    with pytest.raises(ContextValidationError):
        ProjectRollupReport("eva", "ready", "unchanged", 1, "t")
    with pytest.raises(ContextValidationError):
        ProjectRollupReport("eva", "failed", "unchanged", None, None)
    with pytest.raises(ContextValidationError):
        ProjectRollupReport("eva", "skipped", "invalid_json", None, None)
    with pytest.raises(ContextValidationError):
        ProjectRollupReport("", "skipped", "no_sources", None, None)
    with pytest.raises(ContextValidationError):
        ProjectRollupReport("eva", "ready", "", -1, None)
    ok = ProjectRollupReport("eva", "skipped", "no_sources", None, None)
    assert ok.project == "eva" and ok.context_id is None


def test_rollup_project_rejects_blank_project(test_config, store):
    gen = ProjectRollupGenerator(test_config, store, llm=_SpyLlm(_ok_response()))
    with pytest.raises(ContextValidationError):
        gen.rollup_project("  ")
    with pytest.raises(ContextValidationError):
        gen.covered_source_ids("")


def test_generator_validates_constructor_arguments(test_config, store):
    with pytest.raises(ContextValidationError):
        ProjectRollupGenerator("not-a-config", store)
    with pytest.raises(ContextValidationError):
        ProjectRollupGenerator(test_config, "not-a-store")
    with pytest.raises(ContextValidationError):
        ProjectRollupGenerator(test_config, store, llm=42)
    with pytest.raises(ContextValidationError):
        ProjectRollupGenerator(test_config, store, vector_sync=42)


# ---- ContextService trigger wiring ----


def test_persist_extraction_rolls_up_resolved_summary_project(test_config, dual_store):
    test_config.embedding_dim = 3

    class _Engine:
        is_loaded = True

        def encode_document(self, text):
            return [1.0, 0.0, 0.0]

        def close(self):
            pass

    service = ContextService(test_config, store=dual_store, embedding_engine=_Engine())
    service.initialize(mode=ContextMode.SHADOW, adapter="kimi")
    try:
        _register(dual_store, "eva")
        result = service.persist_legacy_extraction(
            _extraction_request("eva", "t1"), llm=_SpyLlm(_ok_response())
        )
        assert result.persisted == 1
        summaries = dual_store.list_item_ids(
            status=ContextStatus.ACTIVE,
            content_type=ContextContentType.PROJECT_SUMMARY,
            project="eva",
        )
        assert len(summaries) == 1
        row = _rollup_row(dual_store, "eva")
        assert row["status"] == "ready"
        assert row["current_context_id"] == summaries[0]
        summary_item = dual_store.get_item(result.summary.context_id)
        assert summary_item.project == "eva"
        gen = ProjectRollupGenerator(test_config, dual_store)
        assert gen.covered_source_ids("eva") == frozenset({summary_item.id})
    finally:
        service.close()


def test_persist_extraction_without_llm_skips_rollup(service, dual_store):
    _register(dual_store, "eva")
    result = service.persist_legacy_extraction(_extraction_request("eva", "t1"))
    assert result.persisted == 1
    assert (
        dual_store.list_item_ids(content_type=ContextContentType.PROJECT_SUMMARY) == []
    )
    assert _rollup_row(dual_store, "eva") is None


def test_persist_extraction_with_unresolved_summary_skips_rollup(service, dual_store):
    request = LegacyExtractionRequest(
        summary=LegacyExtractionItem(
            key="plain-progress-note",
            value="无信号摘要，保持未解析状态。",
            attribute="fact",
            tags=(),
            confidence=1.0,
        ),
        candidates=(),
        max_writes=8,
        source_session="session-orphan",
    )
    result = service.persist_legacy_extraction(request, llm=_SpyLlm(_ok_response()))
    assert result.persisted == 1
    assert dual_store.get_item(result.summary.context_id).project == ""
    assert (
        dual_store.list_item_ids(content_type=ContextContentType.PROJECT_SUMMARY) == []
    )


def test_persist_extraction_without_engine_marks_vector_dirty(service, dual_store):
    _register(dual_store, "eva")
    result = service.persist_legacy_extraction(
        _extraction_request("eva", "t1"), llm=_SpyLlm(_ok_response())
    )
    assert result.persisted == 1
    row = _rollup_row(dual_store, "eva")
    assert row["status"] == "vector_dirty"
    # the summary itself is committed and authoritative regardless
    summary = dual_store.get_item(row["current_context_id"])
    assert summary.status is ContextStatus.ACTIVE
    assert summary.content_type is ContextContentType.PROJECT_SUMMARY


def test_rollup_trigger_failure_does_not_affect_extraction(
    service, dual_store, monkeypatch, caplog
):
    _register(dual_store, "eva")

    class _ExplodingGenerator:
        def __init__(self, *args, **kwargs):
            pass

        def rollup_project(self, project):
            raise RuntimeError("rollup exploded at /home/alice/secret.db")

    monkeypatch.setattr(
        "evolvmem.context_service.ProjectRollupGenerator", _ExplodingGenerator
    )
    with caplog.at_level(logging.WARNING, logger="evolvmem.context_service"):
        result = service.persist_legacy_extraction(
            _extraction_request("eva", "t1"), llm=_SpyLlm(_ok_response())
        )
    assert result.persisted == 1
    summary_item = dual_store.get_item(result.summary.context_id)
    assert summary_item.status is ContextStatus.ACTIVE
    assert _rollup_row(dual_store, "eva") is None
    # logs carry a stable line only — never the backend message or its paths
    assert "secret" not in caplog.text
    assert "exploded" not in caplog.text
