"""Local-model acceptance harness over the frozen 30-scenario fixture."""

from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "experience_acceptance.json"


def test_load_fixture_keeps_frozen_scenario_contract():
    from scripts.experience_acceptance import load_fixture

    fixture = load_fixture(FIXTURE)

    scenarios = fixture["acceptance_scenarios"]
    assert len(scenarios) == 30
    assert {scenario["id"] for scenario in scenarios} == {
        *(f"I{i:02d}" for i in range(1, 11)),
        *(f"T{i:02d}" for i in range(1, 7)),
        *(f"N{i:02d}" for i in range(1, 7)),
        *(f"F{i:02d}" for i in range(1, 5)),
        *(f"C{i:02d}" for i in range(1, 5)),
    }


def test_real_local_suite_uses_sqlite_model_sources_feedback_and_continuity(
        tmp_path):
    from scripts.experience_acceptance import load_fixture, run_acceptance

    report = run_acceptance(FIXTURE, data_dir=tmp_path / "library")

    assert report["schema_version"] == "experience-acceptance-report-v1"
    assert report["runtime"]["embedding"] == "nomic-embed-text-v1.5.f16.gguf"
    assert report["runtime"]["sqlite"] is True
    assert report["baseline"] == {
        "status": "not_run", "reason": "delegated_to_root",
    }
    assert report["seed_statuses"] == {
        "evolvmem_projection_reconcile_and_vector_rebuild": "active",
        "eva_claim_attachment_channel_dispatch": "active",
        "bluewhale_session_terminated_retry_budget": "active",
        "eva_refresh_context_race_investigation_only": "candidate",
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
