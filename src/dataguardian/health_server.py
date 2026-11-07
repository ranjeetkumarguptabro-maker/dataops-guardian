"""HTTP health and readiness server for DataOps Guardian."""

import json
import logging
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import getenv

from dataguardian.elasticsearch import ElasticsearchError, fetch_json

logger = logging.getLogger("dataguardian.health_server")


class HealthHandler(BaseHTTPRequestHandler):
    """Handle HTTP health and readiness requests."""

    def _send_json(self, status_code: int, payload: dict) -> None:
        """Send a JSON response."""
        response = json.dumps(payload).encode("utf-8")

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self):
        """Handle health and readiness endpoints."""
        service = getenv("SERVICE_NAME", "dataops-guardian")

        if self.path == "/health":
            self._send_json(
                200,
                {"service": service, "status": "healthy"},
            )
            return

        if self.path == "/ready":
            elasticsearch_url = getenv("ELASTICSEARCH_URL")

            if not elasticsearch_url:
                self._send_json(
                    503,
                    {
                        "service": service,
                        "status": "not_ready",
                        "dependencies": {"elasticsearch": {"status": "not_configured"}},
                    },
                )
                return

            try:
                cluster = fetch_json(
                    elasticsearch_url,
                    "/_cluster/health",
                    timeout=3.0,
                )

                cluster_status = (
                    cluster.get("status") if isinstance(cluster, dict) else None
                )

                ready = cluster_status in {"green", "yellow"}

                dependency = {
                    "status": cluster_status or "unknown",
                    "error_type": (
                        None
                        if cluster_status in {"green", "yellow", "red"}
                        else "invalid_response"
                    ),
                }

                if cluster_status == "yellow":
                    dependency["warning"] = "Cluster has unassigned replicas"

            except ElasticsearchError:
                ready = False
                dependency = {
                    "status": "unavailable",
                    "error_type": "dependency_error",
                }

            self._send_json(
                200 if ready else 503,
                {
                    "service": service,
                    "status": "ready" if ready else "not_ready",
                    "dependencies": {
                        "elasticsearch": dependency,
                    },
                },
            )
            return

        self.send_error(404, "Not Found")

    def log_message(self, format_string, *args):
        """Log HTTP requests."""
        logger.info(
            "%s - %s",
            self.address_string(),
            format_string % args,
        )


def _handle_sigterm(_signum, _frame):
    """Stop serving cleanly when Kubernetes terminates the pod."""
    raise SystemExit(0)


def main():
    """Start the HTTP health server."""
    host = getenv("HEALTH_HOST", "0.0.0.0")
    port = int(getenv("PORT", "8080"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    signal.signal(signal.SIGTERM, _handle_sigterm)

    server = ThreadingHTTPServer((host, port), HealthHandler)
    logger.info("Health server listening on %s:%s", host, port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Health server shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
