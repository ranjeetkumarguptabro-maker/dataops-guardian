"""HTTP health checks for DataOps Guardian."""

import argparse
import json
import logging
import os
import ssl
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger("dataguardian.health")


def check_health(url: str, timeout: float = 3.0) -> dict:
    """Check an HTTP endpoint and return a structured health result."""
    result = {
        "service": "dataguardian",
        "target": url,
        "status": "unhealthy",
        "http_status": None,
        "duration_ms": 0,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "error_type": None,
        "error": None,
    }
    started = time.monotonic()

    try:
        request = Request(
            url,
            headers={"User-Agent": "DataOps-Guardian/1.0"},
        )

        with urlopen(request, timeout=timeout) as response:
            result["http_status"] = response.status
            if 200 <= response.status < 300:
                result["status"] = "healthy"
            else:
                result["error_type"] = "http_error"
                result["error"] = f"Unexpected HTTP status: {response.status}"

    except HTTPError as exc:
        result["http_status"] = exc.code
        result["error_type"] = "http_error"
        result["error"] = f"HTTP {exc.code}: {exc.reason}"

    except URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            result["error_type"] = "tls_error"
            result["error"] = "TLS certificate verification failed"
        elif isinstance(reason, (TimeoutError,)):
            result["error_type"] = "timeout"
            result["error"] = "Request timed out"
        else:
            result["error_type"] = "connection_error"
            result["error"] = str(reason)

    except TimeoutError:
        result["error_type"] = "timeout"
        result["error"] = "Request timed out"

    except (OSError, ValueError) as exc:
        result["error_type"] = "request_error"
        result["error"] = str(exc)

    result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)

    if result["status"] != "healthy":
        logger.warning(
            "Health check failed: target=%s error_type=%s",
            url,
            result["error_type"],
        )
    else:
        logger.info(
            "Health check passed: target=%s duration_ms=%s",
            url,
            result["duration_ms"],
        )

    return result


def main() -> int:
    """Parse arguments, run the check, and return a process exit code."""
    parser = argparse.ArgumentParser(description="Check an HTTP service's health.")
    parser.add_argument(
        "--url",
        default=os.getenv("TARGET_HEALTH_URL"),
        help="HTTP(S) health endpoint or TARGET_HEALTH_URL environment variable.",
    )
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    parsed = urlparse(args.url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        parser.error("Provide a valid HTTP(S) URL.")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero.")

    result = check_health(args.url, args.timeout)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
