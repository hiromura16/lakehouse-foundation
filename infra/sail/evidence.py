"""Compare exact synthetic results without confusing them with performance.

These are result-contract checks. They do not prove an untrusted JSON file
came from a live engine. Keep execution provenance and independent runtime
review alongside the summaries; no code here signs or attests evidence.
"""

import json
import os
import stat

from infra.sail.contract import (
    RUN_STAGES,
    TABLES,
    LabFailure,
    compare,
    fixture_bytes,
    reference,
    schema_for,
)


def read_report(path):
    """Read one tagged record from an explicitly selected private file.

    Surrounding JVM output is ignored and never reproduced. Files must be
    owned, regular, private mode 0600, bounded, and not symlinks/hardlinks.
    No private settings or environment file is ever selected automatically.
    """
    descriptor = None
    try:
        # Nonblocking open lets fstat reject FIFOs/devices without waiting for
        # a writer. Regular private evidence files retain ordinary reads.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 2 * 1024 * 1024
        ):
            raise LabFailure("VALUE_CONTRACT_INVALID")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise LabFailure("VALUE_CONTRACT_INVALID")
        prefix = "LAKEHOUSE_SAIL_RESULT="
        records = [
            line.removeprefix(prefix)
            for line in content.decode().splitlines()
            if line.startswith(prefix)
        ]
        if len(records) != 1:
            raise LabFailure("VALUE_CONTRACT_INVALID")
        result = json.loads(records[0])
        if not isinstance(result, dict):
            raise LabFailure("VALUE_CONTRACT_INVALID")
        return result
    except (OSError, ValueError, UnicodeError):
        raise LabFailure("VALUE_CONTRACT_INVALID") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def compare_reports(spark, sail):
    """Require both complete runs and exact schemas, hashes and multiplicity.

    Each report must match the checked-in Python oracle independently. A
    pair of identically wrong results must not pass merely by agreeing with
    one another. Snapshot IDs are expected to differ and are not compared;
    table format/schema/nullability are compared exactly.
    """
    fixture, checksum = fixture_bytes()
    expected = reference(fixture)
    for engine, report in (("spark", spark), ("sail", sail)):
        if (
            not isinstance(report, dict)
            or type(report.get("schema_version")) is not int
            or report["schema_version"] != 1
            or report.get("kind") != "spark-sail-lab"
            or report.get("engine") != engine
            or report.get("status") != "passed"
            or type(report.get("exit_code")) is not int
            or report["exit_code"] != 0
            or report.get("fixture_sha256") != checksum
            or not isinstance(report.get("tables"), dict)
            or set(report["tables"]) != set(TABLES)
            or report.get("cleanup") != "session-stopped-lab-tables-retained"
        ):
            raise LabFailure("VALUE_MISMATCH")
        if (
            not isinstance(report.get("stages"), dict)
            or set(report["stages"]) != set(RUN_STAGES)
            or any(value != "passed" for value in report["stages"].values())
        ):
            raise LabFailure("VALUE_MISMATCH")
        for table in TABLES:
            entry = report["tables"][table]
            oracle = compare(table, expected[table], expected[table])
            if not isinstance(entry, dict):
                raise LabFailure("VALUE_MISMATCH")
            if entry.get("replay_verified") is not True:
                raise LabFailure("REPLAY_MISMATCH")
            for observation in ("engine", "trino"):
                values = entry.get(observation, {})
                counts = (
                    "expected_count",
                    "observed_count",
                    "missing_count",
                    "extra_count",
                )
                if (
                    not isinstance(values, dict)
                    or values.get("equal") is not True
                    or any(type(values.get(key)) is not int for key in counts)
                    or any(
                        values.get(key) != oracle[key]
                        for key in (
                            "expected_count",
                            "observed_count",
                            "expected_sha256",
                            "observed_sha256",
                            "missing_count",
                            "extra_count",
                        )
                    )
                ):
                    raise LabFailure("VALUE_MISMATCH")
            metadata = entry.get("iceberg", {})
            if (
                not isinstance(metadata, dict)
                or type(metadata.get("format_version")) is not int
                or metadata["format_version"] != 2
                or not metadata.get("snapshot_id")
            ):
                raise LabFailure("TABLE_FORMAT_UNSUPPORTED")
            expected_schema = [list(field) for field in schema_for(table)]
            nullable = metadata.get("nullable")
            if (
                metadata.get("schema") != expected_schema
                or not isinstance(nullable, list)
                or len(nullable) != len(expected_schema)
                or any(type(value) is not bool for value in nullable)
            ):
                raise LabFailure("SCHEMA_MISMATCH")
    for table in TABLES:
        left = spark["tables"][table]["iceberg"]
        right = sail["tables"][table]["iceberg"]
        if left.get("schema") != right.get("schema") or left.get(
            "nullable"
        ) != right.get("nullable"):
            raise LabFailure("SCHEMA_MISMATCH")
    return {
        "schema_version": 1,
        "kind": "spark-sail-comparison",
        "fixture_sha256": checksum,
        "correctness": "passed",
        "table_count": len(TABLES),
        "replay": "passed",
        "performance_verdict": (
            "pending_resource-and-repeated-runtime-evidence"
        ),
        "native_timestamp7": "unsupported-not-an-acceptance-pass",
        "snapshot_ids": "independently-observed-not-required-equal",
        "provenance_verified": False,
    }
