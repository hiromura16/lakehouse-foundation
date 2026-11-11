"""Keep Spark smoke results and fixed internal HTTP probes secret-free.

This module has no Spark imports and performs no I/O at import time. The host
CLI can use the same result contract when build or preflight fails before
Python starts in the one-shot container. Transport failures use fixed codes;
private response bodies, URLs, commands and exception text are never results.
"""

import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

RESULT_PREFIX = "LAKEHOUSE_SPARK_RESULT="
STAGES = (
    "master_reachability",
    "worker_registration",
    "session_creation",
    "dataframe_fixture",
    "rest_catalog",
    "s3_write",
    "dedicated_iceberg_table",
    "spark_readback",
    "trino_readback",
    "count_value_equality",
    "idempotent_replay",
    "cleanup",
)
STATUSES = {"not_run", "passed", "failed", "skipped"}
# Codes form a finite public vocabulary. Regex-shaped vendor messages or
# source identifiers must never become diagnostic values merely by casing.
DIAGNOSTIC_CODES = frozenset(
    {
        "CROSS_READ_MISMATCH",
        "DATAFRAME_FIXTURE_MISMATCH",
        "EXECUTION_MODE_MISMATCH",
        "FIXTURE_CONTRACT_CHANGED",
        "FIXTURE_RETAINED",
        "FIXTURE_ROW_LIMIT",
        "FIXTURE_TARGET_REJECTED",
        "FIXTURE_TYPE_MISMATCH",
        "HTTP_DEADLINE",
        "HTTP_ORIGIN_REJECTED",
        "HTTP_RESPONSE_REJECTED",
        "HTTP_RESPONSE_TOO_LARGE",
        "HTTP_TRANSPORT_FAILED",
        "ICEBERG_WRITE_COMPLETED",
        "INVALID_RESULT_METADATA",
        "LOCAL_ONLY_REQUESTED",
        "LOCK_MARKER_FAILED",
        "LOCK_RELEASE_FAILED",
        "MASTER_NOT_READY",
        "REPLAY_MISMATCH",
        "SESSION_START_UNCONFIRMED",
        "SESSION_STOP_FAILED",
        "SMOKE_ALREADY_RUNNING",
        "SMOKE_LOCK_UNAVAILABLE",
        "SMOKE_LOCK_UNSAFE",
        "SMOKE_OPERATION_FAILED",
        "SMOKE_PREVIOUS_SESSION_UNCONFIRMED",
        "SMOKE_REQUIRES_NONROOT",
        "SPARK_READBACK_MISMATCH",
        "TRINO_CROSS_READ_FAILED",
        "TRINO_ORIGIN_REJECTED",
        "TRINO_PAGE_LIMIT",
        "TRINO_QUERY_FAILED",
        "TRINO_QUERY_REJECTED",
        "TRINO_READBACK_MISMATCH",
        "TRINO_RESPONSE_INVALID",
        "TRINO_ROW_LIMIT",
        "WORKER_NOT_REGISTERED",
    }
)
MASTER_URL = "spark://spark-master:7077"
MASTER_JSON = "http://spark-master:8080/json/"
WORKER_JSON = "http://spark-worker:8081/json/"
TRINO_ENDPOINT = "http://trino:8080/v1/statement"
MAX_BODY = 64 * 1024
MAX_PAGES = 100


class SmokeFailure(ValueError):
    """Carry a fixed code, never a vendor exception or endpoint address."""

    def __init__(self, code, *, cancellation="not_run"):
        if not isinstance(code, str) or code not in DIAGNOSTIC_CODES:
            raise ValueError("Invalid smoke diagnostic code.")
        self.code = code
        if cancellation not in {
            "not_run",
            "succeeded",
            "failed",
            "unavailable",
        }:
            raise ValueError("Invalid cancellation outcome.")
        self.cancellation = cancellation
        super().__init__(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Reject redirection rather than forwarding the internal request."""

    def redirect_request(self, *_args, **_kwargs):
        return None


def utc_now():
    """Return a UTC observation without any source clock or machine name."""
    return datetime.now(timezone.utc).isoformat()


def _profiles(value):
    """Keep only a bounded comma-separated profile list supplied by the CLI."""
    if not value:
        return []
    values = value.split(",")
    if len(values) > 32 or any(
        not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", item) for item in values
    ):
        raise SmokeFailure("INVALID_RESULT_METADATA")
    return list(dict.fromkeys(values))


def new_result(values=None, *, local_only=False, run_id=None):
    """Create a versioned unknown/not-run result before any external work.

    Parent orchestration supplies commit/image evidence only after inspecting
    its selected checkout/image. Direct container invocation may leave either
    unknown; a tag or successful JVM startup is not an immutable image ID.
    """
    values = os.environ if values is None else values
    run_id = (
        run_id or values.get("SPARK_SMOKE_RUN_ID") or secrets.token_hex(16)
    )
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise SmokeFailure("INVALID_RESULT_METADATA")
    commit = values.get("SPARK_SMOKE_SOURCE_COMMIT", "unknown")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        commit = "unknown"
    dirty = values.get("SPARK_SMOKE_DIRTY_STATUS", "unknown")
    if dirty not in {"clean", "dirty", "unknown"}:
        dirty = "unknown"
    image = values.get("SPARK_SMOKE_IMAGE_ID", "unknown")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        image = "unknown"
    return {
        "schema_version": 1,
        "kind": "spark-compatibility",
        "run_id": run_id,
        "started_at": utc_now(),
        "finished_at": None,
        "elapsed_seconds": None,
        "source_commit": commit,
        "dirty_status": dirty,
        "configured_profiles": _profiles(
            values.get("SPARK_SMOKE_CONFIGURED_PROFILES", "")
        ),
        "command_enabled_profiles": _profiles(
            values.get("SPARK_SMOKE_COMMAND_PROFILES", "")
        ),
        "execution_mode": (
            "local[2]-only"
            if local_only
            else "local[2]-with-cluster-readiness"
        ),
        "distributed_executors_verified": False,
        "image_identity": image,
        "synthetic_namespace": "compatibility",
        "synthetic_table": "compatibility.spark_roundtrip",
        "spark_catalog": "lakehouse",
        "trino_catalog": "iceberg",
        "stages": {
            name: {"status": "not_run", "code": None} for name in STAGES
        },
        "overall_status": "failed",
        "exit_code": 1,
        "counts": {
            name: None
            for name in (
                "fixture",
                "spark",
                "trino",
                "replay_spark",
                "replay_trino",
            )
        },
        "value_match": None,
        "cleanup": {
            "artifact": "not_run",
            "session_stopped": None,
            "query_cancellation": "not_run",
        },
        "versions": {"spark": "unknown", "iceberg": "1.12.0"},
    }


def set_stage(result, name, status, code=None):
    """Accept fixed stages and statuses; arbitrary details stay out."""
    if (
        name not in STAGES
        or status not in STATUSES
        or (
            code is not None
            and (not isinstance(code, str) or code not in DIAGNOSTIC_CODES)
        )
    ):
        raise ValueError("Invalid smoke stage result.")
    result["stages"][name] = {"status": status, "code": code}


def finish_result(result, started_clock, *, exit_code=None):
    """Finalize elapsed time and fail closed on every incomplete stage."""
    skipped = (
        {"master_reachability", "worker_registration"}
        if result["execution_mode"] == "local[2]-only"
        else set()
    )
    succeeded = set(result["stages"]) == set(STAGES) and all(
        item["status"] == ("skipped" if name in skipped else "passed")
        for name, item in result["stages"].items()
    )
    succeeded = (
        succeeded
        and result["value_match"] is True
        and set(result["counts"])
        == {"fixture", "spark", "trino", "replay_spark", "replay_trino"}
        and all(
            type(count) is int and count == 3
            for count in result["counts"].values()
        )
        and result["cleanup"]
        == {
            "artifact": "retained_intentionally",
            "session_stopped": True,
            "query_cancellation": "not_run",
        }
    )
    if exit_code is not None:
        succeeded = succeeded and exit_code == 0
    result["overall_status"] = "passed" if succeeded else "failed"
    result["exit_code"] = 0 if succeeded else 1
    result["finished_at"] = utc_now()
    result["elapsed_seconds"] = round(
        max(0, time.monotonic() - started_clock), 6
    )
    return result


def result_record(result):
    """Separate one strict machine-readable record from JVM progress logs."""
    return RESULT_PREFIX + json.dumps(
        result, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def internal_opener():
    """Disable inherited proxy credentials and every HTTP redirect."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect()
    )


def read_json(opener, request, deadline, *, maximum=MAX_BODY):
    """Bound bytes and elapsed work with at most one in-flight read slack."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SmokeFailure("HTTP_DEADLINE")
    try:
        with opener.open(request, timeout=min(5, remaining)) as response:
            if (
                response.status != 200
                or response.headers.get("Content-Encoding", "identity")
                != "identity"
            ):
                raise SmokeFailure("HTTP_RESPONSE_REJECTED")
            data = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise SmokeFailure("HTTP_DEADLINE")
                # HTTPResponse.read1 performs at most one underlying read.
                # Buffered read(n) may loop internally on a slow-drip peer.
                chunk = response.read1(8192)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > maximum:
                    raise SmokeFailure("HTTP_RESPONSE_TOO_LARGE")
            if time.monotonic() >= deadline:
                raise SmokeFailure("HTTP_DEADLINE")
        return json.loads(data)
    except SmokeFailure:
        raise
    except Exception:
        raise SmokeFailure("HTTP_TRANSPORT_FAILED") from None


def get_json(opener, url, deadline):
    """Read exactly one fixed Spark endpoint, never a caller-provided URL."""
    if url not in {MASTER_JSON, WORKER_JSON}:
        raise SmokeFailure("HTTP_ORIGIN_REJECTED")
    return read_json(
        opener,
        urllib.request.Request(url, headers={"Accept-Encoding": "identity"}),
        deadline,
    )


def check_cluster(result, *, opener=None):
    """Join Spark 4.1 Master and Worker JSON by the registered worker ID.

    Source contract: apache/spark v4.1.4 deploy/JsonProtocol.scala. Worker JSON
    has no state field; ALIVE must come from its matching Master worker row.
    No endpoint contents or IDs are included in the public smoke summary.
    """
    opener = opener or internal_opener()
    deadline = time.monotonic() + 15
    try:
        master = get_json(opener, MASTER_JSON, deadline)
        if not isinstance(master, dict) or (
            master.get("status") != "ALIVE" or master.get("url") != MASTER_URL
        ):
            raise SmokeFailure("MASTER_NOT_READY")
        set_stage(result, "master_reachability", "passed")
    except Exception:
        set_stage(result, "master_reachability", "failed", "MASTER_NOT_READY")
        raise SmokeFailure("MASTER_NOT_READY") from None
    try:
        worker = get_json(opener, WORKER_JSON, deadline)
        workers = master.get("workers")
        if (
            not isinstance(worker, dict)
            or worker.get("masterurl") != MASTER_URL
            or not isinstance(worker.get("id"), str)
            or not worker["id"]
            or not isinstance(workers, list)
            or len(workers) > 100
            or not any(
                isinstance(item, dict)
                and item.get("id") == worker["id"]
                and item.get("state") == "ALIVE"
                and item.get("host") == "spark-worker"
                and item.get("port") == 7078
                for item in workers
            )
        ):
            raise SmokeFailure("WORKER_NOT_REGISTERED")
        set_stage(result, "worker_registration", "passed")
    except Exception:
        set_stage(
            result, "worker_registration", "failed", "WORKER_NOT_REGISTERED"
        )
        raise SmokeFailure("WORKER_NOT_REGISTERED") from None


def approved_trino_uri(value):
    """Accept only official statement-page paths on the fixed Trino origin."""
    if not isinstance(value, str) or len(value) > 2048:
        raise SmokeFailure("TRINO_ORIGIN_REJECTED")
    try:
        parts = urllib.parse.urlsplit(value)
        valid = (
            parts.scheme == "http"
            and parts.netloc == "trino:8080"
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
            and re.fullmatch(
                r"/v1/statement/(?:queued|executing)/[A-Za-z0-9_./-]+",
                parts.path,
            )
            and ".." not in parts.path
            and "//" not in parts.path
        )
    except ValueError:
        valid = False
    if not valid:
        raise SmokeFailure("TRINO_ORIGIN_REJECTED")
    return value


def trino_rows(statement, *, opener=None):
    """Read the fixed fixture; cancel partial queries on the same origin."""
    if statement != (
        "SELECT id, label, CAST(amount AS varchar) "
        "FROM iceberg.compatibility.spark_roundtrip ORDER BY id"
    ):
        raise SmokeFailure("TRINO_QUERY_REJECTED")
    opener = opener or internal_opener()
    request = urllib.request.Request(
        TRINO_ENDPOINT,
        data=statement.encode("utf-8"),
        headers={
            "X-Trino-User": "spark-smoke",
            "Content-Type": "text/plain; charset=utf-8",
            "Accept-Encoding": "identity",
        },
        method="POST",
    )
    deadline = time.monotonic() + 120
    rows = []
    pending = None
    try:
        for _ in range(MAX_PAGES):
            payload = read_json(opener, request, deadline)
            if not isinstance(payload, dict):
                raise SmokeFailure("TRINO_RESPONSE_INVALID")
            next_uri = payload.get("nextUri")
            pending = approved_trino_uri(next_uri) if next_uri else None
            if payload.get("error"):
                raise SmokeFailure("TRINO_QUERY_FAILED")
            data = payload.get("data", [])
            if not isinstance(data, list) or any(
                not isinstance(row, list) or len(row) != 3 for row in data
            ):
                raise SmokeFailure("TRINO_RESPONSE_INVALID")
            rows.extend(data)
            if len(rows) > 3:
                raise SmokeFailure("TRINO_ROW_LIMIT")
            if pending is None:
                return rows
            request = urllib.request.Request(
                pending,
                headers={
                    "X-Trino-User": "spark-smoke",
                    "Accept-Encoding": "identity",
                },
            )
            time.sleep(0.1)
        raise SmokeFailure("TRINO_PAGE_LIMIT")
    except Exception:
        cancellation = "unavailable"
        if pending is not None:
            try:
                request = urllib.request.Request(pending, method="DELETE")
                with opener.open(request, timeout=3) as response:
                    cancellation = (
                        "succeeded"
                        if 200 <= response.status < 300
                        else "failed"
                    )
            except Exception:
                # The read-only query may outlive cancellation failure. Do not
                # report complete, and never send cancellation to another URL.
                cancellation = "failed"
        raise SmokeFailure(
            "TRINO_CROSS_READ_FAILED", cancellation=cancellation
        ) from None
