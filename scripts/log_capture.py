#!/usr/bin/env python3
"""Capture a finite selected-project log window into a private curated spool.

This operator tool uses existing Docker CLI permission; it does not grant a
socket, start services, change logging drivers, or run during deployment. The
explicit confirmation is required before any Docker query. Only fixed owned
Compose services in the selected project are eligible. Optional separately
managed Dokploy/Swarm logs are deliberately outside this default capture.

Raw captured logs can contain secrets. Files remain private, immutable and
outside Git. Vector redacts and normalizes before either sink. Overlapping
captures can repeat events; deterministic identity aids comparison, not an
exactly-once promise. No real host logs are used by the unit tests.
"""

import argparse
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from notebook_workspace import selected as selected_workspace
from notebook_workspace import walk, writable_fd

ROOT = Path(__file__).resolve().parents[1]
OWNER = "lakehouse-foundation"
MAX_CONTAINERS = 64
MAX_COMMAND_BYTES = 2 * 1024 * 1024
MAX_CAPTURE_BYTES = 16 * 1024 * 1024
MAX_FILES = 64
MAX_LINES = 10000
MAX_MESSAGE_BYTES = 16384
TIMEOUT_SECONDS = 60
SERVICES = frozenset(
    "catalog-db storage catalog catalog-migrate trino worker demo-api demo "
    "prometheus grafana superset-db superset-redis superset-init superset "
    "node-exporter cadvisor loki vector cdc-kafka cdc-connect cdc-ui "
    "cdc-register cdc-consumer spark-volumes-init spark-master spark-worker "
    "spark-history spark-check n8n-db n8n airflow-db airflow jupyter qdrant "
    "marquez-db marquez marquez-web mcp openobserve sail-server "
    "sail-benchmark".split()
)


class CaptureError(ValueError):
    """Carry fixed codes without Docker output or a selected path."""


def bounded_command(command, *, deadline):
    """Read both pipes under one absolute deadline and byte cap."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CaptureError("LOG_CAPTURE_DEADLINE")
    # No private source/proxy credentials or ambient remote Docker endpoint
    # enter the child environment. The caller supplies a dedicated CLI config.
    process = subprocess.Popen(
        command,
        env={"PATH": os.defpath},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            for stream, name in (
                (process.stdout, "stdout"),
                (process.stderr, "stderr"),
            ):
                selector.register(stream, selectors.EVENT_READ, name)
            total = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CaptureError("LOG_CAPTURE_DEADLINE")
                for key, _ in selector.select(min(0.2, remaining)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > MAX_COMMAND_BYTES:
                        raise CaptureError("LOG_CAPTURE_COMMAND_LIMIT")
                    output[key.data].extend(chunk)
            code = process.wait(
                timeout=max(0.001, deadline - time.monotonic())
            )
            if code != 0:
                raise CaptureError("LOG_CAPTURE_DOCKER_REJECTED")
    except (OSError, subprocess.SubprocessError):
        raise CaptureError("LOG_CAPTURE_COMMAND_FAILED") from None
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise CaptureError(
                    "LOG_CAPTURE_CHILD_CLEANUP_FAILED"
                ) from None
        process.stdout.close()
        process.stderr.close()
    return {key: bytes(value) for key, value in output.items()}


def selected(values):
    """Validate public shape without printing selected private values."""
    if "monitoring" not in values.get("COMPOSE_PROFILES", "").split(","):
        raise CaptureError("LOG_CAPTURE_PROFILE_REQUIRED")
    project = values.get("COMPOSE_PROJECT_NAME", "")
    alias = values.get("MONITORING_HOST_ALIAS", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise CaptureError("LOG_CAPTURE_PROJECT_INVALID")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", alias):
        raise CaptureError("LOG_CAPTURE_ALIAS_INVALID")
    raw = values.get("MONITORING_LOG_DIR", "")
    try:
        path, _, _ = selected_workspace({**values, "NOTEBOOKS_DIR": raw}, ROOT)
    except ValueError:
        raise CaptureError("LOG_CAPTURE_PRIVATE_DIRECTORY_REQUIRED") from None
    if str(os.geteuid()) != values.get("WORKER_UID"):
        raise CaptureError("LOG_CAPTURE_RUNTIME_OWNER_MISMATCH")
    return project, alias, path


def capture_lines(output, *, project, alias, identity, service, replica):
    """Parse Docker timestamps/streams before application text."""
    for stream in ("stdout", "stderr"):
        for line in output[stream].splitlines():
            stamp, delimiter, body = line.partition(b" ")
            if not delimiter or not re.fullmatch(
                rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", stamp
            ):
                raise CaptureError("LOG_CAPTURE_TIMESTAMP_INVALID")
            try:
                datetime.fromisoformat(
                    stamp.decode("ascii").replace("Z", "+00:00")
                )
            except ValueError:
                raise CaptureError("LOG_CAPTURE_TIMESTAMP_INVALID") from None
            position = hashlib.sha256(
                stamp + b"|" + stream.encode() + b"|" + body
            ).hexdigest()
            yield {
                "capture_version": 1,
                "timestamp": stamp.decode("ascii"),
                "compose_project": project,
                "compose_service": service,
                "host_alias": alias,
                "container_alias": service + "-" + replica,
                "source_identity": identity,
                "source_position": position,
                "stream": stream,
                "message": body[:MAX_MESSAGE_BYTES].decode(
                    "utf-8", errors="replace"
                ),
                "truncated": len(body) > MAX_MESSAGE_BYTES,
            }


def inspect_spool(handle, fd):
    """Require a private dedicated spool without links or unrelated files."""
    handle.verify()
    metadata = os.fstat(fd)
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(
        metadata.st_mode
    ) not in {0o700, 0o750}:
        raise CaptureError("LOG_CAPTURE_PRIVATE_DIRECTORY_MODE")
    with os.scandir(fd) as iterator:
        count = 0
        for entry in iterator:
            count += 1
            if count >= MAX_FILES:
                raise CaptureError("LOG_CAPTURE_SPOOL_CAPACITY")
            info = entry.stat(follow_symlinks=False)
            if (
                not re.fullmatch(r"[0-9a-f]{64}\.jsonl", entry.name)
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise CaptureError("LOG_CAPTURE_UNRELATED_OUTPUT_PRESENT")
    handle.verify()


def publish(handle, payload):
    """Keep a readable descriptor pinned for directory sync and publication."""
    fd = writable_fd(handle, len(handle.nodes) - 1)
    try:
        return _publish(handle, fd, payload)
    finally:
        os.close(fd)


def _publish(handle, fd, payload):
    """Publish a private content-addressed file without overwriting."""
    handle.verify()
    inspect_spool(handle, fd)
    identifier = hashlib.sha256(payload).hexdigest()
    destination = identifier + ".jsonl"
    try:
        existing = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        existing = None
    except OSError:
        raise CaptureError("LOG_CAPTURE_OUTPUT_CONFLICT") from None
    if existing is not None:
        try:
            info = os.fstat(existing)
            if not stat.S_ISREG(info.st_mode) or info.st_size != len(payload):
                raise CaptureError("LOG_CAPTURE_OUTPUT_CONFLICT")
            content = bytearray()
            while len(content) < len(payload):
                chunk = os.read(
                    existing, min(65536, len(payload) - len(content))
                )
                if not chunk:
                    break
                content.extend(chunk)
            if bytes(content) != payload:
                raise CaptureError("LOG_CAPTURE_OUTPUT_CONFLICT")
            return identifier, False
        finally:
            os.close(existing)
    temporary = ".capture-" + os.urandom(16).hex()
    created = None
    try:
        created = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=fd,
        )
        offset = 0
        while offset < len(payload):
            written = os.write(created, payload[offset:])
            if written <= 0:
                raise CaptureError("LOG_CAPTURE_OUTPUT_FAILED")
            offset += written
        os.fsync(created)
        handle.verify()
        os.link(
            temporary,
            destination,
            src_dir_fd=fd,
            dst_dir_fd=fd,
            follow_symlinks=False,
        )
        os.fsync(fd)
        return identifier, True
    except OSError:
        raise CaptureError("LOG_CAPTURE_OUTPUT_FAILED") from None
    finally:
        if created is not None:
            os.close(created)
            try:
                os.unlink(temporary, dir_fd=fd)
            except OSError:
                # An operator must inspect this private directory. Never
                # delete unrelated output or report successful publication.
                raise CaptureError("LOG_CAPTURE_TEMP_CLEANUP_FAILED") from None


def capture(values, *, confirm=False, minutes=15, tail=500, runner=None):
    """Collect once after confirmation, without background scheduling."""
    if confirm is not True:
        raise CaptureError("LOG_CAPTURE_APPROVAL_REQUIRED")
    if type(minutes) is not int or not 1 <= minutes <= 1440:
        raise CaptureError("LOG_CAPTURE_WINDOW_INVALID")
    if type(tail) is not int or not 1 <= tail <= 2000:
        raise CaptureError("LOG_CAPTURE_TAIL_INVALID")
    project, alias, path = selected(values)
    runner = bounded_command if runner is None else runner
    deadline = time.monotonic() + TIMEOUT_SECONDS
    since = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat()
    records = []
    containers = 0
    capture_bytes = 0
    with walk(path) as output:
        if output.missing:
            raise CaptureError("LOG_CAPTURE_DIRECTORY_NOT_PREPARED")
        inspection_fd = writable_fd(output, len(output.nodes) - 1)
        try:
            inspect_spool(output, inspection_fd)
        finally:
            os.close(inspection_fd)
        # Force the local default context without reading user registry auth or
        # inheriting a shell DOCKER_HOST pointing at another environment.
        with tempfile.TemporaryDirectory(
            prefix="log-capture-cli-"
        ) as temporary:
            cli = ["docker", "--config", temporary, "--context", "default"]
            listing = runner(
                cli
                + [
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    "label=com.docker.compose.project=" + project,
                    "--filter",
                    "label=app.owner=" + OWNER,
                    "--format",
                    "{{.ID}}",
                ],
                deadline=deadline,
            )
            identifiers = listing["stdout"].decode("ascii").splitlines()
            if len(identifiers) > MAX_CONTAINERS or len(
                set(identifiers)
            ) != len(identifiers):
                raise CaptureError("LOG_CAPTURE_CONTAINER_LIMIT")
            for identifier in identifiers:
                if not re.fullmatch(r"[0-9a-f]{64}", identifier):
                    raise CaptureError("LOG_CAPTURE_CONTAINER_INVALID")
                reply = runner(
                    cli
                    + [
                        "inspect",
                        "--format",
                        "{{json .Config.Labels}}",
                        identifier,
                    ],
                    deadline=deadline,
                )
                labels = json.loads(reply["stdout"])
                if not isinstance(labels, dict):
                    raise CaptureError("LOG_CAPTURE_LABELS_INVALID")
                service = labels.get("com.docker.compose.service")
                if (
                    labels.get("com.docker.compose.project") != project
                    or labels.get("app.owner") != OWNER
                    or service not in SERVICES
                ):
                    continue
                replica = labels.get(
                    "com.docker.compose.container-number", "once"
                )
                if replica != "once" and not re.fullmatch(
                    r"[1-9][0-9]{0,2}", replica
                ):
                    raise CaptureError("LOG_CAPTURE_REPLICA_INVALID")
                reply = runner(
                    cli
                    + [
                        "logs",
                        "--timestamps",
                        "--since",
                        since,
                        "--tail",
                        str(tail),
                        identifier,
                    ],
                    deadline=deadline,
                )
                identity = hashlib.sha256(
                    (project + "|" + identifier).encode()
                ).hexdigest()
                for record in capture_lines(
                    reply,
                    project=project,
                    alias=alias,
                    identity=identity,
                    service=service,
                    replica=replica,
                ):
                    capture_bytes += (
                        len(
                            json.dumps(
                                record,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode()
                        )
                        + 1
                    )
                    if capture_bytes > MAX_CAPTURE_BYTES:
                        raise CaptureError("LOG_CAPTURE_BYTE_LIMIT")
                    if len(records) >= MAX_LINES:
                        raise CaptureError("LOG_CAPTURE_EVENT_LIMIT")
                    records.append(record)
                containers += 1
        records.sort(
            key=lambda row: (
                row["timestamp"],
                row["source_identity"],
                row["stream"],
            )
        )
        payload = b"".join(
            (
                json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            ).encode()
            for row in records
        )
        if len(payload) > MAX_CAPTURE_BYTES:
            raise CaptureError("LOG_CAPTURE_BYTE_LIMIT")
        file_id, published = (
            publish(output, payload) if records else (None, False)
        )
        output.verify()
    return {
        "schema_version": 1,
        "status": "captured",
        "containers": containers,
        "events": len(records),
        "file_id": file_id,
        "published": published,
        "raw_private": True,
        "exactly_once": False,
    }


def main():
    """Read private settings only for an explicit operator invocation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--confirm-capture", action="store_true")
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("--tail", type=int, default=500)
    options = parser.parse_args()
    try:
        from settings import load

        result = capture(
            load(options.env_file),
            confirm=options.confirm_capture,
            minutes=options.minutes,
            tail=options.tail,
        )
    except (ValueError, OSError, UnicodeError):
        result = {
            "schema_version": 1,
            "status": "failed",
            "code": "LOG_CAPTURE_REVIEW_REQUIRED",
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "captured" else 1


if __name__ == "__main__":
    raise SystemExit(main())
