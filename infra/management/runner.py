"""Separately installed host runner; no browser-supplied Docker authority.

The web API only queues logical requests. This runner re-plans from its own
root-private enrollment and current metadata before every fixed container
start/stop. Installation, deployment, build, removal, SQL and network changes
are deliberately absent. A lost response is unknown and is never retried.
"""

import fcntl
import hashlib
import ipaddress
import os
import re
import secrets
import stat
import subprocess
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx

from .journal import (
    approvals_data,
    approvals_value,
    canonical,
    exclusive,
    private_directory,
    private_read,
    strict_json,
)
from .planning import (
    COMPLETED_BOOTSTRAPS,
    EXCLUDED_SERVICES,
    POC_IDS,
    Approval,
    Member,
    hexadecimal,
    inspected,
    plan,
    validate_approvals,
)

DOCKER = [
    "/usr/bin/docker",
    "--config",
    "/nonexistent/poc-manager-config",
    "--host",
    "unix:///var/run/docker.sock",
]
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


def label_format(name):
    """Build a fixed Go-template label projection from a source-owned name."""
    return (
        '{{if .Config.Labels}}{{json (index .Config.Labels "'
        + name
        + '")}}{{else}}null{{end}}'
    )


PROJECTION = (
    '{"id":{{json .Id}},"image":{{json .Image}},'
    '"project":' + label_format("com.docker.compose.project") + ","
    '"service":' + label_format("com.docker.compose.service") + ","
    '"owner":' + label_format("app.owner") + ","
    '"oneoff":' + label_format("com.docker.compose.oneoff") + ","
    '"state":{{json .State.Status}},"exit_code":{{json .State.ExitCode}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}'
    '{{else}}null{{end}},"mounts":{{json .Mounts}},'
    '"runtime":{"memory":{{json .HostConfig.Memory}},'
    '"cpus":{{json .HostConfig.NanoCpus}},"shm":{{json .HostConfig.ShmSize}},'
    '"privileged":{{json .HostConfig.Privileged}},'
    '"readonly":{{json .HostConfig.ReadonlyRootfs}},'
    '"cap_add":{{json .HostConfig.CapAdd}},'
    '"cap_drop":{{json .HostConfig.CapDrop}},'
    '"security":{{json .HostConfig.SecurityOpt}},'
    '"pid":{{json .HostConfig.PidMode}},'
    '"network":{{json .HostConfig.NetworkMode}},'
    '"devices":{{json .HostConfig.Devices}},'
    '"sysctls":{{json .HostConfig.Sysctls}},"user":{{json .Config.User}}},'
    '"networks":{{json .NetworkSettings.Networks}}}'
)


class RunnerError(ValueError):
    """Only fixed codes are passed to the web journal."""


def require(condition, code="APPROVAL_CHANGED"):
    """Reject a failed contract with a fixed code, never input or exception
    details.
    """
    if not condition:
        raise RunnerError(code)


def digest(value):
    """Hash canonical non-secret identity metadata."""
    return hashlib.sha256(canonical(value)).hexdigest()


def private_json(path):
    """Read a protected bounded JSON file through its private directory FD."""
    path = Path(path)
    directory = private_directory(path.parent)
    try:
        return strict_json(private_read(directory, path.name))
    finally:
        os.close(directory)


def manifest_value(value):
    """Validate a root enrollment; no web input may replace this authority."""
    require(
        type(value) is dict
        and set(value)
        == {
            "schema_version",
            "runner_id",
            "approvals",
            "excluded",
            "maintenance",
            "independent_storage_verified",
        }
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1
    )
    require(
        type(value["runner_id"]) is str
        and re.fullmatch(r"[a-f0-9]{32}", value["runner_id"])
    )
    approvals = approvals_value(value["approvals"])
    require(
        value["independent_storage_verified"] is True,
        "STORAGE_SEPARATION_APPROVAL_REQUIRED",
    )
    require(
        type(value["excluded"]) is dict and set(value["excluded"]) == POC_IDS
    )
    require(
        type(value["maintenance"]) is dict
        and set(value["maintenance"]) == POC_IDS
    )
    all_ids = {m.container_id for a in approvals.values() for m in a.members}
    for poc_id in POC_IDS:
        require(
            type(value["excluded"][poc_id]) is list
            and len(value["excluded"][poc_id]) <= 32
        )
        for row in value["excluded"][poc_id]:
            require(
                type(row) is dict
                and set(row) == {"id", "service", "identity"}
                and hexadecimal(row["id"])
                and row["id"] not in all_ids
                and row["service"] in EXCLUDED_SERVICES
                and row["service"]
                not in {
                    "dokploy",
                    "dokploy-postgres",
                    "dokploy-traefik",
                    "shared-sqlserver",
                }
                and hexadecimal(row["identity"])
            )
            all_ids.add(row["id"])
        item = value["maintenance"][poc_id]
        require(
            type(item) is dict
            and set(item) == {"token_file", "reservation_id", "network"}
            and type(item["token_file"]) is str
            and Path(item["token_file"]).is_absolute()
            and ".." not in Path(item["token_file"]).parts
            and hexadecimal(item["reservation_id"])
            and type(item["network"]) is str
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", item["network"])
        )
    return value


def mount_projection(rows):
    """Normalize mount identity metadata without reading mounted contents."""
    require(type(rows) is list and len(rows) <= 64)
    result = []
    for row in rows:
        require(
            type(row) is dict
            and row.get("Type") in {"bind", "volume", "tmpfs"}
            and type(row.get("Destination")) is str
            and type(row.get("RW")) is bool
        )
        selected = {
            key: row.get(key)
            for key in (
                "Type",
                "Name",
                "Source",
                "Destination",
                "RW",
                "Propagation",
                "Mode",
            )
        }
        require(
            all(
                value is None or type(value) in {str, bool}
                for value in selected.values()
            )
        )
        result.append(selected)
    return sorted(result, key=lambda r: r["Destination"])


def metadata_value(value, identifier, approval):
    """Validate a full-ID projection against its project."""
    require(
        type(value) is dict
        and set(value)
        == {
            "id",
            "image",
            "project",
            "service",
            "owner",
            "oneoff",
            "state",
            "exit_code",
            "health",
            "mounts",
            "runtime",
            "networks",
        }
    )
    require(
        value["id"] == identifier
        and hexadecimal(identifier)
        and hexadecimal(value["image"], prefix="sha256:")
        and value["project"] == approval.project
        and value["owner"] == "lakehouse-foundation"
        and type(value["service"]) is str
        and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value["service"])
        and value["state"] in {"running", "exited", "created"}
        and type(value["exit_code"]) is int
        and value["health"] in {None, "starting", "healthy", "unhealthy"}
        and type(value["runtime"]) is dict
        and type(value["networks"]) is dict
    )
    mounts = mount_projection(value["mounts"])
    return {
        "service": value["service"],
        "container_id": identifier,
        "image_id": value["image"],
        "project": approval.project,
        "owner": value["owner"],
        "kind": "daemon",
        "state": "running" if value["state"] == "running" else "stopped",
        "mount_identity_digest": digest(mounts),
        "runtime_identity_digest": digest(value["runtime"]),
    }


class Docker:
    """Project metadata and start/stop only; no deployment or removal
    interface.
    """

    def __init__(self, run=subprocess.run):
        """Bind explicit inputs; importing or constructing this object does
        not grant privileges.
        """
        self.run = run

    def command(self, arguments, *, timeout=30):
        """Run a fixed Docker CLI request once with a clean environment and
        bounded wait.
        """
        try:
            value = self.run(
                DOCKER + arguments,
                env=ENV,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RunnerError("OUTCOME_UNKNOWN") from None
        except OSError:
            raise RunnerError("DOCKER_FAILED") from None
        require(
            len(value.stdout) <= 2 * 1024**2 and len(value.stderr) <= 65536,
            "DOCKER_FAILED",
        )
        if value.returncode:
            denied = any(
                x in value.stderr[:65536].lower()
                for x in (b"permission denied", b"not permitted", b"forbidden")
            )
            raise RunnerError(
                "DOCKER_PERMISSION_DENIED" if denied else "DOCKER_FAILED"
            )
        return value.stdout

    def ids(self, project):
        """Read full container IDs for one already validated project label."""
        raw = self.command(
            [
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                "label=com.docker.compose.project=" + project,
            ]
        )
        values = raw.decode("ascii").splitlines()
        require(
            len(values) <= 160
            and len(set(values)) == len(values)
            and all(hexadecimal(x) for x in values)
        )
        return values

    def inspect(self, identifier):
        """Read only the fixed metadata projection for a full container ID."""
        require(hexadecimal(identifier))
        return strict_json(
            self.command(
                ["container", "inspect", "--format", PROJECTION, identifier]
            )
        )

    def snapshot(self, manifest):
        """Verify both projects against frozen resident and excluded
        identities.
        """
        approvals = approvals_value(manifest["approvals"])
        inventories, raw_members = {}, {}
        for poc_id, approval in approvals.items():
            expected = {m.container_id: m for m in approval.members}
            excluded = {r["id"]: r for r in manifest["excluded"][poc_id]}
            ids = self.ids(approval.project)
            require(set(ids) == expected.keys() | excluded.keys())
            rows, raw = [], {}
            for identifier in ids:
                value = self.inspect(identifier)
                selected = metadata_value(value, identifier, approval)
                if identifier in excluded:
                    saved = excluded[identifier]
                    require(
                        value["service"] == saved["service"]
                        and digest(
                            {
                                key: selected[key]
                                for key in selected
                                if key != "state"
                            }
                        )
                        == saved["identity"]
                    )
                    require(value["state"] != "running", "WORKLOAD_BUSY")
                    if value["service"] in approval.completed_bootstraps:
                        require(
                            value["state"] == "exited"
                            and value["exit_code"] == 0,
                            "INITIAL_SETUP_REQUIRED",
                        )
                else:
                    require(value["oneoff"] in {None, "False", "false"})
                    rows.append(selected)
                    raw[value["service"]] = value
            rows.sort(key=lambda r: r["service"])
            inspected(approval, rows)
            inventories[poc_id], raw_members[poc_id] = rows, raw
            require(set(self.ids(approval.project)) == set(ids))
        return inventories, raw_members

    def transition(self, identifier, action):
        """Start or gracefully stop one approved existing full-ID container."""
        require(hexadecimal(identifier) and action in {"start", "stop"})
        self.command(["container", action, identifier], timeout=45)


def enroll(spec, docker):
    """Create a candidate from an explicit operator spec; never execute it.

    The caller must read a root-private spec and save the returned manifest
    exclusively. Web requests never call enrollment or provide its projects.
    """
    require(
        type(spec) is dict
        and set(spec)
        == {
            "schema_version",
            "runner_id",
            "pocs",
            "independent_storage_verified",
        }
        and type(spec["schema_version"]) is int
        and spec["schema_version"] == 1
        and type(spec["runner_id"]) is str
        and re.fullmatch(r"[a-f0-9]{32}", spec["runner_id"])
        and type(spec["pocs"]) is dict
        and set(spec["pocs"]) == POC_IDS
    )
    require(
        spec["independent_storage_verified"] is True,
        "STORAGE_SEPARATION_APPROVAL_REQUIRED",
    )
    approvals, excluded, maintenance = {}, {}, {}
    writable = []
    for poc_id, config in spec["pocs"].items():
        require(
            type(config) is dict
            and set(config)
            == {
                "project",
                "services",
                "completed_bootstraps",
                "allow_interruption",
                "maintenance",
            }
            and type(config["project"]) is str
            and re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", config["project"])
            and type(config["services"]) is dict
            and 0 < len(config["services"]) <= 128
            and type(config["completed_bootstraps"]) is list
            and set(config["completed_bootstraps"]) <= COMPLETED_BOOTSTRAPS
            and type(config["allow_interruption"]) is bool
        )
        stub = Approval(poc_id, config["project"], ())
        ids = docker.ids(stub.project)
        members, omitted, bootstraps, seen = [], [], set(), set()
        for identifier in ids:
            raw = docker.inspect(identifier)
            row = metadata_value(raw, identifier, stub)
            service = row["service"]
            require(service not in seen)
            seen.add(service)
            if service not in config["services"]:
                require(
                    service in EXCLUDED_SERVICES
                    and raw["state"] == "exited"
                    and raw["exit_code"] == 0
                )
                omitted.append(
                    {
                        "id": identifier,
                        "service": service,
                        "identity": digest(
                            {key: row[key] for key in row if key != "state"}
                        ),
                    }
                )
                bootstraps.add(service)
                continue
            require(raw["oneoff"] in {None, "False", "false"})
            require(type(config["services"][service]) is list)
            stores = []
            for mount in mount_projection(raw["mounts"]):
                if mount["RW"] and mount["Type"] != "tmpfs":
                    source = mount["Source"]
                    require(
                        type(source) is str
                        and source.startswith("/")
                        and not source.startswith("//")
                        and source != "/"
                        and ".." not in PurePosixPath(source).parts
                        and str(PurePosixPath(source)) == source
                    )
                    for previous_poc, previous in writable:
                        require(
                            previous_poc == poc_id
                            or not (
                                source == previous
                                or source.startswith(previous + "/")
                                or previous.startswith(source + "/")
                            ),
                            "WRITABLE_STORES_OVERLAP",
                        )
                    writable.append((poc_id, source))
                    stores.append(digest(source))
            members.append(
                Member(
                    service,
                    identifier,
                    row["image_id"],
                    row["mount_identity_digest"],
                    row["runtime_identity_digest"],
                    tuple(config["services"][service]),
                    tuple(stores),
                )
            )
        require(
            set(config["services"]) <= seen
            and set(config["completed_bootstraps"]) <= bootstraps,
            "INITIAL_SETUP_REQUIRED",
        )
        approvals[poc_id] = Approval(
            poc_id,
            stub.project,
            tuple(members),
            tuple(config["completed_bootstraps"]),
            config["allow_interruption"],
        )
        excluded[poc_id], maintenance[poc_id] = omitted, config["maintenance"]
        require(set(docker.ids(stub.project)) == set(ids))
    validate_approvals(approvals)
    return manifest_value(
        {
            "schema_version": 1,
            "runner_id": spec["runner_id"],
            "approvals": approvals_data(approvals),
            "excluded": excluded,
            "maintenance": maintenance,
            "independent_storage_verified": True,
        }
    )


class Client:
    """Fixed manager origin, no redirects or environment proxy inheritance."""

    def __init__(self, origin, token, *, public_origin=None, transport=None):
        """Bind explicit inputs; importing or constructing this object does
        not grant privileges.
        """
        parsed = urlsplit(origin)
        require(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and not parsed.username
            and not parsed.password
            and (
                parsed.scheme == "https"
                or parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            )
        )
        self.origin, self.token, self.transport = origin, token, transport
        self.host_headers = internal_host_headers(origin, public_origin)

    def call(self, action, payload):
        """Send a fixed authenticated control request; do not follow redirects
        or retry.
        """
        require(
            action in {"report", "claim", "result", "heartbeat", "reconcile"}
        )
        try:
            with httpx.Client(
                trust_env=False,
                follow_redirects=False,
                timeout=5,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "POST",
                    self.origin + "/internal/runner/" + action,
                    headers={
                        **self.host_headers,
                        "Authorization": "Bearer " + self.token,
                        "Accept-Encoding": "identity",
                    },
                    json=payload,
                ) as response:
                    require(response.status_code == 200, "STATE_UNAVAILABLE")
                    raw = bytearray()
                    for block in response.iter_bytes():
                        raw.extend(block)
                        require(len(raw) <= 65536, "STATE_UNAVAILABLE")
                    return strict_json(raw)
        except httpx.HTTPError:
            raise RunnerError("STATE_UNAVAILABLE") from None


class Maintenance:
    """Use only a proved daemon's fixed private-IP control origin."""

    def __init__(self, manifest, *, transport=None):
        """Bind explicit inputs; importing or constructing this object does
        not grant privileges.
        """
        self.manifest, self.transport = manifest, transport

    def check_config(self, poc_id):
        """Verify the selected root credential before Docker mutations."""
        path = Path(self.manifest["maintenance"][poc_id]["token_file"])
        directory = private_directory(path.parent)
        try:
            token = (
                private_read(directory, path.name, maximum=128)
                .decode("ascii")
                .strip()
            )
            require(hexadecimal(token), "WORKLOAD_BUSY")
        finally:
            os.close(directory)

    def call(self, poc_id, raw, action, *, allow_interruption=False):
        """Send a fixed authenticated control request; do not follow redirects
        or retry.
        """
        require(action in {"status", "initialize", "reserve", "release"})
        selection = self.manifest["maintenance"][poc_id]
        api = raw.get("demo-api")
        require(api and api["state"] == "running", "WORKLOAD_BUSY")
        network = api["networks"].get(selection["network"])
        require(
            type(network) is dict and type(network.get("IPAddress")) is str,
            "WORKLOAD_BUSY",
        )
        address = ipaddress.ip_address(network["IPAddress"])
        require(
            address.version == 4
            and address.is_private
            and not address.is_loopback,
            "WORKLOAD_BUSY",
        )
        path = Path(selection["token_file"])
        directory = private_directory(path.parent)
        try:
            token = (
                private_read(directory, path.name, maximum=128)
                .decode("ascii")
                .strip()
            )
        finally:
            os.close(directory)
        require(hexadecimal(token), "WORKLOAD_BUSY")
        payload = (
            None
            if action == "status"
            else {
                "reservation_id": selection["reservation_id"],
                "allow_interruption": allow_interruption,
            }
        )
        try:
            with httpx.Client(
                trust_env=False,
                follow_redirects=False,
                timeout=3,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "GET" if action == "status" else "POST",
                    "http://"
                    + str(address)
                    + ":8000/whole-maintenance/"
                    + action,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept-Encoding": "identity",
                    },
                    json=payload,
                ) as response:
                    require(response.status_code == 200, "WORKLOAD_BUSY")
                    body = bytearray()
                    for block in response.iter_bytes():
                        body.extend(block)
                        require(len(body) <= 8192, "WORKLOAD_BUSY")
                    value = strict_json(body)
            require(type(value) is dict, "WORKLOAD_BUSY")
            if action == "status":
                require(
                    set(value) == {"reserved", "initialized", "scope"}
                    and type(value["reserved"]) is bool
                    and type(value["initialized"]) is bool
                    and value["scope"] == "demo_jobs_and_worker_only",
                    "WORKLOAD_BUSY",
                )
            if action == "release":
                require(value == {"released": True}, "WORKLOAD_BUSY")
            if action == "initialize":
                require(
                    value
                    == {"initialized": True, "scope": "state_only_no_drain"},
                    "WORKLOAD_BUSY",
                )
            if action == "reserve":
                require(
                    set(value)
                    == {
                        "reserved",
                        "scope",
                        "drained",
                        "interruption_possible",
                    }
                    and value["reserved"] is True
                    and type(value["drained"]) is bool
                    and value["scope"] == "demo_jobs_and_worker_only"
                    and value["interruption_possible"]
                    is (not value["drained"]),
                    "WORKLOAD_BUSY",
                )
            return value
        except httpx.HTTPError:
            raise RunnerError("WORKLOAD_BUSY") from None

    def idle(self, inventory, raw):
        # Only all-stopped is intrinsically idle. A running stack includes job
        # faces beyond JobManager/worker. It needs explicit interruption ACK;
        # no health probe is converted into a whole-stack idle claim.
        """Claim idle only when every approved resident is observed stopped."""
        return all(row["state"] == "stopped" for row in inventory)


def memory_ready():
    """Check the current minimum free-memory gate, not future resource
    guarantees.
    """
    try:
        rows = dict(
            line.split(":", 1)
            for line in Path("/proc/meminfo").read_text().splitlines()
        )
        raw = rows["MemAvailable"].split()
        return (
            len(raw) == 2
            and raw[1] == "kB"
            and int(raw[0]) * 1024 >= 3 * 1024**3
        )
    except (OSError, ValueError, KeyError):
        return False


class Runner:
    """Enforce root-private enrollment, serialization and one-time durable
    intents.
    """

    def __init__(
        self,
        manifest,
        client,
        docker,
        intent_dir,
        maintenance,
        *,
        memory=memory_ready,
        clock=time.time,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ):
        """Bind explicit inputs; importing or constructing this object does
        not grant privileges.
        """
        self.manifest = manifest_value(manifest)
        self.digest = digest(manifest)
        self.approvals = approvals_value(manifest["approvals"])
        self.client, self.docker, self.intents = (
            client,
            docker,
            Path(intent_dir),
        )
        self.maintenance, self.memory, self.clock, self.sleep = (
            maintenance,
            memory,
            clock,
            sleep,
        )
        self.monotonic = monotonic

    def authority(self):
        """Acquire the independent root mutation lock; concurrent runners fail
        closed.
        """
        directory = private_directory(self.intents)
        lock = None
        try:
            require(len(os.listdir(directory)) <= 8192, "STATE_UNAVAILABLE")
            lock = os.open(
                "execution.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            info = os.fstat(lock)
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1,
                "STATE_UNAVAILABLE",
            )
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RunnerError("OPERATION_UNKNOWN") from None
            return directory, lock
        except BaseException:
            if lock is not None:
                os.close(lock)
            os.close(directory)
            raise

    def authority_state(self, directory):
        """Read the independent root unknown-state barrier, if present."""
        try:
            state = strict_json(private_read(directory, "authority.json"))
        except FileNotFoundError:
            return None
        require(
            type(state) is dict
            and set(state) == {"schema_version", "active"}
            and type(state["schema_version"]) is int
            and state["schema_version"] == 1,
            "STATE_UNAVAILABLE",
        )
        return state["active"]

    def save_authority(self, directory, active):
        """Atomically persist the root execution barrier."""
        name = ".authority-" + secrets.token_hex(16)
        exclusive(directory, name, {"schema_version": 1, "active": active})
        os.replace(
            name, "authority.json", src_dir_fd=directory, dst_dir_fd=directory
        )
        os.fsync(directory)

    def report(self):
        """Publish current validated metadata and admission observations to
        the manager.
        """
        inventories, raw = self.docker.snapshot(self.manifest)
        value = {
            "runner_id": self.manifest["runner_id"],
            "manifest_digest": self.digest,
            "approvals": self.manifest["approvals"],
            "inventories": inventories,
            "admission_ready": self.memory(),
            "workloads_idle": {
                key: self.maintenance.idle(inventories[key], raw[key])
                for key in POC_IDS
            },
            "observed_at": self.clock(),
        }
        self.client.call("report", value)
        return value

    def run_once(self):
        """Report, claim once, then independently validate any queued logical
        operation.
        """
        directory, lock = self.authority()
        try:
            require(
                self.authority_state(directory) is None, "OPERATION_UNKNOWN"
            )
        finally:
            os.close(lock)
            os.close(directory)
        self.report()
        claim = self.client.call(
            "claim",
            {
                "runner_id": self.manifest["runner_id"],
                "manifest_digest": self.digest,
            },
        )
        require(
            type(claim) is dict and set(claim) == {"operation"},
            "STATE_UNAVAILABLE",
        )
        if claim["operation"] is None:
            return {"state": "idle", "mutated": False}
        operation = claim["operation"]
        return self.execute(operation)

    def execute(self, operation):
        """Journal and execute a validated logical operation against root-
        owned authority.
        """
        require(
            type(operation) is dict
            and set(operation)
            == {
                "id",
                "poc_id",
                "action",
                "manifest_digest",
                "snapshot_digest",
                "lease_id",
                "step_count",
                "interruption_ack",
            },
            "STATE_UNAVAILABLE",
        )
        require(
            re.fullmatch(r"[a-f0-9]{24}", operation["id"])
            and re.fullmatch(r"[a-f0-9]{32}", operation["lease_id"])
            and operation["poc_id"] in POC_IDS
            and operation["action"] in {"start", "stop", "restart"}
            and operation["manifest_digest"] == self.digest
            and type(operation["interruption_ack"]) is bool,
            "APPROVAL_CHANGED",
        )
        directory, lock = self.authority()
        try:
            require(
                self.authority_state(directory) is None, "OPERATION_UNKNOWN"
            )
            require(
                sum(
                    name.endswith(".intent.json") and ".step-" not in name
                    for name in os.listdir(directory)
                )
                < 128,
                "STATE_UNAVAILABLE",
            )
            self.save_authority(
                directory,
                {
                    "operation_id": operation["id"],
                    "manifest_digest": self.digest,
                },
            )
        except BaseException:
            os.close(lock)
            os.close(directory)
            raise
        completed, attempted, reserved, maintenance_attempted = (
            0,
            False,
            False,
            False,
        )
        result = {
            "operation_id": operation["id"],
            "lease_id": operation["lease_id"],
            "runner_id": self.manifest["runner_id"],
            "status": "failed",
            "completed_steps": 0,
            "error_code": "RUNNER_ERROR",
        }
        poc_id = operation["poc_id"]
        try:
            # O_EXCL makes a claimed operation strictly one-time at the root
            # authority boundary, even if the unprivileged API requeues it.
            exclusive(directory, operation["id"] + ".intent.json", operation)
            inventories, raw = self.docker.snapshot(self.manifest)
            self.maintenance.check_config(poc_id)
            require(
                digest(inventories[poc_id]) == operation["snapshot_digest"]
            )
            idle = self.maintenance.idle(inventories[poc_id], raw[poc_id])
            preview = plan(
                self.approvals,
                {"poc_id": poc_id, "action": operation["action"]},
                inventories[poc_id],
                admission_ready=self.memory(),
                workloads_idle=idle,
                interruption_ack=operation["interruption_ack"],
            )
            require(
                type(operation["step_count"]) is int
                and len(preview["steps"]) == operation["step_count"]
            )
            require(
                not preview.get("requires_interruption_ack")
                or operation["interruption_ack"],
                "WORKLOAD_BUSY",
            )
            allow_interruption = (
                operation["interruption_ack"]
                and self.approvals[poc_id].allow_interruption
            )
            if preview["steps"] and any(
                r["state"] == "running" for r in inventories[poc_id]
            ):
                reserved = True
                maintenance_attempted = True
                self.maintenance.call(
                    poc_id,
                    raw[poc_id],
                    "reserve",
                    allow_interruption=allow_interruption,
                )
            members = {m.service: m for m in self.approvals[poc_id].members}
            for index, step in enumerate(preview["steps"]):
                self.client.call(
                    "heartbeat",
                    {
                        "runner_id": self.manifest["runner_id"],
                        "operation_id": operation["id"],
                        "lease_id": operation["lease_id"],
                    },
                )
                current, _ = self.docker.snapshot(self.manifest)
                require(
                    step["action"] == "stop" or self.memory(), "MEMORY_BUDGET"
                )
                member = members[step["service"]]
                row = next(
                    r
                    for r in current[poc_id]
                    if r["service"] == member.service
                )
                require(
                    row["state"]
                    == ("running" if step["action"] == "stop" else "stopped")
                )
                exclusive(
                    directory,
                    operation["id"] + ".step-" + str(index) + ".intent.json",
                    step,
                )
                attempted = True
                self.docker.transition(member.container_id, step["action"])
                deadline = self.monotonic() + 90
                while True:
                    observed, details = self.docker.snapshot(self.manifest)
                    value = details[poc_id][member.service]
                    wanted = (
                        "running" if step["action"] == "start" else "stopped"
                    )
                    target = next(
                        r
                        for r in observed[poc_id]
                        if r["service"] == member.service
                    )
                    if target["state"] == wanted and (
                        wanted == "stopped"
                        or value["health"] in {None, "healthy"}
                    ):
                        break
                    require(self.monotonic() < deadline, "HEALTH_TIMEOUT")
                    self.sleep(1)
                completed += 1
                exclusive(
                    directory,
                    operation["id"] + ".step-" + str(index) + ".result.json",
                    {"completed": True},
                )
            observed, details = self.docker.snapshot(self.manifest)
            wanted = "stopped" if operation["action"] == "stop" else "running"
            require(
                all(row["state"] == wanted for row in observed[poc_id]),
                "HEALTH_TIMEOUT",
            )
            if wanted == "running":
                require(
                    all(
                        raw["health"] in {None, "healthy"}
                        for raw in details[poc_id].values()
                    ),
                    "HEALTH_TIMEOUT",
                )
            if operation["action"] != "stop":
                # A previously stopped API may retain the successful stop's
                # reservation. Reclaim the enrollment ID, then release.
                status = self.maintenance.call(
                    poc_id, details[poc_id], "status"
                )
                require(
                    type(status) is dict
                    and set(status) == {"reserved", "initialized", "scope"}
                    and type(status["reserved"]) is bool
                    and type(status["initialized"]) is bool
                    and status["scope"] == "demo_jobs_and_worker_only",
                    "WORKLOAD_BUSY",
                )
                if not status["initialized"]:
                    maintenance_attempted = True
                    self.maintenance.call(
                        poc_id, details[poc_id], "initialize"
                    )
                    status = self.maintenance.call(
                        poc_id, details[poc_id], "status"
                    )
                    require(
                        status.get("initialized") is True
                        and status.get("reserved") is False,
                        "WORKLOAD_BUSY",
                    )
                if status["reserved"]:
                    reserved = True
                    maintenance_attempted = True
                    self.maintenance.call(
                        poc_id,
                        details[poc_id],
                        "reserve",
                        allow_interruption=allow_interruption,
                    )
                    self.maintenance.call(
                        poc_id,
                        details[poc_id],
                        "release",
                        allow_interruption=allow_interruption,
                    )
                    reserved = False
            result.update(
                status="succeeded", completed_steps=completed, error_code=None
            )
        except Exception as error:
            code = (
                str(error)
                if type(error) is RunnerError
                and str(error)
                in {
                    "APPROVAL_CHANGED",
                    "IDENTITY_CHANGED",
                    "MEMORY_BUDGET",
                    "WORKLOAD_BUSY",
                    "HEALTH_TIMEOUT",
                    "DOCKER_FAILED",
                    "DOCKER_PERMISSION_DENIED",
                    "STATE_UNAVAILABLE",
                }
                else "RUNNER_ERROR"
            )
            result.update(
                status="unknown"
                if attempted or maintenance_attempted
                else "failed",
                completed_steps=completed,
                error_code=code,
            )
        finally:
            # Reservations are intentionally preserved after failed/unknown
            # work and after stop. Explicit owner reconciliation is required.
            try:
                exclusive(
                    directory,
                    operation["id"] + ".result.json",
                    {
                        **result,
                        "docker_mutation_may_have_started": attempted,
                        "maintenance_retained": reserved,
                        "maintenance_may_have_changed": maintenance_attempted,
                        "automatic_retry": False,
                    },
                )
            except Exception:
                result.update(status="unknown", error_code="STATE_UNAVAILABLE")
            delivered = False
            try:
                receipt = self.client.call("result", result)
                row = (
                    receipt.get("operation") if type(receipt) is dict else None
                )
                require(
                    type(row) is dict
                    and row.get("id") == operation["id"]
                    and row.get("poc_id") == operation["poc_id"]
                    and row.get("action") == operation["action"]
                    and row.get("status") == result["status"]
                    and type(row.get("completed_steps")) is int
                    and row.get("error_code") == result["error_code"]
                    and row.get("completed_steps") == completed,
                    "STATE_UNAVAILABLE",
                )
                delivered = True
            except Exception:
                # Do not resend or clear root authority. The durable local
                # outcome remains available for explicit reconciliation.
                pass
            if delivered and (
                result["status"] == "succeeded"
                or (
                    result["status"] == "failed"
                    and not attempted
                    and not maintenance_attempted
                )
            ):
                try:
                    self.save_authority(directory, None)
                except Exception:
                    result.update(
                        status="unknown", error_code="STATE_UNAVAILABLE"
                    )
            os.close(lock)
            os.close(directory)

        return {
            "state": result["status"] if delivered else "unknown",
            "result_delivery_confirmed": delivered,
            "local_outcome": result["status"],
            "completed_steps": completed,
            "mutated": attempted or maintenance_attempted,
            "error_code": result["error_code"],
        }

    def reconcile(self, operation_id):
        """Explicit operator record reconciliation; never replay a mutation."""
        require(
            type(operation_id) is str
            and re.fullmatch(r"[a-f0-9]{24}", operation_id)
        )
        directory, lock = self.authority()
        try:
            require(
                self.authority_state(directory)
                == {
                    "operation_id": operation_id,
                    "manifest_digest": self.digest,
                },
                "OPERATION_UNKNOWN",
            )
            inventories, _ = self.docker.snapshot(self.manifest)
            original = strict_json(
                private_read(directory, operation_id + ".intent.json")
            )
            require(
                type(original) is dict
                and original.get("id") == operation_id
                and original.get("manifest_digest") == self.digest
                and type(original.get("lease_id")) is str
                and re.fullmatch(r"[a-f0-9]{32}", original["lease_id"]),
                "APPROVAL_CHANGED",
            )
            local_outcome = "not_finalized"
            try:
                local_result = strict_json(
                    private_read(directory, operation_id + ".result.json")
                )
            except FileNotFoundError:
                local_result = None
            if local_result is not None:
                require(
                    type(local_result) is dict
                    and local_result.get("operation_id") == operation_id
                    and local_result.get("lease_id") == original["lease_id"]
                    and local_result.get("runner_id")
                    == self.manifest["runner_id"]
                    and local_result.get("status")
                    in {"succeeded", "failed", "unknown"}
                    and type(local_result.get("completed_steps")) is int
                    and 0
                    <= local_result["completed_steps"]
                    <= original["step_count"],
                    "APPROVAL_CHANGED",
                )
                local_outcome = local_result["status"]
            self.report()
            proof = {
                "operation_id": operation_id,
                "manifest_digest": self.digest,
                "observed_inventory_digest": digest(inventories),
                "automatic_retry": False,
                "local_outcome": local_outcome,
            }
            name = operation_id + ".reconcile.json"
            try:
                previous = strict_json(private_read(directory, name))
                require(previous == proof, "APPROVAL_CHANGED")
            except FileNotFoundError:
                exclusive(directory, name, proof)
            receipt = self.client.call(
                "reconcile",
                {
                    "runner_id": self.manifest["runner_id"],
                    "operation_id": operation_id,
                    "lease_id": original["lease_id"],
                },
            )
            require(
                type(receipt) is dict
                and type(receipt.get("operation")) is dict
                and receipt["operation"].get("id") == operation_id
                and receipt["operation"].get("status")
                in {"failed", "succeeded"},
                "STATE_UNAVAILABLE",
            )
            self.save_authority(directory, None)
            return {
                "state": "operator_reconciled",
                "mutated": False,
                "interrupted_jobs_completed": "not_asserted",
                "maintenance_release": "not_performed",
                "local_outcome": local_outcome,
            }
        finally:
            os.close(lock)
            os.close(directory)


def internal_host_headers(origin, public_origin=None):
    """Connect only to loopback while sending a separately fixed public Host.

    This is an operator-selected internal channel, not a proxy/Host value
    accepted from a browser. The API's exact public Host check stays intact.
    No bearer credential is routed through a public Cloudflare application.
    """
    if public_origin is None:
        return {}
    public = urlsplit(public_origin)
    internal = urlsplit(origin)
    require(
        type(public_origin) is str
        and not any(x.isspace() for x in public_origin)
        and public.scheme == "https"
        and public.hostname
        and not public.username
        and not public.password
        and not public.path
        and not public.query
        and not public.fragment
        and internal.scheme == "http"
        and internal.hostname in {"127.0.0.1", "::1"},
        "MANAGER_ORIGIN_INVALID",
    )
    return {"Host": public.netloc}
