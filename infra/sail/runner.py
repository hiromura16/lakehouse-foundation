"""Explicit synthetic Spark/Sail comparison; normal deploy performs no writes.

The pure plan is safe in source CI. run requires an explicit consent flag,
the dedicated container-owned state volume, and fixed ownership markers.
Actual engine, Iceberg, S3 and Trino failures remain failures: there is no
automatic Spark fallback for Sail or mocked live-success path.
"""

import argparse
import fcntl
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from infra.sail.catalog import Catalog, trino_rows
from infra.sail.contract import (
    ENGINE_NAMESPACES,
    MAX_ROWS,
    OWNER,
    RUN_STAGES,
    SOURCES,
    TABLES,
    LabFailure,
    bronze_rows,
    compare,
    fixture_bytes,
    gold_select,
    native_timestamp7_probe,
    reference,
    schema_for,
    silver_select,
    target,
)
from infra.sail.evidence import compare_reports, read_report

STATE_ROOT = Path("/opt/lakehouse/lab-state")
STAGES = RUN_STAGES


def now():
    """Use explicit UTC timestamps, never local timezone-dependent strings."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def plan():
    """Describe supported source contracts and unmeasured runtime gates."""
    fixture, checksum = fixture_bytes()
    counts = {name: len(rows) for name, rows in reference(fixture).items()}
    return {
        "schema_version": 1,
        "kind": "spark-sail-lab",
        "status": "plan",
        "fixture_sha256": checksum,
        "table_counts": counts,
        "namespaces": dict(ENGINE_NAMESPACES),
        "catalog_alias": "lakehouse",
        "trino_catalog_alias": "iceberg",
        "sail_configured_version": "0.7.3",
        "spark_configured_version": "4.1.4",
        "sail_client_version": "inherited-pyspark-4.1.4",
        "protocol_compatibility": "not_run",
        "iceberg_rest_write": "documented_upstream_not_run_here",
        "native_timestamp7_connect": "unsupported_documented",
        "timestamp_representation": "UTC7-string-and-int64-100ns-ticks",
        "execution_modes": {"spark": "local[2]", "sail": "Spark-Connect"},
        "stages": {name: "not_run" for name in STAGES},
        "resource_metrics": {
            "engine_cpu_average": None,
            "engine_cpu_max": None,
            "engine_memory_average": None,
            "engine_memory_max": None,
            "spill_bytes": None,
            "measurement_period_seconds": None,
        },
        "performance_verdict": "pending_runtime_measurements",
        "permissions": "explicit-synthetic-lab-write-consent-required",
        "retention": "lab-tables-and-evidence-retained-no-automatic-delete",
    }


@contextmanager
def exclusive_run(engine, root=STATE_ROOT):
    """Serialize each engine and fail closed after uncertain/crashed writers.

    Only the dedicated named-volume root is accepted by the CLI. Tests pass
    owned temporary roots explicitly. No initialization/chown or permission
    repair is performed. An interrupted marker requires operator review of
    container quiescence; rerunning never clears it automatically.
    """
    if engine not in ENGINE_NAMESPACES:
        raise LabFailure("DESTINATION_REJECTED")
    descriptor = directory = None
    blocked, initialized = False, False
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        metadata = os.fstat(directory)
        if (
            metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise LabFailure("STATE_UNAVAILABLE")
        descriptor = os.open(
            engine + ".lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise LabFailure("STATE_UNAVAILABLE")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LabFailure("RUN_ACTIVE") from None
        if os.fstat(descriptor).st_size:
            raise LabFailure("PREVIOUS_RUN_UNCERTAIN")
        if os.write(descriptor, b"RUN_IN_PROGRESS\n") != 16:
            raise LabFailure("STATE_UNAVAILABLE")
        os.fsync(descriptor)
        initialized = True

        def retain_marker():
            nonlocal blocked
            blocked = True

        yield retain_marker
    except OSError:
        raise LabFailure("STATE_UNAVAILABLE") from None
    finally:
        if descriptor is not None:
            if initialized and not blocked:
                os.ftruncate(descriptor, 0)
                os.fsync(descriptor)
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def create_session(engine):
    """Use local Spark or only the fixed native Sail Connect endpoint."""
    try:
        from pyspark.sql import SparkSession
    except ImportError:
        raise LabFailure("DEPENDENCY_UNAVAILABLE") from None
    if engine == "sail":
        try:
            # The thin Connect path must not silently initialize a local JVM.
            import grpc  # noqa: F401

            return SparkSession.builder.remote(
                "sc://sail-server:50051"
            ).getOrCreate()
        except ImportError:
            raise LabFailure("DEPENDENCY_UNAVAILABLE") from None
        except Exception:
            raise LabFailure("ENGINE_CONNECT_FAILED") from None
    if engine != "spark":
        raise LabFailure("DESTINATION_REJECTED")
    prefix = "spark.sql.catalog.lakehouse"
    options = {
        "spark.sql.extensions": (
            "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions"
        ),
        "spark.sql.shuffle.partitions": "2",
        "spark.sql.session.timeZone": "UTC",
        "spark.redaction.regex": (
            "(?i)secret|password|token|access.key|access-key"
        ),
        prefix: "org.apache.iceberg.spark.SparkCatalog",
        prefix + ".catalog-impl": "org.apache.iceberg.rest.RESTCatalog",
        prefix + ".uri": "http://catalog:8181/catalog",
        prefix + ".warehouse": os.environ.get("WAREHOUSE_NAME", ""),
        prefix + ".token": "dummy",
        prefix + ".io-impl": "org.apache.iceberg.aws.s3.S3FileIO",
        prefix + ".s3.endpoint": "http://storage:8333",
        prefix + ".s3.path-style-access": "true",
        prefix + ".s3.access-key-id": os.environ.get("S3_ACCESS_KEY", ""),
        prefix + ".s3.secret-access-key": os.environ.get("S3_SECRET_KEY", ""),
        prefix + ".client.region": "us-east-1",
        prefix + ".rest-metrics-reporting-enabled": "false",
    }
    builder = SparkSession.builder.master("local[2]").appName(
        "synthetic-sail-lab"
    )
    for key, value in options.items():
        builder = builder.config(key, value)
    try:
        return builder.getOrCreate()
    except Exception:
        raise LabFailure("ENGINE_CONNECT_FAILED") from None


def observed_rows(frame, table):
    """Bound collection and compare actual engine column types and names."""
    schema = tuple(
        (field.name, field.dataType.simpleString()) for field in frame.schema
    )
    if schema != schema_for(table):
        raise LabFailure("SCHEMA_MISMATCH")
    rows = frame.take(MAX_ROWS + 1)
    if len(rows) > MAX_ROWS:
        raise LabFailure("VALUE_CONTRACT_INVALID")
    return [row.asDict(recursive=False) for row in rows]


def write_table(session, catalog, engine, table, frame):
    """Create owned tables once, then full-overwrite only proven owned data.

    REST catalog REPLACE/CREATE OR REPLACE is unsupported by Sail 0.7.3.
    The temp view and destination are both fixed, not user input. Ownership
    is checked before EVERY overwrite, including the replay pass.
    """
    destination = target(engine, table)
    prior = catalog.table(engine, table, missing=True)
    frame.createOrReplaceTempView("lab_result")
    if prior is None:
        statement = (
            f"CREATE TABLE {destination} USING iceberg "
            f"TBLPROPERTIES ('format-version'='2', 'lab.owner'='{OWNER}') "
            "AS SELECT * FROM lab_result"
        )
    else:
        statement = (
            f"INSERT OVERWRITE TABLE {destination} SELECT * FROM lab_result"
        )
    session.sql(statement).collect()
    return catalog.table(engine, table)


def workload(session, catalog, engine, fixture):
    """Run the SAME six-source Bronze -> Silver -> factory/day Gold recipe."""
    from pyspark.sql.types import StringType, StructField, StructType

    metadata = {}
    for source in SOURCES:
        table = "bronze_" + source
        columns = schema_for(table)
        schema = StructType(
            [StructField(name, StringType(), True) for name, _ in columns]
        )
        rows = [
            tuple(row[name] for name, _ in columns)
            for row in bronze_rows(fixture, source)
        ]
        metadata[table] = write_table(
            session,
            catalog,
            engine,
            table,
            session.createDataFrame(rows, schema),
        )
    for source in SOURCES:
        session.table(
            target(engine, "bronze_" + source)
        ).createOrReplaceTempView("raw_" + source)
        table = "silver_" + source
        metadata[table] = write_table(
            session, catalog, engine, table, session.sql(silver_select(source))
        )
    for source, alias in (
        ("erp_work_orders", "lab_work"),
        ("mes_events", "lab_events"),
        ("mes_downtime", "lab_downtime"),
    ):
        session.table(
            target(engine, "silver_" + source)
        ).createOrReplaceTempView(alias)
    metadata["factory_daily_production"] = write_table(
        session,
        catalog,
        engine,
        "factory_daily_production",
        session.sql(gold_select()),
    )
    return metadata


def checked_comparison(table, expected, observed, *, replay=False):
    """Fail on exact data mismatch and return only bounded public evidence."""
    evidence = compare(table, expected, observed)
    if not evidence["equal"]:
        raise LabFailure("REPLAY_MISMATCH" if replay else "VALUE_MISMATCH")
    # Synthetic diffs are available through contract.compare during isolated
    # debugging, but never dump engine content to ordinary CLI/public logs.
    return {
        key: value
        for key, value in evidence.items()
        if key != "synthetic_diffs"
    }


def run(
    engine,
    *,
    approve=False,
    trial=1,
    cache_state="unknown",
    session_factory=create_session,
    catalog_factory=Catalog,
    lock_factory=exclusive_run,
    workload_function=workload,
    cross_read=trino_rows,
):
    """Record each real stage and cleanup; never invent unmeasured metrics."""
    result = plan()
    result.update(
        {
            "run_id": uuid.uuid4().hex,
            "engine": engine,
            "started_at": now(),
            "finished_at": None,
            "elapsed_seconds": None,
            "status": "failed",
            "exit_code": 1,
            "trial": trial,
            "cache_state": cache_state,
            "code": None,
            "job_seconds": [],
            "session_startup_seconds": None,
            "runtime_reported_version": "unknown",
            "tables": {},
            "image_identity": "unknown",
            "source_commit": "unknown",
            "dirty_status": "unknown",
            "cleanup": "not_run",
            "capability_scope": (
                "synthetic-UTC7-string-ticks-not-native-timestamp7"
            ),
        }
    )
    configured_commit = os.environ.get("LAKEHOUSE_LAB_SOURCE_COMMIT", "")
    if re.fullmatch(r"[0-9a-f]{40}", configured_commit):
        result["source_commit"] = configured_commit
    start = time.monotonic()
    session, stage = None, "input_checksum"
    try:
        target(engine, "factory_daily_production")
        if not approve:
            raise LabFailure("WRITE_APPROVAL_REQUIRED")
        if (
            type(trial) is not int
            or not 1 <= trial <= 100
            or cache_state not in {"cold", "warm", "unknown"}
        ):
            raise LabFailure("VALUE_CONTRACT_INVALID")
        fixture, _ = fixture_bytes()
        expected = reference(fixture)
        result["stages"][stage] = "passed"
        with lock_factory(engine) as retain_marker:
            try:
                stage = "client_session"
                session_start = time.monotonic()
                session = session_factory(engine)
                if session.sql("SELECT 1 AS ready").take(1)[0][0] != 1:
                    raise LabFailure("ENGINE_CONNECT_FAILED")
                result["session_startup_seconds"] = round(
                    time.monotonic() - session_start, 6
                )
                version = session.version
                if isinstance(version, str) and re.fullmatch(
                    r"[0-9.]{3,20}", version
                ):
                    result["runtime_reported_version"] = version
                result["stages"][stage] = "passed"
                stage = "catalog_ownership"
                catalog = catalog_factory()
                catalog.namespace(engine, approve=True)
                result["stages"][stage] = "passed"
                for replay in (False, True):
                    stage = "replay" if replay else "bronze_write"
                    job_start = time.monotonic()
                    metadata = workload_function(
                        session, catalog, engine, fixture
                    )
                    result["job_seconds"].append(
                        round(time.monotonic() - job_start, 6)
                    )
                    if set(metadata) != set(TABLES):
                        raise LabFailure("SCHEMA_MISMATCH")
                    if not replay:
                        for completed in (
                            "bronze_write",
                            "silver_transform",
                            "gold_transform",
                            "iceberg_metadata",
                        ):
                            result["stages"][completed] = "passed"
                    for table in TABLES:
                        stage = "replay" if replay else "engine_equality"
                        engine_evidence = checked_comparison(
                            table,
                            expected[table],
                            observed_rows(
                                session.table(target(engine, table)), table
                            ),
                            replay=replay,
                        )
                        stage = "replay" if replay else "trino_equality"
                        trino_evidence = checked_comparison(
                            table,
                            expected[table],
                            cross_read(engine, table),
                            replay=replay,
                        )
                        result["tables"][table] = {
                            "engine": engine_evidence,
                            "trino": trino_evidence,
                            "iceberg": metadata[table],
                            "replay_verified": replay,
                        }
                    if replay:
                        result["stages"]["replay"] = "passed"
                    else:
                        result["stages"]["engine_equality"] = "passed"
                        result["stages"]["trino_equality"] = "passed"
            except BaseException:
                # A network/engine failure may leave a remote action running
                # even when client-side stop subsequently returns. Preserve
                # the durable marker on interrupts too, then re-raise them.
                # Independent quiescence review is required before retry.
                retain_marker()
                raise
            finally:
                if session is not None:
                    try:
                        session.stop()
                        result["stages"]["session_stop"] = "passed"
                        result["cleanup"] = (
                            "session-stopped-lab-tables-retained"
                        )
                    except BaseException as error:
                        retain_marker()
                        result["stages"]["session_stop"] = "failed"
                        result["cleanup"] = "uncertain-manual-review-required"
                        if not isinstance(error, Exception):
                            raise
                        raise LabFailure("SESSION_STOP_FAILED") from None
                else:
                    # A client/session creation exception may have left a
                    # remote job or JVM. Do not unlock future writes blindly.
                    retain_marker()
                    result["cleanup"] = "uncertain-manual-review-required"
        if all(value == "passed" for value in result["stages"].values()):
            result["status"], result["exit_code"] = "passed", 0
    except LabFailure as error:
        result["code"] = error.code
        if result["stages"].get(stage) != "passed":
            result["stages"][stage] = "failed"
    except Exception:
        result["code"] = "ENGINE_QUERY_FAILED"
        result["stages"][stage] = "failed"
    result["finished_at"] = now()
    result["elapsed_seconds"] = round(time.monotonic() - start, 6)
    return result


def main(argv=None):
    """Expose plan, a negative probe, and an explicitly consented run."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "plan", help="Pure source plan; no socket or table writes."
    )
    commands.add_parser(
        "timestamp7-probe",
        help="Expected unsupported native 100 ns contract, no engine access.",
    )
    execution = commands.add_parser(
        "run", help="Explicit synthetic lab write operation."
    )
    execution.add_argument(
        "--engine", required=True, choices=sorted(ENGINE_NAMESPACES)
    )
    execution.add_argument("--approve-lab-write", action="store_true")
    execution.add_argument("--trial", type=int, default=1)
    execution.add_argument(
        "--cache-state", choices=("cold", "warm", "unknown"), default="unknown"
    )
    comparison = commands.add_parser(
        "compare", help="Compare two private result summaries."
    )
    comparison.add_argument("--spark-report", required=True, type=Path)
    comparison.add_argument("--sail-report", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "plan":
        result, exit_code = plan(), 0
    elif args.command == "timestamp7-probe":
        try:
            result = native_timestamp7_probe()
            exit_code = 1
        except LabFailure as error:
            result = {
                "schema_version": 1,
                "kind": "spark-sail-capability",
                "status": "expected_blocker",
                "code": error.code,
                "evidence": (
                    "documented-contract-negative-test-not-live-engine"
                ),
            }
            exit_code = 2
    elif args.command == "compare":
        try:
            result = compare_reports(
                read_report(args.spark_report), read_report(args.sail_report)
            )
            exit_code = 0
        except (LabFailure, KeyError, TypeError):
            result = {
                "schema_version": 1,
                "kind": "spark-sail-comparison",
                "correctness": "failed",
                "code": "VALUE_CONTRACT_INVALID",
            }
            exit_code = 1
    else:
        result = run(
            args.engine,
            approve=args.approve_lab_write,
            trial=args.trial,
            cache_state=args.cache_state,
        )
        exit_code = result["exit_code"]
    print(
        "LAKEHOUSE_SAIL_RESULT="
        + json.dumps(
            result, ensure_ascii=False, sort_keys=True, allow_nan=False
        )
    )
    return exit_code
