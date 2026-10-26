"""Command-line interface for the maintenance workflow."""

import argparse
import json

from dataguardian.maintenance import run_maintenance_workflow


def main() -> int:
    """Parse arguments, run the workflow, and return an exit code."""
    parser = argparse.ArgumentParser(
        description="Run safety checks and plan Elasticsearch maintenance."
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Health-check URL, for example http://localhost:8080/health",
    )
    parser.add_argument(
        "--operation",
        required=True,
        choices=("rolling_restart", "index_cleanup"),
        help="Maintenance operation to plan",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Health-check timeout in seconds (default: 3)",
    )
    parser.add_argument("--required-version")
    parser.add_argument("--actual-version")
    parser.add_argument("--elasticsearch-url")
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Explicitly approve the maintenance plan",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Request execution; unavailable until an executor is configured",
    )
    parser.add_argument(
        "--report-dir",
        default="reports",
        help="Directory for the JSON workflow report",
    )

    args = parser.parse_args()

    if args.execute:
        parser.error(
            "Execution is disabled because no maintenance executor is configured. "
            "Remove --execute to run a safe dry run."
        )

    report = run_maintenance_workflow(
        health_url=args.url,
        operation=args.operation,
        timeout=args.timeout,
        required_version=args.required_version,
        actual_version=args.actual_version,
        elasticsearch_url=args.elasticsearch_url,
        approved=args.approve,
        dry_run=True,
        report_dir=args.report_dir,
    )

    print(json.dumps(report, indent=2))

    if report["status"] in {"failed", "blocked"}:
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
