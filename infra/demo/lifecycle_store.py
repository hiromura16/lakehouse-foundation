"""Bounded, private lifecycle journal; leases never replay uncertain commands.

The web process records requests, not Docker commands. An independently started
operator claims them. A lost execution lease becomes unknown and needs explicit
operator reconciliation. POSIX locks serialize cooperating API workers.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import stat
import threading
import time
from pathlib import Path

from lifecycle_catalog import ACTIONS, group_for, profile_set

MAX_BYTES = 262144
PLAN_TTL = 60
RUNNER_TTL = 30
LEASE_TTL = 300
ACTIVE = frozenset({"queued", "claimed", "running", "unknown"})
TERMINAL = frozenset({"succeeded", "failed"})
SAFE_ERRORS = frozenset(
    {
        "INITIAL_SETUP_REQUIRED",
        "APPROVAL_CHANGED",
        "MEMORY_BUDGET",
        "OPERATOR_AUTH_REQUIRED",
        "OPERATOR_COMMAND_FAILED",
        "STATE_UNAVAILABLE",
        "OPERATION_UNKNOWN",
        "OPERATOR_RECONCILED",
        "PROFILE_SAVE_FAILED",
    }
)


class LifecycleError(ValueError):
    """Carry only fixed error codes, never subprocess or connection errors."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def public_settings_digest(public):
    """Bind plans to non-secret settings and secret-presence flags."""
    return hashlib.sha256(
        canonical(
            {
                "values": {
                    k: v
                    for k, v in public["values"].items()
                    if k != "COMPOSE_PROFILES"
                },
                "configured_secrets": sorted(public["configured_secrets"]),
            }
        )
    ).hexdigest()


def open_directory(path, *, create=False):
    """Walk without symlink following; create only the selected leaf."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise LifecycleError("STATE_UNAVAILABLE")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(path.parts[1:]):
            if create and index == len(path.parts) - 2:
                parent = os.fstat(fd)
                if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
                    raise LifecycleError("STATE_UNAVAILABLE")
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise LifecycleError("STATE_UNAVAILABLE")
        return fd
    except Exception:
        os.close(fd)
        raise


def private_bytes(fd, name, *, maximum=MAX_BYTES):
    """Read only an owned bounded regular file through its directory FD."""
    handle = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
    )
    with os.fdopen(handle, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > maximum
        ):
            raise LifecycleError("STATE_UNAVAILABLE")
        result = source.read(maximum + 1)
        if len(result) > maximum:
            raise LifecycleError("STATE_UNAVAILABLE")
        return result


class Journal:
    """Keep one runner and one outstanding operation in an atomic JSON file."""

    def __init__(self, directory, *, clock=time.time):
        self.directory = Path(directory)
        self.clock = clock
        self.lock = threading.RLock()

    @staticmethod
    def empty():
        return {
            "version": 1,
            "runner": None,
            "plans": {},
            "operations": {},
            "audit": [],
        }

    @contextlib.contextmanager
    def transaction(self, *, create=True):
        with self.lock:
            try:
                fd = open_directory(self.directory, create=create)
            except FileNotFoundError:
                if not create:
                    yield self.empty()
                    return
                raise LifecycleError("STATE_UNAVAILABLE") from None
            lock_fd = None
            temporary = None
            try:
                lock_fd = os.open(
                    "journal.lock",
                    os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=fd,
                )
                info = os.fstat(lock_fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise LifecycleError("STATE_UNAVAILABLE")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    data = json.loads(private_bytes(fd, "journal.json"))
                except FileNotFoundError:
                    data = self.empty()
                if (
                    not isinstance(data, dict)
                    or data.get("version") != 1
                    or not isinstance(data.get("operations"), dict)
                    or not isinstance(data.get("plans"), dict)
                    or len(data["operations"]) > 100
                    or len(data["plans"]) > 50
                ):
                    raise LifecycleError("STATE_UNAVAILABLE")
                before = canonical(data)
                self.expire(data)
                yield data
                content = canonical(data)
                if len(content) > MAX_BYTES:
                    raise LifecycleError("STATE_UNAVAILABLE")
                # Also persist lease expiry on reads of an existing journal.
                if create or content != before:
                    temporary = ".journal-" + secrets.token_hex(12)
                    handle = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=fd,
                    )
                    with os.fdopen(handle, "wb") as output:
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(
                        temporary, "journal.json", src_dir_fd=fd, dst_dir_fd=fd
                    )
                    temporary = None
                    os.fsync(fd)
            except (OSError, TypeError, KeyError, json.JSONDecodeError):
                raise LifecycleError("STATE_UNAVAILABLE") from None
            finally:
                if temporary:
                    os.unlink(temporary, dir_fd=fd)
                if lock_fd is not None:
                    os.close(lock_fd)
                os.close(fd)

    def expire(self, data):
        now = self.clock()
        data["plans"] = {
            k: v for k, v in data["plans"].items() if v["expires_at"] >= now
        }
        for operation in data["operations"].values():
            if (
                operation["status"] in {"claimed", "running"}
                and operation.get("lease_expires_at", 0) < now
            ):
                operation.update(
                    status="unknown",
                    updated_at=now,
                    error={
                        "code": "OPERATION_UNKNOWN",
                        "message": "実行結果の確認が必要です。",
                    },
                )
            if (
                operation["status"] == "queued"
                and operation["created_at"] + LEASE_TTL < now
            ):
                operation.update(
                    status="failed",
                    updated_at=now,
                    error={
                        "code": "RUNNER_DISCONNECTED",
                        "message": "実行担当へ届きませんでした。",
                    },
                )

    def connected(self, data):
        runner = data["runner"]
        return bool(
            runner and runner["last_seen"] + RUNNER_TTL >= self.clock()
        )

    @staticmethod
    def active(data):
        return next(
            (o for o in data["operations"].values() if o["status"] in ACTIVE),
            None,
        )

    @staticmethod
    def operation_view(operation):
        keys = (
            "id",
            "service_id",
            "action",
            "group_id",
            "status",
            "created_at",
            "updated_at",
            "error",
            "result",
        )
        return {key: operation[key] for key in keys}

    def event(self, data, code, operation=None):
        """Audit a fixed actor category and action, without request bodies."""
        data["audit"].append(
            {
                "at": self.clock(),
                "actor": "poc-owner",
                "event": code,
                "operation_id": operation,
            }
        )
        data["audit"] = data["audit"][-100:]

    def plan(self, data, service_id, action, public):
        if action not in ACTIONS:
            raise LifecycleError("SERVICE_UNSUPPORTED")
        try:
            group = group_for(service_id)
        except ValueError as error:
            raise LifecycleError(str(error)) from None
        if not self.connected(data):
            raise LifecycleError("RUNNER_DISCONNECTED")
        if self.active(data):
            raise LifecycleError("OPERATION_BUSY")
        runner = data["runner"]
        if runner["config_digest"] != public_settings_digest(public):
            raise LifecycleError("RUNNER_SCOPE_CHANGED")
        if group.id not in runner["groups"]:
            raise LifecycleError("INITIAL_SETUP_REQUIRED")
        reason = runner["blocked_groups"].get(group.id)
        if reason and not (reason == "MEMORY_BUDGET" and action == "stop"):
            raise LifecycleError(reason)
        profiles = profile_set(public["values"])
        if (
            action == "stop"
            and group.id == "compute"
            and profiles & {"sail-lab", "notebooks"}
        ):
            raise LifecycleError("PROFILE_DEPENDENCY")
        if (
            action == "stop"
            and group.id == "monitoring"
            and "openobserve-lab" in profiles
        ):
            raise LifecycleError("PROFILE_DEPENDENCY")
        if action == "start" and group.id in {"sail-lab", "openobserve-lab"}:
            other = "openobserve-lab" if group.id == "sail-lab" else "sail-lab"
            if other in profiles:
                raise LifecycleError("MEMORY_BUDGET")
        identifier = secrets.token_hex(12)
        affected = group.starts if action == "start" else group.daemons
        if group.id == "openobserve-lab":
            affected = (*affected, "vector")
        result = {
            "id": identifier,
            "service_id": service_id,
            "action": action,
            "group_id": group.id,
            "affected_services": list(affected),
            "added_profiles": sorted(set(group.profiles) - profiles)
            if action == "start"
            else [],
            "warnings": [
                "GROUP_OPERATION",
                "VOLUMES_PRESERVED",
                "PROFILE_REDEPLOY_PENDING",
                "EXTERNAL_JOBS_NOT_ATOMICALLY_LOCKED",
            ],
            "confirmation": secrets.token_hex(16),
            "expires_at": self.clock() + PLAN_TTL,
            "manifest_digest": runner["manifest_digest"],
        }
        result["config_digest"] = public_settings_digest(public)
        if len(data["plans"]) >= 50:
            raise LifecycleError("STATE_UNAVAILABLE")
        data["plans"][identifier] = result
        return {
            key: value
            for key, value in result.items()
            if key != "config_digest"
        }

    def submit(self, data, plan_id, confirmation, key, public):
        existing = next(
            (
                o
                for o in data["operations"].values()
                if o["idempotency_key"] == key
            ),
            None,
        )
        if existing:
            if existing["plan_id"] != plan_id:
                raise LifecycleError("PLAN_CHANGED")
            return self.operation_view(existing)
        plan = data["plans"].get(plan_id)
        if not plan or plan["expires_at"] < self.clock():
            raise LifecycleError("PLAN_EXPIRED")
        if not secrets.compare_digest(plan["confirmation"], confirmation):
            raise LifecycleError("CONFIRMATION_REQUIRED")
        if not self.connected(data):
            raise LifecycleError("RUNNER_DISCONNECTED")
        if self.active(data):
            raise LifecycleError("OPERATION_BUSY")
        runner = data["runner"]
        if plan["manifest_digest"] != runner["manifest_digest"] or plan[
            "config_digest"
        ] != public_settings_digest(public):
            raise LifecycleError("PLAN_CHANGED")
        if len(data["operations"]) >= 100:
            oldest = next(
                (
                    key
                    for key, item in data["operations"].items()
                    if item["status"] in TERMINAL
                ),
                None,
            )
            if oldest is None:
                raise LifecycleError("STATE_UNAVAILABLE")
            del data["operations"][oldest]
        identifier = secrets.token_hex(12)
        operation = {
            "id": identifier,
            "plan_id": plan_id,
            "idempotency_key": key,
            "service_id": plan["service_id"],
            "group_id": plan["group_id"],
            "action": plan["action"],
            "manifest_digest": plan["manifest_digest"],
            "status": "queued",
            "created_at": self.clock(),
            "updated_at": self.clock(),
            "error": None,
            "result": None,
            "lease_id": None,
        }
        data["operations"][identifier] = operation
        self.event(data, "operation_accepted", identifier)
        return self.operation_view(operation)

    def claim(self, data, runner_id):
        runner = data["runner"]
        if not self.connected(data) or runner["runner_id"] != runner_id:
            raise LifecycleError("RUNNER_DISCONNECTED")
        operation = self.active(data)
        if not operation or operation["status"] != "queued":
            return None
        if operation["manifest_digest"] != runner["manifest_digest"]:
            raise LifecycleError("PLAN_CHANGED")
        operation.update(
            status="claimed",
            updated_at=self.clock(),
            runner_id=runner_id,
            lease_id=secrets.token_hex(16),
            lease_expires_at=self.clock() + LEASE_TTL,
        )
        self.event(data, "operator_claimed", operation["id"])
        return {
            key: operation[key]
            for key in (
                "id",
                "service_id",
                "group_id",
                "action",
                "lease_id",
                "manifest_digest",
                "lease_expires_at",
            )
        }

    def runner_state(self, data, payload):
        old = data["runner"]
        if (
            old
            and self.connected(data)
            and old["runner_id"] != payload["runner_id"]
        ):
            raise LifecycleError("OPERATION_BUSY")
        if (
            old
            and self.active(data)
            and old["manifest_digest"] != payload["manifest_digest"]
        ):
            raise LifecycleError("RUNNER_SCOPE_CHANGED")
        data["runner"] = {**payload, "last_seen": self.clock()}

    def finish(self, data, payload):
        operation = data["operations"].get(payload["operation_id"])
        if (
            not operation
            or operation["status"] not in {"claimed", "running"}
            or operation["lease_id"] != payload["lease_id"]
        ):
            raise LifecycleError("OPERATION_UNKNOWN")
        status = payload["status"]
        code = payload.get("error_code")
        if status != "succeeded" and code not in SAFE_ERRORS:
            raise LifecycleError("STATE_UNAVAILABLE")
        operation.update(
            status=status,
            updated_at=self.clock(),
            error=None
            if status == "succeeded"
            else {"code": code, "message": "管理処理を確認してください。"},
            result={"observed_state": payload["observed_state"]},
        )
        self.event(data, "operator_" + status, operation["id"])
        return self.operation_view(operation)
