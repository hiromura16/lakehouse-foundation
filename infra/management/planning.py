"""Validate whole-PoC lifecycle plans without contacting Docker or a database.

Approvals come from the separately enrolled host runner, never from a browser.
Plans deliberately confer no execution authority: the independent manager must
reserve, confirm and journal them before the runner may execute any step.
The existing optional-service catalog and its protected core remain unchanged.
"""

import hashlib
import json
import re
from dataclasses import dataclass

POC_IDS = frozenset({"foundation", "vision"})
MANAGEMENT_PROJECTS = frozenset(
    {"dokploy", "poc-manager", "lakehouse-control", "lakehouse-manager"}
)
COMPLETED_BOOTSTRAPS = frozenset(
    {"catalog-migrate", "superset-init", "spark-volumes-init", "cdc-register"}
)
EXCLUDED_SERVICES = frozenset(
    {
        "catalog-migrate",
        "superset-init",
        "spark-volumes-init",
        "spark-check",
        "sail-benchmark",
        "cdc-register",
        "dokploy",
        "dokploy-postgres",
        "dokploy-traefik",
        "poc-manager",
        "poc-manager-api",
        "poc-manager-ui",
        "poc-manager-runner",
        "shared-sqlserver",
    }
)


class PlanError(ValueError):
    """Fixed non-secret error codes only."""


def require(ok, code):
    if not ok:
        raise PlanError(code)


@dataclass(frozen=True)
class Member:
    """A previously approved existing daemon; hashes exclude secret values."""

    service: str
    container_id: str
    image_id: str
    mount_identity_digest: str
    runtime_identity_digest: str
    dependencies: tuple[str, ...] = ()
    writable_storage_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Approval:
    """Operator-owned input, never accepted from a web plan request."""

    poc_id: str
    project: str
    members: tuple[Member, ...]
    completed_bootstraps: tuple[str, ...] = ()
    allow_interruption: bool = False


def canonical(value):
    """Create a deterministic digest input; non-finite JSON is never valid."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def hexadecimal(value, *, prefix=""):
    """Accept only full identities, never short IDs or mutable image tags."""
    return (
        type(value) is str
        and re.fullmatch(re.escape(prefix) + r"[0-9a-f]{64}", value)
        is not None
    )


def validate_approvals(approvals):
    """Reject overlapping projects, containers and writable data stores.

    Public read-only config mounts may be shared. Writable storage identities
    must describe the actual underlying store, not merely its container path.
    Shared SQL is independently managed and never enrolled as a PoC daemon.
    """
    require(
        type(approvals) is dict and set(approvals) == POC_IDS,
        "APPROVAL_BAD",
    )
    projects, container_ids, writable_stores = set(), set(), set()
    for poc_id, approval in approvals.items():
        require(type(approval) is Approval, "APPROVAL_BAD")
        require(
            approval.poc_id == poc_id
            and type(approval.project) is str
            and re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", approval.project)
            is not None
            and approval.project not in MANAGEMENT_PROJECTS,
            "APPROVAL_BAD",
        )
        require(approval.project not in projects, "PROJECTS_OVERLAP")
        projects.add(approval.project)
        require(
            type(approval.members) is tuple
            and 0 < len(approval.members) <= 128
            and type(approval.completed_bootstraps) is tuple
            and type(approval.allow_interruption) is bool
            and all(
                type(name) is str and name in COMPLETED_BOOTSTRAPS
                for name in approval.completed_bootstraps
            )
            and len(set(approval.completed_bootstraps))
            == len(approval.completed_bootstraps),
            "APPROVAL_BAD",
        )
        project_stores, services = set(), set()
        for member in approval.members:
            require(type(member) is Member, "APPROVAL_BAD")
            require(
                type(member.service) is str
                and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", member.service)
                is not None
                and member.service not in EXCLUDED_SERVICES,
                "EXCLUDED_SERVICE",
            )
            require(member.service not in services, "APPROVAL_BAD")
            services.add(member.service)
            require(
                hexadecimal(member.container_id)
                and hexadecimal(member.image_id, prefix="sha256:")
                and hexadecimal(member.mount_identity_digest)
                and hexadecimal(member.runtime_identity_digest),
                "APPROVAL_BAD",
            )
            require(
                member.container_id not in container_ids, "PROJECTS_OVERLAP"
            )
            container_ids.add(member.container_id)
            require(
                type(member.dependencies) is tuple
                and all(type(name) is str for name in member.dependencies)
                and len(set(member.dependencies)) == len(member.dependencies)
                and type(member.writable_storage_ids) is tuple
                and all(
                    hexadecimal(name) for name in member.writable_storage_ids
                ),
                "APPROVAL_BAD",
            )
            project_stores.update(member.writable_storage_ids)
        require(
            not project_stores & writable_stores, "WRITABLE_STORES_OVERLAP"
        )
        writable_stores.update(project_stores)
        order(approval.members)


def inspected(approval, inventory):
    """Reject unknown, missing, recreated or mislabeled members."""
    require(type(inventory) is list and len(inventory) <= 128, "INVENTORY_BAD")
    expected = {item.service: item for item in approval.members}
    require(len(expected) == len(approval.members) > 0, "APPROVAL_BAD")
    require(not expected.keys() & EXCLUDED_SERVICES, "EXCLUDED_SERVICE")
    actual = {}
    for row in inventory:
        require(type(row) is dict, "INVENTORY_BAD")
        require(
            set(row)
            == {
                "service",
                "container_id",
                "image_id",
                "mount_identity_digest",
                "runtime_identity_digest",
                "project",
                "owner",
                "kind",
                "state",
            },
            "PROJECTION_BAD",
        )
        service = row["service"]
        require(type(service) is str and service not in actual, "MEMBER_BAD")
        require(service in expected, "UNAPPROVED_MEMBER")
        item = expected[service]
        require(
            row["container_id"] == item.container_id
            and row["image_id"] == item.image_id
            and row["mount_identity_digest"] == item.mount_identity_digest
            and row["runtime_identity_digest"] == item.runtime_identity_digest
            and row["project"] == approval.project
            and row["owner"] == "lakehouse-foundation"
            and row["kind"] == "daemon"
            and type(row["state"]) is str
            and row["state"] in {"running", "stopped"},
            "IDENTITY_CHANGED",
        )
        actual[service] = row
    require(actual.keys() == expected.keys(), "INITIAL_SETUP_REQUIRED")
    return actual


def order(members):
    """Dependency order, deterministic within each tier."""
    remaining = {item.service: set(item.dependencies) for item in members}
    require(
        all(deps <= remaining.keys() for deps in remaining.values()),
        "DEPENDENCY_BAD",
    )
    result = []
    while remaining:
        ready = sorted(name for name, deps in remaining.items() if not deps)
        require(bool(ready), "DEPENDENCY_CYCLE")
        for name in ready:
            result.append(name)
            del remaining[name]
        for deps in remaining.values():
            deps.difference_update(ready)
    return result


def plan(
    approvals,
    request,
    inventory,
    *,
    admission_ready,
    workloads_idle,
    interruption_ack=False,
):
    """Return a validated preview; reservation and execution are separate.

    A real handler must also authenticate, reserve/lock both projects, bind a
    short TTL/confirmation/idempotency key, fsync intent before each call, and
    re-inspect immediately before every mutation. This pure sketch does none
    of those things and must never be used as execution authority by itself.
    """
    require(
        type(request) is dict
        and set(request) == {"poc_id", "action"}
        and type(request["poc_id"]) is str
        and type(request["action"]) is str
        and request["poc_id"] in POC_IDS
        and request["action"] in {"start", "stop", "restart"},
        "REQUEST_BAD",
    )
    require(type(interruption_ack) is bool, "REQUEST_BAD")
    validate_approvals(approvals)
    approval = approvals[request["poc_id"]]
    require(approval.poc_id == request["poc_id"], "APPROVAL_BAD")
    current = inspected(approval, inventory)
    action = request["action"]
    startup = order(approval.members)
    steps = []
    if action in {"stop", "restart"}:
        steps.extend(
            {"service": name, "action": "stop"}
            for name in reversed(startup)
            if current[name]["state"] == "running"
        )
    if action in {"start", "restart"}:
        steps.extend(
            {"service": name, "action": "start"}
            for name in startup
            if action == "restart" or current[name]["state"] == "stopped"
        )
    # A request that would change nothing needs no admission reservation.
    # Every actual transition still requires the runner's independent gates.
    interruption_possible = bool(steps) and workloads_idle is not True
    if steps:
        if interruption_possible:
            # A preview may describe an operator-authorized interruption.
            # API submission AND the host runner must require explicit ack.
            # This never permits kill, remove, unknown members or data loss
            # claims: normal Docker stop can terminate work after its grace.
            require(
                action in {"stop", "restart"} and approval.allow_interruption,
                "WORKLOAD_BUSY",
            )
        require(
            action == "stop"
            or type(admission_ready) is bool
            and admission_ready,
            "MEMORY_BUDGET",
        )
    # Public API: service/action/count only, never IDs or raw inspect.
    return {
        "schema_version": 1,
        "can_execute": False,
        "poc_id": approval.poc_id,
        "action": action,
        "requires_interruption_ack": interruption_possible,
        "interruption_acknowledged": (
            interruption_possible and interruption_ack
        ),
        "effect": (
            "graceful_stop_interruption_possible"
            if interruption_possible
            else "existing_containers_only"
        ),
        "snapshot_digest": hashlib.sha256(canonical(inventory)).hexdigest(),
        "steps": steps,
        "excluded_completed_bootstraps": list(approval.completed_bootstraps),
        "volume_deletion": False,
        "automatic_retry": False,
    }
