"""Tests for the project registry / resolution review-queue CLI."""

import json
import os

import numpy as np
import pytest

from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextMode,
    ContextStatus,
)
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.project_models import ProjectResolutionDecision
from evolvmem.project_store import ProjectStore
from evolvmem.project_cli import main
from evolvmem.vector_index import VectorIndex


FP1 = "hmac-sha256:" + "1" * 64
FP2 = "hmac-sha256:" + "2" * 64


@pytest.fixture
def store(test_config):
    with ContextStore(test_config) as instance:
        yield instance


@pytest.fixture
def ps(store):
    return ProjectStore(
        store._connection(),
        store._require_transaction,
        generic_names=(),
    )


def _run(test_config, *argv) -> int:
    return main(["--data-dir", str(test_config.data_dir), *argv])


def _out_json(capsys) -> dict:
    out = capsys.readouterr().out
    # commands run back-to-back in one test; the last line is the latest payload
    return json.loads(out.strip().splitlines()[-1])


def _err_json(capsys) -> dict:
    return json.loads(capsys.readouterr().err)


def _make_draft(identity_key: str) -> ContextItemDraft:
    return ContextItemDraft(
        identity_key=identity_key,
        content_type=ContextContentType.FACT,
        layers=ContextLayers(
            l0="summary", l1="detail", l2="source", generator="test-suite"
        ),
    )


def _seed_pending(store, ps, identity_key: str):
    item = store.create_item(_make_draft(identity_key))
    with store.transaction():
        ps.record_resolution(item.id, ProjectResolutionDecision.conflict("v1", ()))
    return item


# ---- the brief's flow test ----


def test_register_bind_accept_flow(test_config, store, capsys):
    assert main(["--data-dir", str(test_config.data_dir), "projects", "register", "eva"]) == 0
    assert main(["--data-dir", str(test_config.data_dir), "aliases", "add", "evolv", "eva"]) == 0
    out = capsys.readouterr().out
    assert "eva" in out


# ---- projects ----


def test_projects_list_and_archive_cas(test_config, store, capsys):
    assert _run(test_config, "projects", "register", "eva") == 0
    assert _run(test_config, "projects", "list") == 0
    listing = _out_json(capsys)
    assert listing["projects"] == [
        {"project": "eva", "status": "active", "revision": 1}
    ]
    # stale revision and unknown project both fail closed with exit code 2
    assert (
        _run(test_config, "projects", "archive", "eva", "--expected-revision", "99")
        == 2
    )
    assert _err_json(capsys) == {"error": "revision_conflict"}
    assert (
        _run(test_config, "projects", "archive", "ghost", "--expected-revision", "1")
        == 2
    )
    assert _err_json(capsys) == {"error": "project_not_found"}
    assert (
        _run(test_config, "projects", "archive", "eva", "--expected-revision", "1")
        == 0
    )
    assert _run(test_config, "projects", "list") == 0
    listing = _out_json(capsys)
    assert listing["projects"] == [
        {"project": "eva", "status": "archived", "revision": 2}
    ]


# ---- aliases ----


def test_aliases_add_conflict_and_remove_cas(test_config, store, capsys):
    assert _run(test_config, "aliases", "add", "evolv", "ghost") == 2
    assert _err_json(capsys) == {"error": "project_not_found"}
    assert _run(test_config, "projects", "register", "eva") == 0
    assert _run(test_config, "projects", "register", "hermes") == 0
    assert _run(test_config, "aliases", "add", "evolv", "eva") == 0
    # an alias is globally unique; rebinding it conflicts
    assert _run(test_config, "aliases", "add", "evolv", "hermes") == 2
    assert _err_json(capsys) == {"error": "alias_conflict"}
    assert _run(test_config, "aliases", "list") == 0
    assert _out_json(capsys)["aliases"] == [
        {"alias": "evolv", "project": "eva", "revision": 1}
    ]
    assert (
        _run(test_config, "aliases", "remove", "evolv", "--expected-revision", "99")
        == 2
    )
    assert _err_json(capsys) == {"error": "revision_conflict"}
    assert (
        _run(test_config, "aliases", "remove", "evolv", "--expected-revision", "1")
        == 0
    )
    assert _run(test_config, "aliases", "list") == 0
    assert _out_json(capsys)["aliases"] == []


# ---- bindings ----


def test_bindings_bind_set_default_revoke(test_config, store, capsys):
    assert _run(test_config, "projects", "register", "eva") == 0
    assert _run(test_config, "projects", "register", "hermes") == 0
    assert _run(test_config, "bindings", "bind", FP1, "eva") == 0
    assert _run(test_config, "bindings", "bind", FP1, "hermes", "--default") == 0
    assert _run(test_config, "bindings", "list") == 0
    by_project = {
        row["project"]: row for row in _out_json(capsys)["bindings"]
    }
    assert by_project["eva"] == {
        "workspace_fingerprint": FP1,
        "project": "eva",
        "state": "active",
        "is_default": False,
        "method": "cli",
        "revision": 1,
    }
    assert by_project["hermes"]["is_default"] is True
    # a second default for the same fingerprint conflicts
    assert _run(test_config, "bindings", "bind", FP1, "eva", "--default") == 2
    assert _err_json(capsys) == {"error": "default_binding_conflict"}
    # revoking the default frees the slot; then eva can take it
    assert (
        _run(test_config, "bindings", "revoke", FP1, "hermes", "--expected-revision", "1")
        == 0
    )
    assert (
        _run(
            test_config, "bindings", "set-default", FP1, "eva", "--expected-revision", "1"
        )
        == 0
    )
    assert _run(test_config, "bindings", "list") == 0
    by_project = {
        row["project"]: row for row in _out_json(capsys)["bindings"]
    }
    assert by_project["hermes"]["state"] == "revoked"
    assert by_project["hermes"]["is_default"] is False
    assert by_project["eva"]["is_default"] is True
    assert by_project["eva"]["revision"] == 2
    # stale revision vs missing binding are distinct stable codes
    assert (
        _run(test_config, "bindings", "revoke", FP1, "eva", "--expected-revision", "1")
        == 2
    )
    assert _err_json(capsys) == {"error": "revision_conflict"}
    assert (
        _run(test_config, "bindings", "revoke", FP2, "eva", "--expected-revision", "1")
        == 2
    )
    assert _err_json(capsys) == {"error": "binding_not_found"}
    # binding an unregistered project fails closed
    assert _run(test_config, "bindings", "bind", FP2, "ghost") == 2
    assert _err_json(capsys) == {"error": "project_not_found"}


# ---- resolutions review queue ----


def test_resolutions_list_accept_reject(test_config, store, ps, capsys):
    assert _run(test_config, "projects", "register", "eva") == 0
    item_a = _seed_pending(store, ps, "fact:cli-pending:a")
    item_b = _seed_pending(store, ps, "fact:cli-pending:b")
    assert _run(test_config, "resolutions", "list-pending") == 0
    rows = _out_json(capsys)["resolutions"]
    assert [row["item_id"] for row in rows] == sorted([item_a.id, item_b.id])
    # the queue carries review metadata only — never evidence or content
    for row in rows:
        assert set(row) == {
            "item_id",
            "resolution_state",
            "review_state",
            "proposed_project",
            "resolved_project",
            "confidence",
            "method",
            "revision",
        }
        assert row["resolution_state"] == "conflict"
        assert row["review_state"] == "pending"
        assert row["revision"] == 1
    assert _run(test_config, "resolutions", "list-pending", "--limit", "1") == 0
    assert len(_out_json(capsys)["resolutions"]) == 1
    # accept is revision-CASed and moves the item's project atomically
    assert (
        _run(
            test_config,
            "resolutions", "accept", str(item_a.id), "eva",
            "--expected-revision", "99",
        )
        == 2
    )
    assert _err_json(capsys) == {"error": "revision_conflict"}
    assert store.get_item(item_a.id).project == ""
    assert (
        _run(
            test_config,
            "resolutions", "accept", str(item_a.id), "eva",
            "--expected-revision", "1",
        )
        == 0
    )
    assert store.get_item(item_a.id).project == "eva"
    # accept into an unknown project and review of a missing row fail closed
    assert (
        _run(
            test_config,
            "resolutions", "accept", str(item_b.id), "ghost",
            "--expected-revision", "1",
        )
        == 2
    )
    assert _err_json(capsys) == {"error": "project_not_found"}
    assert (
        _run(test_config, "resolutions", "reject", "999999", "--expected-revision", "1")
        == 2
    )
    assert _err_json(capsys) == {"error": "resolution_not_found"}
    # reject leaves the item's project empty and drains the queue
    assert (
        _run(
            test_config,
            "resolutions", "reject", str(item_b.id), "--expected-revision", "1",
        )
        == 0
    )
    assert store.get_item(item_b.id).project == ""
    assert _run(test_config, "resolutions", "list-pending") == 0
    assert _out_json(capsys)["resolutions"] == []


# ---- rollup run ----


def _make_session_summary(store, project: str, tag: str) -> int:
    return store.create_item(
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
            status=ContextStatus.ACTIVE,
        )
    ).id


def _seed_context_vector(test_config, item_id: int) -> None:
    index = VectorIndex(test_config, path=test_config.context_vector_path)
    index.initialize(dim=test_config.embedding_dim)
    index.add(item_id, np.ones(test_config.embedding_dim, dtype=np.float32))
    index.save()
    index.close()


class _LazyCliEmbedding:
    instances = []

    def __init__(self, config):
        self.config = config
        self.loaded = False
        self.initialize_calls = 0
        self.__class__.instances.append(self)

    @property
    def is_loaded(self):
        return self.loaded

    def initialize(self):
        self.initialize_calls += 1
        self.loaded = True

    def encode_document(self, _text):
        return np.ones(self.config.embedding_dim, dtype=np.float32)

    def close(self):
        self.loaded = False


def test_rollup_run_without_llm_reports_llm_unavailable(
        test_config, store, capsys, monkeypatch):
    monkeypatch.setattr("evolvmem.project_cli._load_rollup_llm", lambda: None)
    _make_session_summary(store, "eva", "a")
    _make_session_summary(store, "hermes", "b")
    assert _run(test_config, "rollup", "run") == 0
    lines = [
        json.loads(line) for line in capsys.readouterr().out.strip().splitlines()
    ]
    assert lines == [
        {"project": "eva", "status": "skipped", "reason": "llm_unavailable", "context_id": None},
        {"project": "hermes", "status": "skipped", "reason": "llm_unavailable", "context_id": None},
    ]
    # a single-project run prints exactly that project's line
    assert _run(test_config, "rollup", "run", "--project", "eva") == 0
    lines = [
        json.loads(line) for line in capsys.readouterr().out.strip().splitlines()
    ]
    assert lines == [
        {"project": "eva", "status": "skipped", "reason": "llm_unavailable", "context_id": None}
    ]
    # the degraded run wrote nothing
    assert (
        store._connection()
        .execute("SELECT COUNT(*) AS n FROM context_project_rollups")
        .fetchone()["n"]
        == 0
    )


def test_rollup_run_uses_configured_llm(test_config, store, capsys, monkeypatch):
    source_id = _make_session_summary(store, "eva", "a")
    _seed_context_vector(test_config, source_id)
    response = json.dumps(
        {
            "l0": "项目正在完成滚动摘要接线。",
            "l1": "进展：接入已有模型；决定：复用现有调用；待办：继续验证。",
            "l2": "完整细节：来源是会话摘要，模型输出通过结构和内容门控。",
        },
        ensure_ascii=False,
    )
    prompts = []
    monkeypatch.setattr(
        "evolvmem.project_cli._load_rollup_llm",
        lambda: lambda prompt: prompts.append(prompt) or response,
    )
    monkeypatch.setattr(
        "evolvmem.project_cli.EmbeddingEngine", _LazyCliEmbedding
    )

    assert _run(test_config, "rollup", "run", "--project", "eva") == 0

    payload = _out_json(capsys)
    assert payload["project"] == "eva"
    assert payload["status"] == "ready"
    assert len(prompts) == 1


def test_rollup_run_syncs_new_summary_before_fresh_primary_startup(
    test_config, store, capsys, monkeypatch
):
    source_id = _make_session_summary(store, "eva", "a")
    _seed_context_vector(test_config, source_id)
    response = json.dumps(
        {
            "l0": "项目正在完成滚动摘要接线。",
            "l1": "进展：接入已有模型；决定：复用现有调用；待办：继续验证。",
            "l2": "完整细节：来源是会话摘要，模型输出通过结构和内容门控。",
        },
        ensure_ascii=False,
    )
    _LazyCliEmbedding.instances = []
    monkeypatch.setattr(
        "evolvmem.project_cli._load_rollup_llm", lambda: lambda _prompt: response
    )
    monkeypatch.setattr(
        "evolvmem.project_cli.EmbeddingEngine", _LazyCliEmbedding, raising=False
    )

    assert _run(test_config, "rollup", "run", "--project", "eva") == 0

    assert _out_json(capsys)["status"] == "ready"
    assert len(_LazyCliEmbedding.instances) == 1
    assert _LazyCliEmbedding.instances[0].initialize_calls == 1
    fresh = ContextService(test_config)
    fresh.initialize(mode=ContextMode.PRIMARY, adapter="codex")
    fresh.vector_index.initialize(dim=test_config.embedding_dim)
    fresh._refresh_health()
    status = fresh.status()
    assert status.ready is True
    assert status.diagnostics == ()
    fresh.close()


def test_rollup_run_reports_vector_dirty_when_embedding_cannot_load(
    test_config, store, capsys, monkeypatch
):
    source_id = _make_session_summary(store, "eva", "a")
    _seed_context_vector(test_config, source_id)
    response = json.dumps(
        {
            "l0": "项目正在完成滚动摘要接线。",
            "l1": "进展：接入已有模型；决定：复用现有调用；待办：继续验证。",
            "l2": "完整细节：来源是会话摘要，模型输出通过结构和内容门控。",
        },
        ensure_ascii=False,
    )

    class UnavailableEmbedding(_LazyCliEmbedding):
        def initialize(self):
            self.initialize_calls += 1
            raise RuntimeError("synthetic model unavailable")

    monkeypatch.setattr(
        "evolvmem.project_cli._load_rollup_llm", lambda: lambda _prompt: response
    )
    monkeypatch.setattr(
        "evolvmem.project_cli.EmbeddingEngine", UnavailableEmbedding, raising=False
    )

    assert _run(test_config, "rollup", "run", "--project", "eva") == 0

    assert _out_json(capsys)["status"] == "vector_dirty"
    assert test_config.context_vector_path.with_suffix(".usearch.dirty").exists()


def test_rollup_run_output_hygiene(test_config, store, capsys, monkeypatch):
    monkeypatch.setattr("evolvmem.project_cli._load_rollup_llm", lambda: None)
    _make_session_summary(store, "eva", "a")
    assert _run(test_config, "rollup", "run") == 0
    captured = capsys.readouterr()
    assert str(test_config.data_dir) not in captured.out
    assert captured.err == ""
    assert _run(test_config, "rollup", "run", "--project", "  ") == 2
    err = _err_json(capsys)
    assert err == {"error": "invalid_project"}
    assert str(test_config.data_dir) not in json.dumps(err)


# ---- workspace fingerprint helper commands ----


def test_fingerprint_and_bootstrap_key(test_config, store, temp_dir, capsys):
    # without a key, fingerprint fails closed with exit code 2 and a hint
    assert _run(test_config, "fingerprint", str(temp_dir)) == 2
    err = _err_json(capsys)
    assert err["error"] == "workspace_key_missing"
    assert "bootstrap-key" in err["hint"]
    assert str(temp_dir) not in json.dumps(err)
    # bootstrap creates the owner-only key; a second run is a no-op
    assert _run(test_config, "bootstrap-key") == 0
    assert _out_json(capsys) == {"state": "ready"}
    assert _run(test_config, "bootstrap-key") == 0
    assert _out_json(capsys) == {"state": "ready"}
    assert _run(test_config, "fingerprint", str(temp_dir)) == 0
    payload = _out_json(capsys)
    assert payload["fingerprint"].startswith("hmac-sha256:")
    assert payload["kind"] in ("git", "non_git")
    # the transient path never crosses the output boundary
    assert str(temp_dir) not in json.dumps(payload)


# ---- output hygiene ----


def test_explicit_data_dir_wins_over_env_override(
    test_config, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("EVOLVMEM_DATA_DIR", str(tmp_path))
    assert _run(test_config, "projects", "register", "eva") == 0
    assert _run(test_config, "projects", "list") == 0
    assert [row["project"] for row in _out_json(capsys)["projects"]] == ["eva"]
    # the env-override directory was never touched, and the override is restored
    assert not (tmp_path / "memory.db").exists()
    assert os.environ["EVOLVMEM_DATA_DIR"] == str(tmp_path)


def test_outputs_never_leak_paths_or_content(test_config, store, capsys):
    assert (
        _run(test_config, "projects", "archive", "ghost", "--expected-revision", "1")
        == 2
    )
    captured = capsys.readouterr()
    assert str(test_config.data_dir) not in captured.out
    assert str(test_config.data_dir) not in captured.err
    assert json.loads(captured.err) == {"error": "project_not_found"}
