"""Organization worker switches old and new write paths explicitly."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from evolvmem.auto_organization import OrganizationWorker


def run_mode(mode):
    worker = OrganizationWorker.__new__(OrganizationWorker)
    service = SimpleNamespace(config=SimpleNamespace(knowledge_structure_mode=mode))
    task = {"id": 7, "stage": "assignment", "source_key": "archive:7"}
    worker._assign = Mock()
    worker._finish = Mock()
    worker._is_current = Mock(return_value=True)
    with patch("evolvmem.auto_organization.settle_ineligible_source", return_value=False), \
         patch("evolvmem.capability_pipeline.CapabilityPipeline.process_task", return_value={}) as capability, \
         patch("evolvmem.project_archive_pipeline.ProjectArchivePipeline.process_task", return_value={}) as archive:
        worker._process(service, task)
    return worker, capability, archive


def test_legacy_mode_keeps_old_write_path_only():
    worker, capability, archive = run_mode("legacy")
    worker._assign.assert_called_once()
    capability.assert_not_called()
    archive.assert_not_called()


def test_dual_mode_writes_new_and_old_paths():
    worker, capability, archive = run_mode("dual")
    capability.assert_called_once()
    archive.assert_called_once()
    worker._assign.assert_called_once()


def test_primary_mode_finishes_without_old_project_assignment():
    worker, capability, archive = run_mode("primary")
    capability.assert_called_once()
    archive.assert_called_once()
    worker._assign.assert_not_called()
    worker._finish.assert_called_once()
