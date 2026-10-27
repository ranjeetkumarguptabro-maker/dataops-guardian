
"""HTTP health server for DataOps Guardian."""

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os import getenv

logger = logging.getLogger("dataguardian.health_server")


class HealthHandler(BaseHTTPRequestHandler):
    """Handle HTTP health-check requests."""

    def do_GET(self):
        """Return the health status for the service."""
        if self.path != "/health":
            self.send_error(404, "Not Found")
            return

        response = json.dumps(
            {
                "service": getenv("SERVICE_NAME", "dataops-guardian"),
                "status": "healthy",
            }
        ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format_string, *args):
        """Log HTTP requests."""
        logger.info(
            "%s - %s",
            self.address_string(),
            format_string % args,
        )


def main():
    """Start the HTTP health server."""
    host = getenv("HEALTH_HOST", "0.0.0.0")
    port = int(getenv("PORT", "8080"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

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
