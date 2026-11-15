"""Private bounded requests, not privileged execution authority.

Only the separately enrolled runner owns the Docker manifest and execution
intent. Expired claims are unknown, never automatically replayed. This file
can be used by the unprivileged API and by the runner's separate private log.
"""

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .planning import (
    POC_IDS,
    Approval,
    Member,
    inspected,
    plan,
    validate_approvals,
)

LIMIT = 512 * 1024
TTL = 60
RUNNER_TTL = 30
LEASE = 300
CAPACITY = 128
CHECKOUT_ROOT = Path(__file__).absolute().parents[2]
ACTIVE = {"queued", "claimed", "running", "unknown"}
STATUSES = ACTIVE | {"succeeded", "failed"}
ERRORS = {
    "OUTCOME_UNKNOWN",
    "RUNNER_ERROR",
    "APPROVAL_CHANGED",
    "IDENTITY_CHANGED",
    "MEMORY_BUDGET",
    "WORKLOAD_BUSY",
    "HEALTH_TIMEOUT",
    "DOCKER_FAILED",
    "DOCKER_PERMISSION_DENIED",
    "STATE_UNAVAILABLE",
    "OPERATOR_RECONCILED",
    "PLAN_EXPIRED",
}


class StateError(ValueError):
    """A fixed safe public code."""


def require(condition, code="STATE_UNAVAILABLE"):
    """Reject a failed contract with a fixed code, never input or exception
    details.
    """
    if not condition:
        raise StateError(code)


def canonical(value):
    """Serialize deterministic finite JSON for identity comparison."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def strict_json(raw):
    """Reject duplicate keys and non-finite JSON values."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    def invalid(_):
        raise StateError("STATE_UNAVAILABLE")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def private_directory(path, *, create=False):
    """No symlink traversal; only a selected leaf may be newly created."""
    path = Path(path)
    require(
        path != CHECKOUT_ROOT and CHECKOUT_ROOT not in path.parents,
        "PRIVATE_STATE_INSIDE_CHECKOUT",
    )
    require(
        path.is_absolute() and ".." not in path.parts and len(path.parts) > 1
    )
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(path.parts[1:], 1):
            parent = os.fstat(fd)
            # /tmp is useful for disposable tests. Its child must be private;
            # no other writable shared ancestor is accepted.
            sticky_tmp = (
                index == 2
                and path.parts[1] == "tmp"
                and parent.st_uid == 0
                and stat.S_IMODE(parent.st_mode) == 0o1777
            )
            require(
                sticky_tmp
                or parent.st_uid in {0, os.geteuid()}
                and not parent.st_mode & 0o022
            )
            if create and index == len(path.parts) - 1:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        require(
            info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700
        )
        return fd
    except BaseException:
        os.close(fd)
        raise


def private_read(directory, name, *, maximum=LIMIT):
    """Read a bounded, owned 0600 regular file without following links."""
    require(type(name) is str and re.fullmatch(r"[a-zA-Z0-9._-]+", name))
    fd = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
    )
    try:
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.geteuid()
            and stat.S_IMODE(before.st_mode) == 0o600
            and before.st_nlink == 1
            and 0 < before.st_size <= maximum
        )
        raw = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        require(
            len(raw) == before.st_size
            and all(
                getattr(before, key) == getattr(after, key)
                for key in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_uid",
                    "st_nlink",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            )
        )
        return raw
    finally:
        os.close(fd)


def exclusive(directory, name, value):
    """Create and fsync one new private JSON record; never overwrite."""
    raw = canonical(value)
    require(len(raw) <= LIMIT)
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    with os.fdopen(fd, "wb") as target:
        os.fchmod(target.fileno(), 0o600)
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())
    os.fsync(directory)


def approvals_data(approvals):
    """Serialize the validated approval dataclasses for private transport."""
    validate_approvals(approvals)
    return strict_json(
        canonical({key: asdict(value) for key, value in approvals.items()})
    )


def approvals_value(value):
    """Strict conversion of enrolled runner data, never browser fields."""
    require(type(value) is dict and set(value) == POC_IDS, "APPROVAL_CHANGED")
    result = {}
    try:
        for key, row in value.items():
            require(
                type(row) is dict
                and set(row)
                == {
                    "poc_id",
                    "project",
                    "members",
                    "completed_bootstraps",
                    "allow_interruption",
                },
                "APPROVAL_CHANGED",
            )
            require(type(row["members"]) is list, "APPROVAL_CHANGED")
            members = []
            for member in row["members"]:
                require(
                    type(member) is dict
                    and set(member)
                    == {
                        "service",
                        "container_id",
                        "image_id",
                        "mount_identity_digest",
                        "runtime_identity_digest",
                        "dependencies",
                        "writable_storage_ids",
                    },
                    "APPROVAL_CHANGED",
                )
                require(
                    type(member["dependencies"]) is list
                    and type(member["writable_storage_ids"]) is list,
                    "APPROVAL_CHANGED",
                )
                members.append(
                    Member(
                        **{
                            **member,
                            "dependencies": tuple(member["dependencies"]),
                            "writable_storage_ids": tuple(
                                member["writable_storage_ids"]
                            ),
                        }
                    )
                )
            require(
                type(row["completed_bootstraps"]) is list, "APPROVAL_CHANGED"
            )
            result[key] = Approval(
                row["poc_id"],
                row["project"],
                tuple(members),
                tuple(row["completed_bootstraps"]),
                allow_interruption=row["allow_interruption"],
            )
        validate_approvals(result)
    except (TypeError, KeyError):
        raise StateError("APPROVAL_CHANGED") from None
    return result


def report_value(value):
    """Validate a bounded runner report with fixed identities and booleans."""
    require(
        type(value) is dict
        and set(value)
        == {
            "runner_id",
            "manifest_digest",
            "approvals",
            "inventories",
            "admission_ready",
            "workloads_idle",
            "observed_at",
        },
        "RUNNER_REPORT_INVALID",
    )
    for field, length in (("runner_id", 32), ("manifest_digest", 64)):
        require(
            type(value[field]) is str
            and re.fullmatch("[a-f0-9]{" + str(length) + "}", value[field]),
            "RUNNER_REPORT_INVALID",
        )
    approvals = approvals_value(value["approvals"])
    require(
        type(value["inventories"]) is dict
        and set(value["inventories"]) == POC_IDS
        and type(value["admission_ready"]) is bool
        and type(value["workloads_idle"]) is dict
        and set(value["workloads_idle"]) == POC_IDS
        and all(type(x) is bool for x in value["workloads_idle"].values())
        and type(value["observed_at"]) in {int, float}
        and math.isfinite(value["observed_at"]),
        "RUNNER_REPORT_INVALID",
    )
    for key in POC_IDS:
        inspected(approvals[key], value["inventories"][key])
    return value


def operation_view(row):
    """Return only public operation fields, with no credentials or container
    IDs.
    """
    return {
        key: row[key]
        for key in (
            "id",
            "poc_id",
            "action",
            "status",
            "created_at",
            "updated_at",
            "completed_steps",
            "step_count",
            "error_code",
            "interruption_ack",
            "interruption_possible",
        )
    }


class Journal:
    """One outstanding operation across both PoCs; no uncertain replay."""

    def __init__(self, path, *, clock=time.time):
        """Bind explicit inputs; importing or constructing this object does
        not grant privileges.
        """
        self.path, self.clock = Path(path), clock
        self.lock = threading.RLock()

    @contextlib.contextmanager
    def transaction(self):
        """Lock and atomically persist one bounded API journal transaction."""
        with self.lock:
            directory = private_directory(self.path)
            lock = temporary = None
            try:
                lock = os.open(
                    "journal.lock",
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                info = os.fstat(lock)
                require(
                    stat.S_ISREG(info.st_mode)
                    and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) == 0o600
                    and info.st_nlink == 1
                )
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    data = strict_json(private_read(directory, "journal.json"))
                except FileNotFoundError:
                    data = {
                        "schema_version": 1,
                        "runner": None,
                        "plans": {},
                        "operations": {},
                        "keys": {},
                    }
                require(
                    type(data) is dict
                    and set(data)
                    == {
                        "schema_version",
                        "runner",
                        "plans",
                        "operations",
                        "keys",
                    }
                    and type(data["schema_version"]) is int
                    and data["schema_version"] == 1
                )
                for key in ("plans", "operations", "keys"):
                    require(
                        type(data[key]) is dict and len(data[key]) <= CAPACITY
                    )
                now = self.clock()
                for row in data["operations"].values():
                    require(
                        type(row) is dict and row.get("status") in STATUSES
                    )
                    if (
                        row["status"] in {"claimed", "running"}
                        and row["lease_until"] <= now
                    ):
                        row.update(
                            status="unknown",
                            error_code="OUTCOME_UNKNOWN",
                            updated_at=now,
                        )
                data["plans"] = {
                    key: row
                    for key, row in data["plans"].items()
                    if row["expires_at"] > now
                }
                original = canonical(data)
                yield data
                raw = canonical(data)
                require(len(raw) <= LIMIT)
                # Persist expiry transitions even when a read changed no field.
                temporary = ".journal-" + secrets.token_hex(16)
                exclusive(directory, temporary, data)
                os.replace(
                    temporary,
                    "journal.json",
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                )
                temporary = None
                os.fsync(directory)
                del original
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except OSError:
                        pass
                if lock is not None:
                    os.close(lock)
                os.close(directory)

    def connected(self, data):
        """Require a fresh runner observation within the short report
        lifetime.
        """
        value = data["runner"]
        return (
            bool(value)
            and 0 <= self.clock() - value["observed_at"] <= RUNNER_TTL
        )

    @staticmethod
    def active(data):
        """Find the single queued, executing or unresolved global operation."""
        return next(
            (x for x in data["operations"].values() if x["status"] in ACTIVE),
            None,
        )

    def report(self, data, value):
        """Publish current validated metadata and admission observations to
        the manager.
        """
        report_value(value)
        require(
            0 <= self.clock() - value["observed_at"] <= RUNNER_TTL,
            "RUNNER_REPORT_STALE",
        )
        active = self.active(data)
        if active:
            require(
                active["runner_id"] == value["runner_id"]
                and active["manifest_digest"] == value["manifest_digest"],
                "OPERATION_UNKNOWN",
            )
        data["runner"] = value

    def create_plan(self, data, poc_id, action, actor):
        """Create a short, actor-bound preview from fresh enrolled
        observations.
        """
        require(self.connected(data), "RUNNER_OFFLINE")
        require(self.active(data) is None, "OPERATION_BUSY")
        require(len(data["plans"]) < CAPACITY, "STATE_CAPACITY")
        report = data["runner"]
        preview = plan(
            approvals_value(report["approvals"]),
            {"poc_id": poc_id, "action": action},
            report["inventories"].get(poc_id),
            admission_ready=report["admission_ready"],
            workloads_idle=report["workloads_idle"].get(poc_id),
        )
        identifier, confirmation = secrets.token_hex(12), secrets.token_hex(16)
        data["plans"][identifier] = {
            **preview,
            "plan_id": identifier,
            "confirmation": confirmation,
            "expires_at": self.clock() + TTL,
            "actor": actor,
            "runner_id": report["runner_id"],
            "manifest_digest": report["manifest_digest"],
        }
        return {
            key: value
            for key, value in data["plans"][identifier].items()
            if key not in {"actor", "runner_id", "manifest_digest"}
        }

    def submit(
        self,
        data,
        identifier,
        confirmation,
        key,
        actor,
        interruption_ack=False,
    ):
        """Confirm a preview and bind its request key before queueing one
        operation.
        """
        require(
            type(key) is str and re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key),
            "REQUEST_BAD",
        )
        key_hash = hashlib.sha256((actor + "\0" + key).encode()).hexdigest()
        if key_hash in data["keys"]:
            row = data["operations"][data["keys"][key_hash]]
            require(row["plan_id"] == identifier, "IDEMPOTENCY_CONFLICT")
            return operation_view(row)
        require(self.connected(data), "RUNNER_OFFLINE")
        require(self.active(data) is None, "OPERATION_BUSY")
        require(len(data["operations"]) < CAPACITY, "STATE_CAPACITY")
        saved = data["plans"].get(identifier)
        require(
            saved
            and saved["actor"] == actor
            and saved["expires_at"] > self.clock()
            and secrets.compare_digest(saved["confirmation"], confirmation),
            "PLAN_EXPIRED",
        )
        require(
            type(interruption_ack) is bool
            and (
                not saved.get("requires_interruption_ack") or interruption_ack
            ),
            "INTERRUPTION_ACK_REQUIRED",
        )
        require(
            saved["manifest_digest"] == data["runner"]["manifest_digest"]
            and saved["runner_id"] == data["runner"]["runner_id"],
            "APPROVAL_CHANGED",
        )
        identifier2, now = secrets.token_hex(12), self.clock()
        row = {
            "id": identifier2,
            "plan_id": identifier,
            "poc_id": saved["poc_id"],
            "action": saved["action"],
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "step_count": len(saved["steps"]),
            "completed_steps": 0,
            "error_code": None,
            "actor": actor,
            "runner_id": saved["runner_id"],
            "manifest_digest": saved["manifest_digest"],
            "snapshot_digest": saved["snapshot_digest"],
            "lease_id": None,
            "lease_until": 0,
            "interruption_ack": interruption_ack,
            "interruption_possible": bool(
                saved.get("requires_interruption_ack")
            ),
        }
        data["operations"][identifier2], data["keys"][key_hash] = (
            row,
            identifier2,
        )
        del data["plans"][identifier]
        return operation_view(row)

    def claim(self, data, runner_id, manifest_digest):
        """Lease a queued operation once; expiry does not replay uncertain
        work.
        """
        row = self.active(data)
        if not row or row["status"] != "queued":
            return None
        require(
            self.connected(data)
            and row["runner_id"] == runner_id
            and row["manifest_digest"] == manifest_digest,
            "APPROVAL_CHANGED",
        )
        if self.clock() - row["created_at"] > TTL:
            row.update(
                status="failed",
                error_code="PLAN_EXPIRED",
                updated_at=self.clock(),
            )
            return None
        row.update(
            status="claimed",
            lease_id=secrets.token_hex(16),
            lease_until=self.clock() + LEASE,
            updated_at=self.clock(),
        )
        return {
            key: row[key]
            for key in (
                "id",
                "poc_id",
                "action",
                "manifest_digest",
                "snapshot_digest",
                "lease_id",
                "step_count",
                "interruption_ack",
            )
        }

    def heartbeat(self, data, runner_id, operation_id, lease_id):
        """Extend only the exact live runner lease."""
        row = data["operations"].get(operation_id)
        require(
            row
            and row["status"] in {"claimed", "running"}
            and row["runner_id"] == runner_id
            and row["lease_id"] == lease_id,
            "OPERATION_UNKNOWN",
        )
        row.update(
            status="running",
            lease_until=self.clock() + LEASE,
            updated_at=self.clock(),
        )

    def reconcile(self, data, runner_id, operation_id, lease_id):
        """Record explicit reconciliation without an execution retry."""
        row = data["operations"].get(operation_id)
        require(
            row
            and row["status"]
            in {"claimed", "running", "unknown", "succeeded", "failed"}
            and row["runner_id"] == runner_id
            and row["lease_id"] == lease_id
            and self.connected(data)
            and data["runner"]["runner_id"] == runner_id,
            "OPERATION_UNKNOWN",
        )
        if row["status"] in {"succeeded", "failed"}:
            return operation_view(row)
        row.update(
            status="failed",
            error_code="OPERATOR_RECONCILED",
            updated_at=self.clock(),
        )
        return operation_view(row)

    def finish(self, data, runner_id, value):
        """Record a safe terminal or unknown result."""
        row = data["operations"].get(value["operation_id"])
        require(
            row
            and row["runner_id"] == runner_id
            and row["status"] in {"claimed", "running"}
            and row["lease_id"] == value["lease_id"],
            "OPERATION_UNKNOWN",
        )
        require(
            value["status"] in {"succeeded", "failed", "unknown"}
            and type(value["completed_steps"]) is int
            and 0 <= value["completed_steps"] <= row["step_count"]
            and (value["error_code"] is None or value["error_code"] in ERRORS)
            and (
                value["status"] != "succeeded"
                or value["completed_steps"] == row["step_count"]
            ),
            "RESULT_INVALID",
        )
        row.update(
            status=value["status"],
            completed_steps=value["completed_steps"],
            error_code=value["error_code"],
            updated_at=self.clock(),
        )
        return operation_view(row)
