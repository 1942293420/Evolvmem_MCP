"""Public synthetic workflow acceptance; real semantic evaluation is opt-in."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest


class DeterministicEmbedding:
    """A reproducible vector test double, with no claim of semantic quality."""

    is_loaded = True

    def encode_document(self, text):
        digest = hashlib.sha256(text.encode()).digest()
        return [(digest[index % len(digest)] - 127.5) / 127.5 for index in range(768)]

    encode_query = encode_document

    def close(self):
        self.is_loaded = False


FIXTURE = Path(__file__).parent / "fixtures" / "experience_acceptance.json"


def test_load_fixture_keeps_frozen_scenario_contract():
    from scripts.experience_acceptance import load_fixture

    fixture = load_fixture(FIXTURE)

    assert fixture["source_policy"]["kind"] == "synthetic"
    assert {seed["case"]["project"] for seed in fixture["seed_cases"]} == {
        "demo-memory", "demo-support", "demo-runner",
    }
    assert all(seed["synthetic"] is True for seed in fixture["seed_cases"])
    assert not any("source_ref" in proof or "quote" in proof
                   for seed in fixture["seed_cases"] for proof in seed["evidence"])
    scenarios = fixture["acceptance_scenarios"]
    assert len(scenarios) == 30
    assert {scenario["id"] for scenario in scenarios} == {
        *(f"I{i:02d}" for i in range(1, 11)),
        *(f"T{i:02d}" for i in range(1, 7)),
        *(f"N{i:02d}" for i in range(1, 7)),
        *(f"F{i:02d}" for i in range(1, 5)),
        *(f"C{i:02d}" for i in range(1, 5)),
    }


def test_synthetic_suite_uses_sqlite_sources_feedback_and_continuity(
        tmp_path):
    from scripts.experience_acceptance import load_fixture, run_acceptance

    report = run_acceptance(FIXTURE, data_dir=tmp_path / "library",
                            embedding_engine=DeterministicEmbedding())

    assert report["schema_version"] == "experience-acceptance-report-v1"
    assert report["runtime"]["embedding"] == "injected_test_double"
    assert report["runtime"]["semantic_model_verified"] is False
    assert report["runtime"]["sources"] == "synthetic"
    assert report["overall"] == "not_evaluated"
    assert report["workflow_status"] == "pass"
    assert report["runtime"]["sqlite"] is True
    assert report["baseline"] == {
        "status": "not_run", "reason": "no_external_baseline_requested",
    }
    assert report["seed_statuses"] == {
        "demo_memory_projection_reconcile_and_vector_rebuild": "active",
        "demo_support_claim_attachment_channel_dispatch": "active",
        "demo_runner_session_terminated_retry_budget": "active",
        "demo_support_refresh_context_race_investigation_only": "candidate",
    }
    assert [case["id"] for case in report["cases"]] == [
        scenario["id"]
        for scenario in load_fixture(FIXTURE)["acceptance_scenarios"]
    ]
    assert len(report["cases"]) == 30
    assert all(set(case) == {"id", "pass", "reason", "time_ms"}
               for case in report["cases"])
    assert all(case["pass"] for case in report["cases"]
               if case["id"].startswith(("F", "C")))
    assert report["goals"]["implicit_required"] == 8
    assert report["goals"]["implicit_total"] == 10


def test_synthetic_seed_evidence_is_local_and_rejects_an_unbound_quote(tmp_path, monkeypatch):
    from scripts.experience_acceptance import seed_library

    unrelated = tmp_path / "unrelated-environment-data"
    monkeypatch.setenv("EVOLVMEM_DATA_DIR", str(unrelated))
    library = seed_library(tmp_path / "library", embedding_engine=DeterministicEmbedding())
    try:
        assert library.config.data_dir == (tmp_path / "library").resolve()
        assert not unrelated.exists()
        for item_id in library.labels.values():
            item = library.experiences.read(item_id)
            assert item["sources"]
            for source in item["sources"]:
                assert source["source_kind"] == "tool_result"
                path, _, line = source["source_ref"].rpartition("#")
                assert Path(path).is_relative_to(library.data_dir)
                event = json.loads(Path(path).read_text().splitlines()[int(line) - 1])
                assert "synthetic" in event["payload"]["output"]
            assert all("synthetic" in proof["note"] for proof in item["evidence"])
        item = library.experiences.read(next(iter(library.labels.values())))
        first = item["evidence"][0]
        with pytest.raises(ValueError, match="quote"):
            library.experiences.outcome(item["id"], {
                "outcome": "success", "task_id": first["task_id"],
                "event_id": "unbound-observation", "level": "technical",
                "conditions": item["conditions"], "source_kind": "tool_result",
                "source_ref": item["sources"][0]["source_ref"],
                "quote": "an observation absent from the synthetic transcript",
                "note": "[synthetic test fixture] Reject an unbound observation.",
            })
        assert library.experiences.read(item["id"])["success_count"] == 1
    finally:
        library.close()


def test_real_local_semantic_suite_requires_explicit_opt_in_and_model(tmp_path):
    if os.environ.get("EVOLVMEM_RUN_MODEL_TESTS") != "1":
        pytest.skip("set EVOLVMEM_RUN_MODEL_TESTS=1 and EVOLVMEM_TEST_MODEL to opt in")
    if importlib.util.find_spec("llama_cpp") is None:
        pytest.skip("optional llama-cpp-python backend is not installed")
    model = Path(os.environ.get("EVOLVMEM_TEST_MODEL", "")).expanduser()
    if not model.is_file():
        pytest.skip("EVOLVMEM_TEST_MODEL must point to an existing Nomic GGUF model")
    from scripts.experience_acceptance import run_acceptance

    report = run_acceptance(FIXTURE, data_dir=tmp_path / "real-model-library",
                            model_path=model)
    assert report["runtime"]["semantic_model_verified"] is True
    assert report["overall"] == "pass", report["goals"]


def test_codex_cutover_experience_suite_delegates_without_client_run(
        monkeypatch, capsys):
    import scripts.accept_codex_cutover as codex_acceptance
    import scripts.experience_acceptance as experience_acceptance

    calls = []
    monkeypatch.setattr(
        experience_acceptance, "main",
        lambda argv=None: calls.append(argv) or 7,
    )

    result = codex_acceptance.main(["--experience-suite", "--json"])

    assert result == 7
    assert calls == [["--json"]]
    assert capsys.readouterr().out == ""
