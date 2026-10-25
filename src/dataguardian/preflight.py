
"""Pre-upgrade validation for DataOps Guardian."""

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dataguardian.elasticsearch import get_elasticsearch_version
from dataguardian.health import check_health
from dataguardian.version import check_version

logger = logging.getLogger("dataguardian.preflight")


def is_http_url(url: str | None) -> bool:
    """Return whether a URL is a valid HTTP(S) URL without embedded credentials."""
    if not isinstance(url, str) or not url.strip():
        return False

    try:
        parsed = urlparse(url.strip())

        return (
            parsed.scheme.lower() in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and parsed.port != 0
        )
    except (ValueError, AttributeError):
        return False


def run_preflight(
    health_url: str,
    timeout: float = 3.0,
    required_version: str | None = None,
    actual_version: str | None = None,
    elasticsearch_url: str | None = None,
) -> dict:
    """Validate configuration, service health, and version before maintenance."""

    checks = []

    def record(name: str, passed: bool, message: str) -> None:
        checks.append({
            "name": name,
            "status": "passed" if passed else "failed",
            "message": message,
        })

    # 1. Validate the health-check URL.
    valid_health_url = is_http_url(health_url)

    record(
        "health_url",
        valid_health_url,
        "Valid HTTP(S) URL"
        if valid_health_url
        else "A valid HTTP(S) URL without embedded credentials or fragments is required",
    )

    # 2. Validate the network timeout.
    valid_timeout = (
        isinstance(timeout, (int, float))
        and not isinstance(timeout, bool)
        and math.isfinite(timeout)
        and 0 < timeout <= 60
    )

    record(
        "timeout",
        valid_timeout,
        "Timeout is within the allowed range"
        if valid_timeout
        else "Timeout must be a finite number greater than 0 and no more than 60 seconds",
    )

    # 3. Validate version configuration.
    version_config_ok = (
        required_version is None
        or (
            isinstance(required_version, str)
            and bool(required_version.strip())
        )
    )

    record(
        "required_version",
        version_config_ok,
        "Version configuration is valid"
        if version_config_ok
        else "Required version must not be empty",
    )

    # 4. Discover or accept the actual version and compare it.
    if required_version is not None and version_config_ok:
        discovered_version = actual_version

        if elasticsearch_url is not None:
            if not is_http_url(elasticsearch_url):
                record(
                    "version_check",
                    False,
                    "A valid Elasticsearch HTTP(S) URL without embedded credentials or fragments is required",
                )
            elif not valid_timeout:
                record(
                    "version_check",
                    False,
                    "Version discovery skipped because timeout is invalid",
                )
            else:
                discovery = get_elasticsearch_version(
                    elasticsearch_url,
                    timeout=timeout,
                )

                if discovery.get("status") != "passed":
                    record(
                        "version_check",
                        False,
                        discovery.get(
                            "message",
                            "Elasticsearch version discovery failed",
                        ),
                    )
                else:
                    discovered_version = discovery.get("version")

                    version_result = check_version(
                        discovered_version,
                        required_version,
                    )

                    record(
                        "version_check",
                        version_result["status"] == "passed",
                        version_result["message"],
                    )

        elif discovered_version is None:
            record(
                "version_check",
                False,
                "Provide --elasticsearch-url or --actual-version",
            )
        else:
            version_result = check_version(
                discovered_version,
                required_version,
            )

            record(
                "version_check",
                version_result["status"] == "passed",
                version_result["message"],
            )

    # 5. Check service health only when configuration is valid.
    if valid_health_url and valid_timeout and version_config_ok:
        health = check_health(health_url, timeout)
        healthy = health.get("status") == "healthy"

        record(
            "service_health",
            healthy,
            "Target endpoint is healthy"
            if healthy
            else "Target endpoint is unhealthy",
        )
    else:
        record(
            "service_health",
            False,
            "Skipped because configuration validation failed",
        )

    # 6. Build a report without embedding credentials in target URLs.
    failed_count = sum(
        check["status"] == "failed"
        for check in checks
    )

    safe_target = health_url if is_http_url(health_url) else "<invalid URL>"

    return {
        "component": "dataguardian-preflight",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": safe_target,
        "status": "passed" if failed_count == 0 else "failed",
        "checks": checks,
        "check_count": len(checks),
        "failed_count": failed_count,
    }


def save_report(report: dict, output_dir: str = "reports") -> Path:
    """Save a timestamped JSON report."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    output_path = directory / f"preflight-{timestamp}.json"

    output_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )

    return output_path


def main() -> int:
    """Run preflight validation from the command line."""

    parser = argparse.ArgumentParser(
        description="Validate prerequisites before platform maintenance."
    )

    parser.add_argument(
        "--url",
        default=os.getenv("TARGET_HEALTH_URL"),
        help="Health-check URL or TARGET_HEALTH_URL environment variable.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="Network timeout in seconds (greater than 0, maximum 60).",
    )
    parser.add_argument(
        "--required-version",
        default=os.getenv("REQUIRED_VERSION"),
        help="Expected Elasticsearch version.",
    )
    parser.add_argument(
        "--actual-version",
        default=os.getenv("ACTUAL_VERSION"),
        help="Manually supplied version; automatic discovery is preferred.",
    )
    parser.add_argument(
        "--elasticsearch-url",
        default=os.getenv("ELASTICSEARCH_URL"),
        help="Elasticsearch base URL for automatic version discovery.",
    )
    parser.add_argument(
        "--report-dir",
        default="reports",
        help="Directory for JSON reports.",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if not args.url:
        parser.error("Provide --url or set TARGET_HEALTH_URL.")

    report = run_preflight(
        health_url=args.url,
        timeout=args.timeout,
        required_version=args.required_version,
        actual_version=args.actual_version,
        elasticsearch_url=args.elasticsearch_url,
    )

    print(json.dumps(report, indent=2))

    try:
        report_path = save_report(report, args.report_dir)
    except OSError:
        logger.error("Could not save preflight report.")
        return 2

    logger.info("Report saved to %s", report_path)
    logger.info(
        "Preflight completed: status=%s failed_count=%s",
        report["status"],
        report["failed_count"],
    )

    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
