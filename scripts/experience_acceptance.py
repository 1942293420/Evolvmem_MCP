#!/usr/bin/env python3
"""Evaluate public synthetic scenarios using isolated SQLite and an optional local model.

Generated evidence is explicitly synthetic test input, never a historical repair
claim. An injected embedding double exercises workflows only; semantic acceptance
requires a real local Nomic model and is reported separately.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import tempfile
import time

from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from evolvmem.context_service import ContextService
from evolvmem.continuity_models import (
    ContinuityCheckpointRequest,
    ContinuityResumeRequest,
)
from evolvmem.continuity_service import ContinuityService
from evolvmem.embedding import EmbeddingEngine
from evolvmem.experience_service import ExperienceService
from evolvmem.experience_sources import ExperienceSourceResolver
from evolvmem.project_store import ProjectStore


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "experience_acceptance.json"
SENTINEL = ".evolvmem-experience-acceptance.sentinel"


def load_fixture(path: str | Path = DEFAULT_FIXTURE) -> dict:
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    scenarios = fixture.get("acceptance_scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != 30:
        raise ValueError("fixture must contain exactly 30 scenarios")
    if len({item.get("id") for item in scenarios}) != 30:
        raise ValueError("scenario IDs must be unique")
    if fixture.get("source_policy", {}).get("kind") != "synthetic":
        raise ValueError("acceptance fixtures must be explicitly synthetic")
    return fixture


@dataclass
class SeededLibrary:
    data_dir: Path
    config: Config
    core: ContextService
    experiences: ExperienceService
    labels: dict[str, int]
    real_embedding: bool

    def close(self) -> None:
        self.core.close()


def _prepare_empty_library(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    entries = list(data_dir.iterdir())
    if entries and {entry.name for entry in entries} != {SENTINEL}:
        raise ValueError("acceptance data directory must be empty")
    (data_dir / SENTINEL).write_text("isolated-experience-acceptance\n",
                                     encoding="utf-8")


def seed_library(
    data_dir: str | Path,
    fixture: dict | None = None,
    *,
    model_path: str | Path | None = None,
    embedding_engine=None,
) -> SeededLibrary:
    """Seed synthetic verified/candidate examples through the real source resolver."""
    data_dir = Path(data_dir).resolve()
    _prepare_empty_library(data_dir)
    fixture = fixture or load_fixture()
    config = Config(data_dir=data_dir)
    # The explicitly isolated directory wins over an inherited runtime setting.
    config.data_dir = data_dir
    real_embedding = embedding_engine is None
    engine = embedding_engine
    if real_embedding:
        source_model = Path(model_path or Config().model_path).expanduser().resolve()
        if not source_model.is_file():
            raise FileNotFoundError("local Nomic model unavailable; pass --model-file")
        model_dir = data_dir / "models"
        model_dir.mkdir()
        (model_dir / config.embedding_model_filename).symlink_to(source_model)
        engine = EmbeddingEngine(config)
        engine.initialize()
    core = ContextService(config, embedding_engine=engine)
    core.initialize(mode=ContextMode.SHADOW, adapter="codex")
    service = ExperienceService(core, source_resolver=synthetic_source_resolver(data_dir))
    labels: dict[str, int] = {}
    try:
        for seed in fixture["seed_cases"]:
            evidence = materialize_seed_evidence(data_dir, seed)
            saved = service.record(
                seed["case"], evidence=evidence[0] if evidence else None)
            for proof in evidence[1:]:
                saved = service.outcome(saved["id"], proof)
            if saved["status"] != seed["expected_status"]:
                raise RuntimeError("seed status differs from frozen fixture")
            labels[seed["label"]] = saved["id"]
        return SeededLibrary(data_dir, config, core, service, labels, real_embedding)
    except Exception:
        core.close()
        raise


def synthetic_source_resolver(data_dir: Path) -> ExperienceSourceResolver:
    """Resolve only generated test events, with all personal adapter roots disabled."""
    return ExperienceSourceResolver(
        codex_roots=(data_dir / "synthetic-codex",), kimi_roots=(), dsh_roots=(),
        fix_root=data_dir / "unused-synthetic-records")


def materialize_seed_evidence(data_dir: Path, seed: dict) -> list[dict]:
    if seed.get("synthetic") is not True:
        raise ValueError("seed must be explicitly synthetic")
    root = data_dir / "synthetic-codex"
    root.mkdir(parents=True, exist_ok=True)
    evidence = []
    for index, template in enumerate(seed.get("evidence", []), 1):
        task = f"synthetic-seed-{seed['label']}-{index}"
        quote = f"[synthetic test fixture] {seed['label']}: {seed['case']['result']}"
        transcript = root / f"rollout-{task}.jsonl"
        transcript.write_text(json.dumps({
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": quote},
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        evidence.append({
            "source_kind": "tool_result", "source_ref": f"{transcript.resolve()}#1",
            "quote": quote, "task_id": task, "event_id": template["synthetic_event"],
            "outcome": template["outcome"], "level": template["level"],
            "conditions": template["conditions"],
            "note": "[synthetic test fixture] Source-binding test; no historical repair claim.",
        })
    return evidence


def _timed(case_id, operation):
    started = time.monotonic()
    try:
        passed, reason = operation()
    except Exception as error:
        passed, reason = False, f"error:{type(error).__name__}"
    return {"id": case_id, "pass": bool(passed), "reason": reason,
            "time_ms": int((time.monotonic() - started) * 1000)}


def _retrieval_case(library: SeededLibrary, scenario: dict):
    recalled = library.experiences.recall(
        project=scenario["project"], query=scenario["prompt"],
        constraints=scenario.get("constraints"))
    ids = {item["id"] for item in recalled["results"]}
    label = scenario.get("expected_case_label")
    expected_id = library.labels.get(label) if label else None
    category = scenario["category"]
    if category == "implicit_paraphrase":
        return (expected_id in ids,
                "expected_case_found" if expected_id in ids
                else "expected_case_missing")
    if category == "transfer":
        must_find = scenario["id"] in {"T02", "T06"}
        correct = (expected_id in ids) is must_find
        return correct, ("transfer_candidate_found" if must_find and correct
                         else "inapplicable_case_omitted" if correct
                         else "transfer_expectation_missed")
    forbidden = {
        "N02": "demo_support_refresh_context_race_investigation_only",
        "N03": "demo_memory_projection_reconcile_and_vector_rebuild",
        "N04": "demo_support_claim_attachment_channel_dispatch",
        "N05": "demo_runner_session_terminated_retry_budget",
        "N06": "demo_memory_projection_reconcile_and_vector_rebuild",
    }.get(scenario["id"])
    if scenario["id"] == "N01":
        return (not ids, "empty" if not ids else "unexpected_case")
    invalid = library.labels.get(forbidden) in ids
    return (not invalid, "no_invalid_adoption" if not invalid
            else "invalid_case_returned")


def _synthetic_proof(library, scenario, conditions):
    task = f"acceptance-{scenario['id']}"
    quote = f"synthetic acceptance verification {scenario['id']}: observable result"
    root = library.data_dir / "synthetic-codex"
    root.mkdir(exist_ok=True)
    transcript = root / f"rollout-{task}.jsonl"
    transcript.write_text(json.dumps({
        "type": "response_item",
        "payload": {"type": "function_call_output", "output": quote},
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    return {
        "task_id": task, "event_id": f"verification-{scenario['id']}",
        "outcome": scenario["feedback"]["outcome"], "level": "technical",
        "source_kind": "tool_result", "source_ref": f"{transcript}#1",
        "quote": quote,
        "note": f"[synthetic acceptance verification] {scenario['id']}",
        "conditions": conditions,
    }


def _feedback_case(library: SeededLibrary, scenario: dict):
    label = {
        "F01": "demo_runner_session_terminated_retry_budget",
        "F02": "demo_support_claim_attachment_channel_dispatch",
        "F03": "demo_runner_session_terminated_retry_budget",
        "F04": "demo_support_refresh_context_race_investigation_only",
    }[scenario["id"]]
    item_id = library.labels[label]
    before = library.experiences.read(item_id)
    if scenario["id"] == "F04":
        try:
            library.experiences.outcome(item_id, {
                "task_id": "acceptance-F04", "event_id": "unsupported",
                "outcome": "success", "conditions": before["conditions"],
                "note": "synthetic claim without verification",
            })
            return False, "unsupported_success_accepted"
        except ValueError:
            after = library.experiences.read(item_id)
            good = after["status"] == "candidate" and after["success_count"] == 0
            return good, "unsupported_success_rejected" if good else "candidate_changed"
    conditions = (scenario["feedback"].get("conditions")
                  if scenario["id"] == "F03" else before["conditions"])
    proof = _synthetic_proof(library, scenario, conditions)
    after = library.experiences.outcome(item_id, proof)
    if scenario["id"] == "F01":
        good = (after["validation_level"] == "repeated_verified"
                and after["success_count"] == before["success_count"] + 1)
    elif scenario["id"] == "F02":
        good = (after["status"] == "candidate"
                and after["failure_count"] == before["failure_count"] + 1)
    else:
        good = (after["status"] == "active"
                and after["failure_count"] == before["failure_count"])
    return good, "feedback_applied" if good else "feedback_expectation_missed"


def _git_workspace(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", str(path)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for key, value in (("user.email", "acceptance@example.invalid"),
                       ("user.name", "acceptance"), ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path


def _bind(library, provider, workspace, project):
    fingerprint = provider.resolve(str(workspace)).fingerprint
    with library.core.store.transaction():
        projects = ProjectStore(library.core.store._connection(),
                                library.core.store._require_transaction,
                                generic_names=())
        projects.register_project(project)
        projects.bind_workspace(fingerprint, project, method="test", make_default=True)
    return fingerprint


def _continuity_case(library: SeededLibrary, scenario: dict):
    provider = library.core._workspace_identity()
    provider.bootstrap_key()
    workspace = _git_workspace(library.data_dir / "workspaces", scenario["id"])
    fingerprint = _bind(library, provider, workspace, scenario["project"])
    service = ContinuityService(library.config, library.core.store, provider)

    def create(*, focus=False, objective=None):
        return service.checkpoint(ContinuityCheckpointRequest(
            action="create", workspace_path=str(workspace),
            objective=objective or scenario["prompt"], next_action="执行唯一下一步",
            make_focus=focus, expected_focus_revision=0 if focus else None))

    if scenario["id"] == "C01":
        made = create(focus=True)
        row = library.core.store._connection().execute(
            "SELECT status FROM continuity_workstreams WHERE id=?",
            (made.workstream_id,)).fetchone()
        good = made.status == "open" and row["status"] == "open"
        return good, "checkpoint_created" if good else "checkpoint_create_failed"
    if scenario["id"] == "C02":
        made = create(focus=True)
        service.checkpoint(ContinuityCheckpointRequest(
            action="pause", workspace_path=str(workspace),
            workstream_id=made.workstream_id,
            expected_checkpoint_revision=made.checkpoint_revision,
            expected_state_version=made.state_version))
        resumed = service.resume(ContinuityResumeRequest(
            workspace_path=str(workspace), project_hint=scenario["project"]))
        good = (resumed.code == "ok" and resumed.status == "paused"
                and resumed.staleness == "fresh" and resumed.checkpoint is not None)
        return good, "paused_checkpoint_resumed" if good else "resume_expectation_missed"
    if scenario["id"] == "C03":
        made = create(focus=True)
        library.core.store._connection().execute(
            "UPDATE continuity_workstreams SET workspace_fingerprint=? WHERE id=?",
            ("hmac-sha256:" + "f" * 64, made.workstream_id))
        library.core.store._connection().commit()
        resumed = service.resume(ContinuityResumeRequest(
            workspace_path=str(workspace), project_hint=scenario["project"]))
        good = resumed.staleness == "wrong_workspace" and resumed.checkpoint is None
        return good, "wrong_workspace_hidden" if good else "workspace_guard_failed"
    create(objective="采集稳定性甲")
    create(objective="采集稳定性乙")
    resumed = service.resume(ContinuityResumeRequest(
        workspace_path=str(workspace), project_hint=scenario["project"]))
    good = resumed.code == "ambiguous" and len(resumed.candidates) == 2
    return good, "ambiguous_candidates_returned" if good else "ambiguity_missed"


def run_acceptance(
    fixture_path: str | Path = DEFAULT_FIXTURE,
    *,
    data_dir: str | Path,
    model_path: str | Path | None = None,
    embedding_engine=None,
) -> dict:
    fixture = load_fixture(fixture_path)
    library = seed_library(data_dir, fixture, model_path=model_path,
                           embedding_engine=embedding_engine)
    try:
        seed_statuses = {
            label: library.experiences.read(item_id)["status"]
            for label, item_id in library.labels.items()
        }
        cases = []
        for scenario in fixture["acceptance_scenarios"]:
            category = scenario["category"]
            operation = (_retrieval_case if category in {
                "implicit_paraphrase", "transfer", "negative"
            } else _feedback_case if category == "feedback" else _continuity_case)
            cases.append(_timed(
                scenario["id"], lambda s=scenario, op=operation: op(library, s)))
        groups = {
            prefix: [case for case in cases if case["id"].startswith(prefix)]
            for prefix in "ITNFC"
        }
        goals = {
            "implicit_passed": sum(case["pass"] for case in groups["I"]),
            "implicit_required": 8, "implicit_total": 10,
            "transfer_passed": sum(case["pass"] for case in groups["T"]),
            "negative_passed": sum(case["pass"] for case in groups["N"]),
            "feedback_passed": sum(case["pass"] for case in groups["F"]),
            "continuity_passed": sum(case["pass"] for case in groups["C"]),
        }
        overall = (goals["implicit_passed"] >= 8
                   and all(case["pass"] for prefix in "TNFC"
                           for case in groups[prefix]))
        return {
            "schema_version": "experience-acceptance-report-v1",
            "overall": ("pass" if overall else "fail") if library.real_embedding
                       else "not_evaluated",
            "workflow_status": "pass" if all(case["pass"] for prefix in "TNFC"
                                             for case in groups[prefix]) else "fail",
            "runtime": {"sqlite": True,
                        "embedding": library.config.embedding_model_filename
                                     if library.real_embedding else "injected_test_double",
                        "semantic_model_verified": library.real_embedding and overall,
                        "sources": "synthetic"},
            "baseline": {"status": "not_run", "reason": "no_external_baseline_requested"},
            "seed_statuses": seed_statuses, "goals": goals, "cases": cases,
        }
    finally:
        library.close()


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--data-dir")
    parser.add_argument("--model-file")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.data_dir:
        report = run_acceptance(args.fixture, data_dir=args.data_dir,
                                model_path=args.model_file)
    else:
        with tempfile.TemporaryDirectory(prefix="evolvmem-experience-acceptance.") as root:
            report = run_acceptance(args.fixture, data_dir=Path(root) / "library",
                                    model_path=args.model_file)
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["overall"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
