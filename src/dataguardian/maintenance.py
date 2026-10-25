"""Safe, approval-gated Elasticsearch maintenance workflow."""

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from dataguardian.health import check_health
from dataguardian.preflight import run_preflight


def plan_maintenance(
    preflight_report: dict,
    operation: str,
    approved: bool = False,
    dry_run: bool = True,
) -> dict:
    """Plan maintenance and block unsafe or unapproved operations."""
    allowed_operations = {"rolling_restart", "index_cleanup"}

    if operation not in allowed_operations:
        status = "blocked"
        message = "Unsupported maintenance operation"
    elif preflight_report.get("status") != "passed":
        status = "blocked"
        message = "Preflight failed; maintenance is blocked"
    elif dry_run:
        status = "dry_run"
        message = "Dry run only; no changes were made"
    elif not approved:
        status = "blocked"
        message = "Explicit approval is required"
    else:
        status = "ready"
        message = "Approved; awaiting a maintenance executor"

    return {
        "component": "dataguardian-maintenance",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "status": status,
        "dry_run": dry_run,
        "approved": approved,
        "message": message,
        "changes_made": False,
    }


def run_maintenance_workflow(
    health_url: str,
    operation: str,
    timeout: float = 3.0,
    required_version: str | None = None,
    actual_version: str | None = None,
    elasticsearch_url: str | None = None,
    approved: bool = False,
    dry_run: bool = True,
    executor: Callable[[str], dict] | None = None,
    report_dir: str = "reports",
) -> dict:
    """Run preflight, enforce safety gates, and save a combined JSON report."""
    preflight = run_preflight(
        health_url=health_url,
        timeout=timeout,
        required_version=required_version,
        actual_version=actual_version,
        elasticsearch_url=elasticsearch_url,
    )

    plan = plan_maintenance(
        preflight_report=preflight,
        operation=operation,
        approved=approved,
        dry_run=dry_run,
    )

    execution = {
        "status": "skipped",
        "message": "No maintenance executor was run",
        "changes_made": False,
    }
    verification = {
        "status": "skipped",
        "message": "No completed maintenance operation to verify",
    }
    workflow_status = plan["status"]

    if plan["status"] == "ready" and executor is not None:
        try:
            execution = executor(operation)
            if not isinstance(execution, dict):
                raise TypeError("Executor must return a dictionary")
        except Exception:
            execution = {
                "status": "failed",
                "message": "Maintenance executor failed",
                "changes_made": False,
            }

        if (
            execution.get("status") == "passed"
            and execution.get("changes_made") is True
        ):
            try:
                health = check_health(health_url, timeout)
                healthy = health.get("status") == "healthy"
            except Exception:
                healthy = False

            verification = {
                "status": "passed" if healthy else "failed",
                "message": (
                    "Post-maintenance health check passed"
                    if healthy
                    else "Post-maintenance health check failed"
                ),
            }
            workflow_status = "completed" if healthy else "failed"
        else:
            workflow_status = "failed"
            verification = {
                "status": "skipped",
                "message": (
                    "Verification skipped because execution did not "
                    "confirm successful changes"
                ),
            }

    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report_path = directory / f"maintenance-{timestamp}.json"

    report = {
        "component": "dataguardian-maintenance-workflow",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": preflight.get("target", "<unknown>"),
        "status": workflow_status,
        "preflight": preflight,
        "maintenance": plan,
        "execution": execution,
        "verification": verification,
        "report_path": str(report_path),
    }

    report_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )

    return report
