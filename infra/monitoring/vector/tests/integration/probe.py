#!/usr/bin/env python3
"""Run only inside the confined disposable integration image.

Use actual official Vector/Loki processes and an invented OpenObserve HTTP
protocol stub. OpenObserve authentication and partial acceptance,
production disk buffers, real capture and company deployment stay untested.
All data is synthetic and all endpoints are loopback in a network-none test.
"""

import base64
import collections
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG = Path("/opt/vector")
LOKI_CONFIG = Path("/opt/loki.yaml")
BODY_LIMIT = 1024 * 1024
PROCESS_SECONDS = 240


class TestFailure(ValueError):
    """Return fixed codes without process bodies or synthetic originals."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep synthetic requests on the fixed loopback origin."""

    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise TestFailure("UNEXPECTED_REDIRECT")


def request(path, port):
    """Bound local HTTP responses without proxy or redirect behavior."""
    if port not in {3100, 9598} or not path.startswith("/"):
        raise TestFailure("UNAPPROVED_TEST_ENDPOINT")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect()
    )
    try:
        with opener.open(
            f"http://127.0.0.1:{port}" + path, timeout=2
        ) as reply:
            body = reply.read(BODY_LIMIT + 1)
            if len(body) > BODY_LIMIT:
                raise TestFailure("TEST_RESPONSE_LIMIT")
            return body
    except (OSError, urllib.error.URLError):
        raise TestFailure("TEST_ENDPOINT_UNAVAILABLE") from None


class Receiver:
    """Hold bounded protocol-stub records; no external OpenObserve is used."""

    def __init__(self):
        self.available = True
        self.records = []
        self.rejected = 0
        self.lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            """Accept the selected sink's JSON-array request format only."""

            def log_message(self, format, *arguments):
                return

            def do_POST(self):
                try:
                    count = int(self.headers.get("Content-Length", "0"))
                    if not 0 < count <= BODY_LIMIT:
                        raise TestFailure("STUB_BODY_LIMIT")
                    body = self.rfile.read(count)
                    if self.headers.get("Content-Encoding") == "gzip":
                        with gzip.GzipFile(fileobj=io.BytesIO(body)) as source:
                            body = source.read(BODY_LIMIT + 1)
                    if len(body) > BODY_LIMIT:
                        raise TestFailure("STUB_BODY_LIMIT")
                    authorization = (
                        "Basic "
                        + base64.b64encode(
                            b"collector:synthetic-only"
                        ).decode()
                    )
                    if (
                        self.path != "/api/default/lakehouse_logs/_json"
                        or self.headers.get("Authorization") != authorization
                        or self.headers.get("Content-Type")
                        != "application/json"
                    ):
                        raise TestFailure("STUB_PROTOCOL")
                    rows = json.loads(body)
                    if not isinstance(rows, list) or len(rows) > 256:
                        raise TestFailure("STUB_ARRAY_REQUIRED")
                    with receiver.lock:
                        if not receiver.available:
                            receiver.rejected += len(rows)
                            self.send_response(503)
                            self.end_headers()
                            return
                        if len(receiver.records) + len(rows) > 1000:
                            raise TestFailure("STUB_RECORD_LIMIT")
                        receiver.records.extend(rows)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        b'{"code":200,"status":[{"successful":1}]}'
                    )
                except (ValueError, OSError):
                    self.send_response(400)
                    self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 5080), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def snapshot(self):
        """Copy accepted events without exposing raw bodies in reports."""
        with self.lock:
            return list(self.records)

    def close(self):
        """Stop the stub and release only its loopback socket."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise TestFailure("STUB_CLEANUP_FAILED")


def variants(directory):
    """Use public transforms with explicit small-memory test-buffer variants.

    These eight-event buffers exercise bounded fan-out topology and drops;
    they cannot validate production disk persistence/capacity/saturation.
    """
    target = directory / "config"
    shutil.copytree(CONFIG / "transforms", target / "transforms")
    for name in ("common.yaml", "loki.yaml", "openobserve.yaml"):
        text = (CONFIG / name).read_text()
        text = text.replace("/var/lib/vector", str(directory / "state"))
        text = text.replace(
            "/var/lib/lakehouse-log-spool", str(directory / "spool")
        )
        text = text.replace(
            "/run/secrets/openobserve-vector.json", str(directory / "secret")
        )
        text = text.replace("http://loki:3100", "http://127.0.0.1:3100")
        text = text.replace("http://openobserve:5080", "http://127.0.0.1:5080")
        text = text.replace("0.0.0.0:9598", "127.0.0.1:9598")
        if name != "common.yaml":
            original = "type: disk\n      max_size: 536870912"
            if text.count(original) != 1:
                raise TestFailure("TEST_VARIANT_CONTRACT_CHANGED")
            text = text.replace(original, "type: memory\n      max_events: 8")
            text = text.replace("max_events: 256", "max_events: 1")
            text = text.replace(
                "retry_max_duration_secs: 30", "retry_max_duration_secs: 1"
            )
        (target / name).write_text(text)
    (directory / "secret").write_text('{"token":"synthetic-only"}')
    (directory / "secret").chmod(0o600)
    for name in ("state", "spool", "loki"):
        (directory / name).mkdir(mode=0o700)
    loki = LOKI_CONFIG.read_text()
    loki = loki.replace("/loki", str(directory / "loki"))
    loki = loki.replace(
        "server:\n",
        "server:\n  http_listen_address: 127.0.0.1\n"
        "  grpc_listen_address: 127.0.0.1\n",
    )
    (directory / "loki.yaml").write_text(loki)
    return target


def terminate(process, *, abrupt=False):
    """Bound child cleanup and fail if a supposedly stopped child survives."""
    if process is None or process.poll() is not None:
        return
    if abrupt:
        process.kill()
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
        if not abrupt:
            raise TestFailure("PROCESS_GRACEFUL_CLEANUP_TIMEOUT") from None


def ids(records):
    """Validate deterministic source IDs and shared final severity fields."""
    result = set()
    for row in records:
        identifier = row.get("source_event_id", "")
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise TestFailure("EVENT_ID_INVALID")
        if row.get("level") != row.get("level_normalized"):
            raise TestFailure("LEVEL_FIELDS_DISAGREE")
        if "unit-redact-marker" in json.dumps(row):
            raise TestFailure("SYNTHETIC_SECRET_LEAK")
        result.add(identifier)
    return result


def has_phase(records, phase):
    """Require a newly emitted phase, not unrelated pending-buffer progress."""
    ids(records)
    return any(
        row.get("message", "").startswith("synthetic " + phase + " event ")
        for row in records
    )


def publish(directory, rows):
    """Expose only complete synthetic files to the tested file source."""
    payload = b"".join((json.dumps(row) + "\n").encode() for row in rows)
    destination = directory / (hashlib.sha256(payload).hexdigest() + ".jsonl")
    temporary = directory / (".pending-" + os.urandom(8).hex())
    with temporary.open("xb") as output:
        output.write(payload)
    temporary.chmod(0o600)
    os.link(temporary, destination)
    temporary.unlink()


class Trial:
    """Orchestrate only disposable child processes with a global deadline."""

    def __init__(self, directory):
        self.directory = directory
        self.config = variants(directory)
        self.deadline = time.monotonic() + PROCESS_SECONDS
        self.vector = None
        self.loki = None
        self.receiver = Receiver()
        self.sequence = 0
        self.stages = []

    def wait(self, condition, code, seconds=35):
        """Stop when a process dies or a bounded stage expires."""
        until = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < until:
            for process in (self.vector, self.loki):
                if process is not None and process.poll() is not None:
                    raise TestFailure("TEST_PROCESS_EXITED")
            try:
                outcome = condition()
                if outcome:
                    return outcome
            except (TestFailure, ValueError, KeyError):
                pass
            time.sleep(0.2)
        raise TestFailure(code)

    def start_loki(self):
        """Start official Loki against only temporary data."""
        self.loki = subprocess.Popen(
            [
                "/usr/bin/loki",
                "-config.file=" + str(self.directory / "loki.yaml"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath},
        )
        self.wait(lambda: request("/ready", 3100), "LOKI_NOT_READY", 50)

    def start_vector(self):
        """Start official Vector without environment secrets."""
        self.vector = subprocess.Popen(
            [
                "/usr/bin/vector",
                "--log-format",
                "json",
                "--config",
                "common.yaml",
                "--config",
                "loki.yaml",
                "--config",
                "openobserve.yaml",
            ],
            cwd=self.config,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath},
        )
        self.wait(lambda: request("/metrics", 9598), "VECTOR_NOT_READY")

    def emit(self, phase, count, *, sensitive=False):
        """Create immutable synthetic capture files; never touch host logs."""
        rows = []
        for index in range(count):
            self.sequence += 1
            stamp = datetime.now(timezone.utc).isoformat()
            message = f"synthetic {phase} event {self.sequence}"
            if sensitive and index == 0:
                message += " password=unit-redact-marker"
            body = json.dumps({"level": "INFO", "message": message})
            rows.append(
                {
                    "capture_version": 1,
                    "timestamp": stamp,
                    "compose_project": "synthetic",
                    "compose_service": "worker",
                    "host_alias": "lab",
                    "container_alias": "worker-1",
                    "source_identity": "a" * 64,
                    "source_position": hashlib.sha256(
                        (stamp + body).encode()
                    ).hexdigest(),
                    "stream": "stdout",
                    "message": body,
                    "truncated": False,
                }
            )
        publish(self.directory / "spool", rows)
        return rows

    def loki_records(self):
        """Query only the fixed synthetic label scope with a bounded result."""
        parameters = urllib.parse.urlencode(
            {
                "query": (
                    '{compose_project="synthetic",compose_service="worker"}'
                ),
                "start": str(time.time_ns() - 600_000_000_000),
                "end": str(time.time_ns()),
                "limit": "1000",
                "direction": "forward",
            }
        )
        result = json.loads(
            request("/loki/api/v1/query_range?" + parameters, 3100)
        )
        return [
            json.loads(value[1])
            for series in result["data"]["result"]
            for value in series["values"]
        ]

    def drop_evidence(self, sink):
        """Observe exported drop counters without inventing their names."""
        names = set()
        total = 0.0
        for line in request("/metrics", 9598).decode().splitlines():
            if line.startswith("#") or "discard" not in line:
                continue
            if f'component_id="{sink}"' not in line:
                continue
            found = re.fullmatch(
                r"([a-zA-Z_:][a-zA-Z0-9_:]*)\{[^\n]+\}\s+([0-9.eE+-]+)", line
            )
            if found and float(found[2]) > 0:
                names.add(found[1])
                total += float(found[2])
        return {"names": sorted(names), "count": total}

    def stage(self, name, **evidence):
        """Record sanitized per-stage evidence without raw records or logs."""
        self.stages.append({"name": name, "status": "passed", **evidence})

    def exercise(self):
        """Test finite delivery/outages/replay using real local processes."""
        self.start_loki()
        self.start_vector()
        original = self.emit("baseline", 3, sensitive=True)
        self.wait(
            lambda: (
                len(ids(self.receiver.snapshot())) == 3
                and len(ids(self.loki_records())) == 3
            ),
            "BASELINE_DELIVERY_FAILED",
        )
        baseline = self.receiver.snapshot()
        left = ids(self.loki_records())
        right = ids(baseline)
        if left != right or not any(
            row["message"] == "[REDACTED: sensitive log content]"
            for row in baseline
        ):
            raise TestFailure("BASELINE_REDACTION_OR_ID_MISMATCH")
        if any("_timestamp" not in row for row in baseline):
            raise TestFailure("OPENOBSERVE_TIMESTAMP_MISSING")
        self.stage("same_ids_redacted_both_sinks", unique_events=3)

        # Protocol-stub outage, not an actual OpenObserve durability test.
        self.receiver.available = False
        self.emit("openobserve_outage", 80)
        self.wait(
            lambda: has_phase(self.loki_records(), "openobserve_outage"),
            "LOKI_BLOCKED_BY_OPTIONAL_SINK",
        )
        self.wait(
            lambda: self.drop_evidence("openobserve")["count"] > 0,
            "OPTIONAL_SINK_DROP_EVIDENCE_MISSING",
        )
        self.stage(
            "openobserve_stub_outage_saturation",
            drops=self.drop_evidence("openobserve"),
        )
        self.receiver.available = True
        self.emit("openobserve_recovery", 3)
        self.wait(
            lambda: has_phase(
                self.receiver.snapshot(), "openobserve_recovery"
            ),
            "OPTIONAL_SINK_RECOVERY_FAILED",
        )
        self.stage("openobserve_stub_recovery")

        # Kill actual Loki, demonstrate the HTTP stub progresses, then restart
        # Loki using the same disposable filesystem and check prior data.
        terminate(self.loki, abrupt=True)
        self.loki = None
        self.emit("loki_outage", 80)
        self.wait(
            lambda: has_phase(self.receiver.snapshot(), "loki_outage"),
            "OPTIONAL_SINK_BLOCKED_BY_LOKI",
        )
        self.wait(
            lambda: self.drop_evidence("loki")["count"] > 0,
            "LOKI_DROP_EVIDENCE_MISSING",
        )
        self.stage("loki_outage_saturation", drops=self.drop_evidence("loki"))
        self.start_loki()
        self.wait(
            lambda: left <= ids(self.loki_records()),
            "LOKI_PRIOR_DATA_NOT_RECOVERED",
        )
        self.stage("loki_restart_prior_data_visible")

        # Drain, checkpoint, restart Vector, and emit a new immutable window.
        time.sleep(3)
        before_restart = collections.Counter(
            row["source_event_id"] for row in self.receiver.snapshot()
        )
        terminate(self.vector)
        self.vector = None
        self.start_vector()
        self.emit("vector_restart", 3)
        self.wait(
            lambda: (
                has_phase(self.receiver.snapshot(), "vector_restart")
                and has_phase(self.loki_records(), "vector_restart")
            ),
            "VECTOR_RESTART_NO_PROGRESS",
        )
        after_restart = collections.Counter(
            row["source_event_id"] for row in self.receiver.snapshot()
        )
        if any(after_restart[item] != before_restart[item] for item in left):
            raise TestFailure("CHECKPOINT_UNEXPECTED_BASELINE_REPLAY")
        self.stage("vector_checkpoint_restart_progress")

        # Overlapping capture windows can replay old logical events. IDs aid
        # reconciliation; backend duplicate behavior is deliberately reported.
        newer = self.emit("overlap_new", 1)
        combined = original + newer
        before_replay = collections.Counter(
            row["source_event_id"] for row in self.receiver.snapshot()
        )
        publish(self.directory / "spool", combined)
        self.wait(
            lambda: any(
                sum(
                    row["source_event_id"] == item
                    for row in self.receiver.snapshot()
                )
                > before_replay[item]
                for item in left
            ),
            "OVERLAP_REPLAY_NOT_OBSERVED",
        )
        accepted = self.receiver.snapshot()
        self.stage(
            "overlap_replay_identity_aid",
            accepted_records=len(accepted),
            unique_ids=len(ids(accepted)),
            exactly_once=False,
        )

    def close(self):
        """Attempt all cleanup even when an earlier child reports failure."""
        failures = []
        for process in (self.vector, self.loki):
            try:
                terminate(process)
            except (TestFailure, OSError, subprocess.SubprocessError):
                failures.append("process")
        self.receiver.close()
        if failures:
            raise TestFailure("TEST_CLEANUP_FAILED")


def main():
    """Print one sanitized JSON result; missing evidence is not a pass."""
    result = {
        "schema_version": 1,
        "status": "failed",
        "stages": [],
        "scope": "isolated_loopback_vector_loki_openobserve_http_stub",
        "buffer_variant": "eight_event_memory_not_production_disk",
        "production_disk_saturation": "not_run",
        "actual_openobserve": "not_run",
        "company_acceptance": "not_run",
        "cleanup": "not_run",
    }
    trial = None
    try:
        with tempfile.TemporaryDirectory(prefix="vector-isolated-") as scratch:
            trial = Trial(Path(scratch))
            try:
                trial.exercise()
                result["status"] = "passed"
            finally:
                result["stages"] = trial.stages
                trial.close()
                result["cleanup"] = "completed"
    except (
        TestFailure,
        ValueError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        result["status"] = "failed"
        result["code"] = "ISOLATED_TRIAL_FAILED_REVIEW_REQUIRED"
        if isinstance(error, TestFailure) and re.fullmatch(
            r"[A-Z0-9_]{1,80}", str(error)
        ):
            result["code"] = str(error)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
