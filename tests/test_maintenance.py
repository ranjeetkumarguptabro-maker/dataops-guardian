import unittest

from dataguardian.maintenance import plan_maintenance


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


if __name__ == "__main__":
    unittest.main()
