"""HTTP health checks for DataOps Guardian."""

import argparse
import json
import logging
import math
import os
import ssl
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger("dataguardian.health")


def _sanitize_url(url: str) -> str:
    """Remove credentials, query parameters, and fragments from a URL."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname

        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            return "<invalid URL>"

        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"

        port = parsed.port
        netloc = f"{hostname}:{port}" if port is not None else hostname

        return parsed._replace(netloc=netloc, query="", fragment="").geturl()
    except (ValueError, AttributeError):
        return "<invalid URL>"


def check_health(
    url: str,
    timeout: float = 3.0,
    retries: int = 0,
    retry_backoff: float = 0.5,
) -> dict:
    """Check an HTTP endpoint, optionally retrying transient failures."""
    safe_target = _sanitize_url(url)
    result = {
        "service": "dataguardian",
        "target": safe_target,
        "status": "unhealthy",
        "http_status": None,
        "duration_ms": 0,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "error_type": None,
        "error": None,
    }

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        result["error_type"] = "configuration_error"
        result["error"] = "Invalid timeout"
        return result

    if not math.isfinite(timeout) or timeout <= 0 or timeout > 60:
        result["error_type"] = "configuration_error"
        result["error"] = "Timeout must be greater than 0 and no more than 60 seconds"
        return result

    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        result["error_type"] = "configuration_error"
        result["error"] = "Retries must be a non-negative integer"
        return result

    if (
        isinstance(retry_backoff, bool)
        or not isinstance(retry_backoff, (int, float))
        or not math.isfinite(retry_backoff)
        or retry_backoff < 0
        or retry_backoff > 60
    ):
        result["error_type"] = "configuration_error"
        result["error"] = "Retry backoff must be between 0 and 60 seconds"
        return result

    started = time.monotonic()
    attempt = 0

    while True:
        try:
            request = Request(
                url,
                headers={"User-Agent": "DataOps-Guardian/1.0"},
            )

            with urlopen(request, timeout=float(timeout)) as response:
                result["http_status"] = response.status

                if 200 <= response.status < 300:
                    result["status"] = "healthy"
                    result["error_type"] = None
                    result["error"] = None
                else:
                    result["error_type"] = "http_error"
                    result["error"] = f"Unexpected HTTP status: {response.status}"

            break

        except HTTPError as exc:
            result["http_status"] = exc.code
            result["error_type"] = "http_error"
            result["error"] = f"HTTP {exc.code}: {exc.reason}"
            break

        except URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                result["error_type"] = "tls_error"
                result["error"] = "TLS verification or handshake failed"
                break

            if isinstance(exc.reason, TimeoutError):
                result["error_type"] = "timeout"
                result["error"] = "Request timed out"
            else:
                result["error_type"] = "connection_error"
                result["error"] = "Could not connect to service"

        except TimeoutError:
            result["error_type"] = "timeout"
            result["error"] = "Request timed out"

        except (OSError, ValueError) as exc:
            result["error_type"] = "request_error"
            result["error"] = (
                "Network request failed"
                if isinstance(exc, OSError)
                else "Invalid request URL"
            )
            if isinstance(exc, ValueError):
                break

        if attempt >= retries:
            break

        attempt += 1
        delay = float(retry_backoff) * (2 ** (attempt - 1))

        logger.warning(
            "Health check attempt failed; retrying: target=%s "
            "attempt=%s max_attempts=%s error_type=%s",
            safe_target,
            attempt,
            retries + 1,
            result["error_type"],
        )
        time.sleep(delay)

    result["duration_ms"] = round((time.monotonic() - started) * 1000, 2)

    if result["status"] != "healthy":
        logger.warning(
            "Health check failed: target=%s error_type=%s",
            safe_target,
            result["error_type"],
        )
    else:
        logger.info(
            "Health check passed: target=%s duration_ms=%s",
            safe_target,
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
    parser.add_argument(
        "--retries",
        type=int,
        default=int(os.getenv("HEALTH_RETRIES", "0")),
        help="Number of retries after transient failures (default: 0).",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=0.5,
        help="Initial retry delay in seconds; doubles after each retry.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if not args.url or _sanitize_url(args.url) == "<invalid URL>":
        parser.error("Provide a valid HTTP(S) URL using --url or TARGET_HEALTH_URL.")

    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 60:
        parser.error("--timeout must be greater than 0 and no more than 60.")

    if args.retries < 0:
        parser.error("--retries must be non-negative.")

    if not math.isfinite(args.retry_backoff) or not 0 <= args.retry_backoff <= 60:
        parser.error("--retry-backoff must be between 0 and 60.")

    result = check_health(
        args.url,
        timeout=args.timeout,
        retries=args.retries,
        retry_backoff=args.retry_backoff,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
