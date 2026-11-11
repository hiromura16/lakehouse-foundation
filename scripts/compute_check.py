"""Run an explicitly selected, already-running project's synthetic Spark check.

No daemon is started, initialized or repaired here. Docker metadata excludes
container environment, bind sources and vendor logs. The fixed one-shot is
built by its owner and removed by Compose. Host and container failures share
one sanitized result contract; a CLI timeout is never proof of job cleanup.
"""

import importlib.util
import json
import os
import re
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass

from private_result import validate_destination, write_result
from settings import ROOT

SPEC = importlib.util.spec_from_file_location(
    "compute_smoke_result", ROOT / "infra/compute/smoke_result.py"
)
CONTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)
HOST_STAGES = ("settings", "target_project", "bootstrap", "build", "container")
SERVICE_FORMAT = (
    '{"project":{{json (index .Config.Labels '
    '"com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"owner":{{json (index .Config.Labels "app.owner")}},'
    '"run_id":{{json (index .Config.Labels "lakehouse.validation.run_id")}},'
    '"state":{{json .State.Status}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}'
    '{{else}}"unknown"{{end}},"exit_code":{{.State.ExitCode}},'
    '"mounts":[{{range $i,$v := .Mounts}}{{if $i}},{{end}}'
    '{"type":{{json $v.Type}},"name":{{json $v.Name}},'
    '"target":{{json $v.Destination}}}{{end}}]}'
)


@dataclass
class CommandResult:
    """Bounded, unpublished command output plus normalized failure state."""

    returncode: int
    output: bytes = b""
    records: tuple = ()
    error: str | None = None


class CheckFailure(ValueError):
    """Keep command arguments, endpoints and operating-system text private."""


def selected_project(values, project=None):
    """Override interpolation only for an explicit validation target."""
    selected = dict(values)
    if project is not None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", project):
            raise CheckFailure("INVALID_PROJECT_NAME")
        selected["COMPOSE_PROJECT_NAME"] = project
    return selected


def settings_failure(*, local_only=False, result_dir=None):
    """Emit safe evidence even if settings/preflight cannot be constructed."""
    started = time.monotonic()
    result = CONTRACT.new_result({}, local_only=local_only)
    result["host_stages"] = {
        name: {"status": "not_run", "code": None} for name in HOST_STAGES
    }
    result["host_stages"]["settings"] = {
        "status": "failed",
        "code": "SETTINGS_INVALID",
    }
    CONTRACT.finish_result(result, started, exit_code=1)
    if result_dir is not None:
        try:
            write_result(result_dir, result)
        except (ValueError, OSError):
            result["evidence_error"] = "RESULT_WRITE_FAILED_OR_UNCERTAIN"
    print(CONTRACT.result_record(result))
    return 1


def bounded_command(command, env, *, timeout=30, capture=True):
    """Bound child time/output; discard JVM/build noise without printing it."""
    process = None
    output = bytearray()
    pending = bytearray()
    records = []
    total = 0
    failure = None
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            command,
            env=env,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            bufsize=0,
        )
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    failure = "COMMAND_TIMEOUT"
                    break
                for key, _ in selector.select(0.2):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > 16 * 1024 * 1024:
                        failure = "COMMAND_OUTPUT_LIMIT"
                        break
                    if capture:
                        output.extend(chunk)
                        if len(output) > 65536:
                            failure = "COMMAND_OUTPUT_LIMIT"
                            break
                    else:
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            if line.startswith(
                                CONTRACT.RESULT_PREFIX.encode()
                            ):
                                records.append(bytes(line))
                            if len(records) > 1:
                                failure = "INVALID_CONTAINER_RESULT"
                                break
                        if len(pending) > 65536:
                            failure = "COMMAND_LINE_LIMIT"
                if failure:
                    break
        if failure:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            return CommandResult(-1, error=failure)
        if pending.startswith(CONTRACT.RESULT_PREFIX.encode()):
            records.append(bytes(pending))
        return CommandResult(
            process.wait(timeout=max(1, deadline - time.monotonic())),
            bytes(output),
            tuple(records),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):
                pass
        return CommandResult(-1, error="COMMAND_UNAVAILABLE")
    finally:
        if process is not None and process.stdout is not None:
            process.stdout.close()


def metadata(command, env, runner):
    """Return only bounded command output, never its diagnostic body."""
    result = runner(command, env, timeout=30, capture=True)
    if result.returncode or result.error:
        raise CheckFailure("TARGET_METADATA_UNAVAILABLE")
    return result.output.decode("utf-8").strip()


def container(identifier, env, runner):
    """Inspect selected labels/state/volume names without credentials."""
    if not re.fullmatch(r"[0-9a-f]{12,64}", identifier):
        raise CheckFailure("INVALID_CONTAINER_ID")
    value = json.loads(
        metadata(
            ["docker", "inspect", "--format", SERVICE_FORMAT, identifier],
            env,
            runner,
        )
    )
    if not isinstance(value, dict):
        raise CheckFailure("INVALID_TARGET_METADATA")
    return value


def service(
    compose,
    name,
    values,
    env,
    runner,
    *,
    completed=False,
    health_required=True,
):
    """Refuse missing, ambiguous, unhealthy or foreign project services."""
    identifier = metadata(
        compose + ["--profile", "compute", "ps", "--all", "--quiet", name],
        env,
        runner,
    )
    item = container(identifier, env, runner)
    if (
        item.get("project") != values["COMPOSE_PROJECT_NAME"]
        or item.get("service") != name
        or item.get("owner") != "lakehouse-foundation"
    ):
        raise CheckFailure("TARGET_IDENTITY_MISMATCH")
    if completed:
        if item.get("state") != "exited" or item.get("exit_code") != 0:
            raise CheckFailure("BOOTSTRAP_NOT_COMPLETE")
    elif item.get("state") != "running" or (
        health_required and item.get("health") != "healthy"
    ):
        raise CheckFailure("TARGET_NOT_HEALTHY")
    return item


def check_volumes(item, values):
    """Require existing Spark roots in the intended physical volume prefix."""
    prefix = values.get("DATA_VOLUME_PREFIX") or values["COMPOSE_PROJECT_NAME"]
    expected = {
        "/opt/spark/events": prefix + "_spark-events",
        "/opt/spark/work": prefix + "_spark-work",
    }
    mounts = item.get("mounts")
    if not isinstance(mounts, list):
        raise CheckFailure("VOLUME_IDENTITY_MISMATCH")
    for target, name in expected.items():
        found = [m for m in mounts if m.get("target") == target]
        if (
            len(found) != 1
            or found[0].get("type") != "volume"
            or (found[0].get("name") != name)
        ):
            raise CheckFailure("VOLUME_IDENTITY_MISMATCH")


def child_result(lines, expected):
    """Validate a single tagged record; unknown fields never reach evidence."""
    if (
        len(lines) != 1
        or type(lines[0]) is not bytes
        or len(lines[0]) > 65536
        or not lines[0].startswith(CONTRACT.RESULT_PREFIX.encode())
    ):
        raise CheckFailure("INVALID_CONTAINER_RESULT")
    value = json.loads(lines[0].removeprefix(CONTRACT.RESULT_PREFIX.encode()))
    if not isinstance(value, dict):
        raise CheckFailure("INVALID_CONTAINER_RESULT")
    fixed = (
        "schema_version",
        "kind",
        "run_id",
        "source_commit",
        "dirty_status",
        "configured_profiles",
        "command_enabled_profiles",
        "execution_mode",
        "distributed_executors_verified",
        "synthetic_namespace",
        "synthetic_table",
        "spark_catalog",
        "trino_catalog",
        "image_identity",
    )
    if any(
        type(value.get(key)) is not type(expected[key])
        or (value.get(key) != expected[key])
        for key in fixed
    ):
        raise CheckFailure("INVALID_CONTAINER_RESULT")
    cleaned = {key: expected[key] for key in fixed}
    stages = value.get("stages")
    if not isinstance(stages, dict) or set(stages) != set(CONTRACT.STAGES):
        raise CheckFailure("INVALID_CONTAINER_RESULT")
    cleaned["stages"] = {}
    for name, stage in stages.items():
        if not isinstance(stage, dict):
            raise CheckFailure("INVALID_CONTAINER_RESULT")
        status, code = stage.get("status"), stage.get("code")
        if status not in CONTRACT.STATUSES or (
            code is not None and code not in CONTRACT.DIAGNOSTIC_CODES
        ):
            raise CheckFailure("INVALID_CONTAINER_RESULT")
        cleaned["stages"][name] = {"status": status, "code": code}
    counts = value.get("counts")
    if not isinstance(counts, dict) or set(counts) != set(expected["counts"]):
        raise CheckFailure("INVALID_CONTAINER_RESULT")
    if any(
        c is not None and (type(c) is not int or not 0 <= c <= 100)
        for c in counts.values()
    ):
        raise CheckFailure("INVALID_CONTAINER_RESULT")
    cleaned["counts"] = dict(counts)
    if value.get("value_match") not in (True, False, None) or (
        value.get("value_match") is not None
        and type(value["value_match"]) is not bool
    ):
        raise CheckFailure("INVALID_CONTAINER_RESULT")
    cleaned["value_match"] = value.get("value_match")
    cleanup = value.get("cleanup")
    if not isinstance(cleanup, dict) or (
        cleanup.get("artifact")
        not in {"retained_intentionally", "not_run", "unknown"}
        or cleanup.get("query_cancellation")
        not in {"not_run", "succeeded", "failed", "unavailable"}
        or type(cleanup.get("session_stopped")) not in {bool, type(None)}
    ):
        raise CheckFailure("INVALID_CONTAINER_RESULT")
    cleaned["cleanup"] = {key: cleanup[key] for key in expected["cleanup"]}
    versions = value.get("versions")
    spark = versions.get("spark") if isinstance(versions, dict) else None
    if not isinstance(spark, str) or not re.fullmatch(
        r"unknown|[0-9]{1,3}\.[0-9]{1,3}\.[0-9A-Za-z.+-]{1,24}", spark
    ):
        raise CheckFailure("INVALID_CONTAINER_RESULT")
    cleaned["versions"] = {"spark": spark, "iceberg": "1.12.0"}
    return cleaned


def stop_owned_run(name, run_id, values, env, runner):
    """On uncertain completion, stop only our exact ephemeral container.

    A missing container or failed inspection is unknown, not successful
    cleanup. Stop uses its inspected immutable ID, never a reusable name.
    """
    try:
        raw = metadata(
            ["docker", "inspect", "--format", "{{.Id}}", name], env, runner
        )
        item = container(raw, env, runner)
        if (
            item.get("project") != values["COMPOSE_PROJECT_NAME"]
            or item.get("service") != "spark-check"
            or item.get("owner") != "lakehouse-foundation"
            or item.get("run_id") != run_id
        ):
            return "unknown"
        outcome = runner(
            ["docker", "stop", "--time", "10", raw],
            env,
            timeout=20,
            capture=True,
        )
        return (
            "stopped_owned_one_shot" if outcome.returncode == 0 else "unknown"
        )
    except (ValueError, OSError):
        return "unknown"


def run_check(
    compose,
    values,
    env,
    *,
    local_only=False,
    result_dir=None,
    runner=bounded_command,
):
    """Build/run only after target proof, then emit one safe JSON record."""
    started = time.monotonic()
    safe = {
        "SPARK_SMOKE_CONFIGURED_PROFILES": values.get("COMPOSE_PROFILES", ""),
        "SPARK_SMOKE_COMMAND_PROFILES": "compute",
    }
    for arguments, field, pattern in (
        (["git", "rev-parse", "HEAD"], "SOURCE_COMMIT", r"[0-9a-f]{40,64}"),
        (["git", "status", "--porcelain"], "DIRTY_STATUS", None),
    ):
        try:
            raw = metadata(arguments, env, runner)
            safe["SPARK_SMOKE_" + field] = (
                ("dirty" if raw else "clean")
                if pattern is None
                else raw
                if re.fullmatch(pattern, raw)
                else "unknown"
            )
        except (ValueError, OSError):
            safe["SPARK_SMOKE_" + field] = "unknown"
    result = CONTRACT.new_result(safe, local_only=local_only)
    result["host_stages"] = {
        name: {"status": "not_run", "code": None} for name in HOST_STAGES
    }
    result["container_cleanup"] = "not_run"
    # This is a tag observation, not proof of the one-shot's actual image ID.
    result["built_image_identity"] = "unknown"
    current = "settings"
    try:
        if result_dir is not None:
            validate_destination(result_dir)
        result["host_stages"][current]["status"] = "passed"
        current = "target_project"
        for core in ("catalog-db", "storage", "catalog", "trino", "worker"):
            # Storage has no image-provided healthcheck in the existing stack.
            # Its running process is not reported as an S3 round-trip result.
            service(
                compose,
                core,
                values,
                env,
                runner,
                health_required=core != "storage",
            )
        for name in () if local_only else ("spark-master", "spark-worker"):
            check_volumes(service(compose, name, values, env, runner), values)
        result["host_stages"][current]["status"] = "passed"
        current = "bootstrap"
        service(
            compose, "catalog-migrate", values, env, runner, completed=True
        )
        check_volumes(
            service(
                compose,
                "spark-volumes-init",
                values,
                env,
                runner,
                completed=True,
            ),
            values,
        )
        result["host_stages"][current]["status"] = "passed"
        current = "build"
        build = runner(
            compose + ["--profile", "compute", "build", "spark-volumes-init"],
            env,
            timeout=900,
            capture=False,
        )
        if build.returncode or build.error:
            raise CheckFailure("IMAGE_BUILD_FAILED")
        image = metadata(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                values["COMPOSE_PROJECT_NAME"] + "-spark-check:local",
            ],
            env,
            runner,
        )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise CheckFailure("BUILT_IMAGE_IDENTITY_UNAVAILABLE")
        result["built_image_identity"] = image
        result["host_stages"][current]["status"] = "passed"
        current = "container"
        name = "lakehouse-smoke-" + result["run_id"]
        arguments = [
            "--profile",
            "compute",
            "run",
            "--rm",
            "--no-deps",
            "--name",
            name,
            "--label",
            "lakehouse.validation.run_id=" + result["run_id"],
        ]
        for key, value in {
            **safe,
            "SPARK_SMOKE_RUN_ID": result["run_id"],
        }.items():
            arguments += ["--env", key + "=" + value]
        arguments += ["spark-check"]
        if local_only:
            arguments += [
                "/opt/spark/bin/spark-submit",
                "--master",
                "local[2]",
                "--driver-memory",
                "768m",
                "/opt/lakehouse/spark_smoke.py",
                "--local-only",
            ]
        try:
            outcome = runner(
                compose + arguments, env, timeout=600, capture=False
            )
            if outcome.error:
                raise CheckFailure("CONTAINER_RESULT_UNKNOWN")
            result.update(child_result(outcome.records, result))
            if outcome.returncode:
                raise CheckFailure("CONTAINER_FAILED")
        except (ValueError, OSError, TypeError, KeyError):
            # An ordinary nonzero CLI exit can follow an attach transport
            # error after the container started. --rm is not stop evidence.
            # Missing/malformed records likewise leave execution uncertain.
            result["container_cleanup"] = stop_owned_run(
                name, result["run_id"], values, env, runner
            )
            result["cleanup"]["artifact"] = "unknown"
            raise
        result["host_stages"][current]["status"] = "passed"
        result["container_cleanup"] = "compose_run_rm"
    except (ValueError, OSError, TypeError, KeyError):
        # Neither Docker output nor private settings/error strings are printed.
        result["host_stages"][current] = {
            "status": "failed",
            "code": current.upper() + "_FAILED",
        }
    CONTRACT.finish_result(result, started)
    if any(
        stage["status"] != "passed" for stage in result["host_stages"].values()
    ):
        result["overall_status"], result["exit_code"] = "failed", 1
    if result_dir is not None:
        try:
            write_result(result_dir, result)
        except (ValueError, OSError):
            result["overall_status"], result["exit_code"] = "failed", 1
            result["evidence_error"] = "RESULT_WRITE_FAILED_OR_UNCERTAIN"
    print(CONTRACT.result_record(result))
    return result["exit_code"]
