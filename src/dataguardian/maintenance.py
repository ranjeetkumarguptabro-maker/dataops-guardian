"""Safe, approval-gated Elasticsearch maintenance planning."""

from datetime import datetime, timezone


def plan_maintenance(
    preflight_report: dict,
    operation: str,
    approved: bool = False,
    dry_run: bool = True,
) -> dict:
    """Plan maintenance and block execution when safety checks fail."""
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
        message = (
            "Approved for a future maintenance executor; no cluster changes were made"
        )

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
