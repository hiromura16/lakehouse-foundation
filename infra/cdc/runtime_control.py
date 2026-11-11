"""Serve a secret-free status cache and enqueue fixed CDC rebuild requests.

The HTTP thread never touches SQLite, Kafka or Iceberg. The consumer main
thread owns those handles and takes queued work between bounded poll batches.
HTTP 202 acknowledges only a process-local queue; clients poll the matching
request ID and never treat acceptance as completed materialization.
"""

import copy
import json
import re
import secrets
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ObjectPairs(list):
    """Distinguish JSON objects from arrays while retaining duplicate keys."""


def observed_at():
    """Format an observation time without disclosing source clock details."""
    return datetime.now(timezone.utc).isoformat()


class Control:
    """Exchange immutable snapshots and one queued request between threads."""

    def __init__(self, mode, token, tables):
        """Require internal authentication before opening a status listener."""
        if not isinstance(token, str) or not re.fullmatch(
            r"[A-Za-z0-9]{20,128}", token
        ):
            raise ValueError("Configure the internal worker authentication.")
        self.token = token
        self.lock = threading.RLock()
        self.status = {
            "mode": mode,
            "ready": False,
            "blocked": False,
            "busy": False,
            "observation_time": None,
            "last_success": None,
            "error_code": "CDC_STARTING",
            "current_rows": {name: None for name in tables},
            "partitions": [],
            "rebuild": None,
            "raw_events_appended_since_start": 0,
        }

    def snapshot(self):
        """Copy only cached metadata; no SQL or broker request occurs here."""
        with self.lock:
            return copy.deepcopy(self.status)

    def update(self, **values):
        """Publish a main-thread observation without accepting extra fields."""
        with self.lock:
            if not values.keys() <= self.status.keys():
                raise ValueError("Unknown CDC status cache field.")
            self.status.update(copy.deepcopy(values))
            self.status["observation_time"] = observed_at()

    def enqueue(self):
        """Queue one rebuild, rejecting conflicts and query blockers."""
        with self.lock:
            if self.status["mode"] != "fixed-demo":
                return 409, {"error_code": "CDC_REBUILD_NOT_SUPPORTED"}
            if self.status["blocked"]:
                return 409, {"error_code": "CDC_QUERY_REVIEW_REQUIRED"}
            prior = self.status["rebuild"]
            if prior and prior["status"] in {"queued", "running"}:
                return 409, {"error_code": "CDC_REBUILD_BUSY"}
            request_id = secrets.token_hex(16)
            self.status["rebuild"] = {
                "request_id": request_id,
                "status": "queued",
                "completed": False,
                "error_code": None,
                "raw_snapshot_id": None,
            }
            return 202, {
                "accepted": True,
                "request_id": request_id,
                "status": "queued",
                "completed": False,
            }

    def take_rebuild(self):
        """Let the main thread claim one queued request between batches."""
        with self.lock:
            request = self.status["rebuild"]
            if request is None or request["status"] != "queued":
                return None
            request["status"] = "running"
            self.status["busy"] = True
            self.status["ready"] = False
            return request["request_id"]

    def finish_rebuild(
        self, request_id, *, succeeded, snapshot_id=None, error_code=None
    ):
        """Publish a terminal result for the same accepted request ID."""
        with self.lock:
            request = self.status["rebuild"]
            if request is None or request["request_id"] != request_id:
                raise ValueError("CDC rebuild request identity changed.")
            request.update(
                status="succeeded" if succeeded else "failed",
                completed=succeeded,
                error_code=error_code,
                raw_snapshot_id=snapshot_id,
            )
            self.status["busy"] = False
            self.status["observation_time"] = observed_at()

    def dispatch(self, method, path, authorization, body=b""):
        """Validate a fixed internal request before it can change the queue."""
        if not self.authenticated(authorization):
            return 401, {"error_code": "AUTH_REQUIRED"}
        if method == "GET" and path == "/status":
            return 200, self.snapshot()
        if method != "POST" or path != "/rebuild":
            return 404, {"error_code": "NOT_FOUND"}
        if len(body) > 4096:
            return 400, {"error_code": "INVALID_REBUILD_REQUEST"}
        try:
            pairs = json.loads(body, object_pairs_hook=ObjectPairs)
            # A pair list rejects duplicate keys and strict bool avoids 1/true
            # equivalence. Nested/arbitrary configuration is never accepted.
            valid = (
                isinstance(pairs, ObjectPairs)
                and len(pairs) == 1
                and pairs[0][0] == "confirm"
                and pairs[0][1] is True
            )
        except (ValueError, TypeError, IndexError, UnicodeError):
            valid = False
        if not valid:
            return 400, {"error_code": "INVALID_REBUILD_REQUEST"}
        return self.enqueue()

    def authenticated(self, authorization):
        """Compare Bearer auth safely, including invalid Unicode input."""
        if not isinstance(authorization, str):
            return False
        try:
            return secrets.compare_digest(
                authorization, "Bearer " + self.token
            )
        except TypeError:
            return False


def start_server(control, *, host="0.0.0.0", port=8001):
    """Start the internal container listener without publishing a host port."""

    class Handler(BaseHTTPRequestHandler):
        """Adapt HTTP transport to the pure, testable control dispatcher."""

        def setup(self):
            """Bound reads and retain standard HTTP header parser limits."""
            self.request.settimeout(5)
            super().setup()

        def log_message(self, *_):
            """Never log peer addresses, authentication or request bodies."""
            pass

        def handle_request(self):
            """Bound input and expose only application-owned JSON output."""
            try:
                authorization = self.headers.get("Authorization", "")
                if not control.authenticated(authorization):
                    status, payload = 401, {"error_code": "AUTH_REQUIRED"}
                else:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= 4096:
                        raise ValueError("Invalid body length.")
                    status, payload = control.dispatch(
                        self.command,
                        self.path,
                        authorization,
                        self.rfile.read(length),
                    )
            except Exception:
                status, payload = (
                    400,
                    {"error_code": "INVALID_REBUILD_REQUEST"},
                )
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = handle_request
        do_POST = handle_request

    class Server(ThreadingHTTPServer):
        """Keep client handling bounded to daemon threads and silent errors."""

        daemon_threads = True

        def handle_error(self, *_):
            """Suppress transport tracebacks with private peer details."""
            pass

    server = Server((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
