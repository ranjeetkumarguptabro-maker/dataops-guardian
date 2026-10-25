import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dataguardian.maintenance import (
    plan_maintenance,
    run_maintenance_workflow,
)


class TestMaintenance(unittest.TestCase):
    def test_dry_run_does_not_make_changes(self):
        result = plan_maintenance(
            {"status": "passed"},
            "rolling_restart",
        )

        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["changes_made"])

    def test_failed_preflight_blocks_maintenance(self):
        result = plan_maintenance(
            {"status": "failed"},
            "rolling_restart",
            approved=True,
            dry_run=False,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["changes_made"])

    def test_approval_required(self):
        result = plan_maintenance(
            {"status": "passed"},
            "rolling_restart",
            approved=False,
            dry_run=False,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["changes_made"])

    def test_unsupported_operation_is_blocked(self):
        result = plan_maintenance(
            {"status": "passed"},
            "delete_cluster",
            approved=True,
            dry_run=False,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["changes_made"])

    def test_approved_operation_is_not_executed_yet(self):
        result = plan_maintenance(
            {"status": "passed"},
            "rolling_restart",
            approved=True,
            dry_run=False,
        )

        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["changes_made"])

    @patch("dataguardian.maintenance.run_preflight")
    def test_failed_preflight_blocks_workflow(self, mock_preflight):
        mock_preflight.return_value = {
            "status": "failed",
            "target": "https://example.com/health",
            "failed_count": 1,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_maintenance_workflow(
                health_url="https://example.com/health",
                operation="rolling_restart",
                approved=True,
                dry_run=False,
                report_dir=temp_dir,
            )

            report_path = Path(result["report_path"])
            saved_report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["maintenance"]["status"], "blocked")
        self.assertEqual(result["execution"]["status"], "skipped")
        self.assertEqual(saved_report["status"], "blocked")
        mock_preflight.assert_called_once()

    @patch("dataguardian.maintenance.run_preflight")
    def test_dry_run_skips_execution(self, mock_preflight):
        mock_preflight.return_value = {
            "status": "passed",
            "target": "https://example.com/health",
            "failed_count": 0,
        }

        def forbidden_executor(_operation):
            raise AssertionError("Executor must not run during a dry run")

        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_maintenance_workflow(
                health_url="https://example.com/health",
                operation="rolling_restart",
                dry_run=True,
                executor=forbidden_executor,
                report_dir=temp_dir,
            )

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["execution"]["status"], "skipped")
        self.assertEqual(result["verification"]["status"], "skipped")

    @patch("dataguardian.maintenance.check_health")
    @patch("dataguardian.maintenance.run_preflight")
    def test_successful_executor_triggers_health_verification(
        self, mock_preflight, mock_health
    ):
        mock_preflight.return_value = {
            "status": "passed",
            "target": "https://example.com/health",
            "failed_count": 0,
        }
        mock_health.return_value = {"status": "healthy"}

        def fake_executor(_operation):
            return {"status": "passed", "changes_made": True}

        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_maintenance_workflow(
                health_url="https://example.com/health",
                operation="rolling_restart",
                approved=True,
                dry_run=False,
                executor=fake_executor,
                report_dir=temp_dir,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["verification"]["status"], "passed")
        mock_health.assert_called_once()

    @patch("dataguardian.maintenance.check_health")
    @patch("dataguardian.maintenance.run_preflight")
    def test_unhealthy_service_after_execution_fails_workflow(
        self, mock_preflight, mock_health
    ):
        mock_preflight.return_value = {
            "status": "passed",
            "target": "https://example.com/health",
            "failed_count": 0,
        }
        mock_health.return_value = {"status": "unhealthy"}

        def fake_executor(_operation):
            return {"status": "passed", "changes_made": True}

        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_maintenance_workflow(
                health_url="https://example.com/health",
                operation="rolling_restart",
                approved=True,
                dry_run=False,
                executor=fake_executor,
                report_dir=temp_dir,
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["verification"]["status"], "failed")
        mock_health.assert_called_once()

    @patch("dataguardian.maintenance.run_preflight")
    def test_executor_failure_skips_verification(self, mock_preflight):
        mock_preflight.return_value = {
            "status": "passed",
            "target": "https://example.com/health",
            "failed_count": 0,
        }

        def failing_executor(_operation):
            return {"status": "failed", "changes_made": False}

        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_maintenance_workflow(
                health_url="https://example.com/health",
                operation="rolling_restart",
                approved=True,
                dry_run=False,
                executor=failing_executor,
                report_dir=temp_dir,
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["verification"]["status"], "skipped")

    @patch("dataguardian.maintenance.run_preflight")
    def test_approved_workflow_without_executor_stays_ready(self, mock_preflight):
        mock_preflight.return_value = {
            "status": "passed",
            "target": "https://example.com/health",
            "failed_count": 0,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_maintenance_workflow(
                health_url="https://example.com/health",
                operation="rolling_restart",
                approved=True,
                dry_run=False,
                report_dir=temp_dir,
            )

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["execution"]["status"], "skipped")
        self.assertEqual(result["verification"]["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
