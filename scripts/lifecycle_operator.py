"""Run approved fixed-group Compose requests in an owner's foreground shell.

This program is deliberately separate from the web image's request queue. It
never accepts commands, service names, paths or projects from a browser. Plan
does not write. Approval creates only private authority files; running Docker
commands requires a later explicit --run by the host operator. Nothing here
installs a daemon, edits permissions, builds images or initializes SQL.
"""

import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/demo"))

import notebook_workspace  # noqa: E402
import settings  # noqa: E402
from lifecycle_catalog import FILES, GROUPS, profile_set  # noqa: E402
from lifecycle_store import (  # noqa: E402
    canonical,
    open_directory,
    private_bytes,
    public_settings_digest,
)

LIMIT = 4 * 1024 * 1024
DOCKER = ("docker", "--host", "unix:///var/run/docker.sock")
SAFE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
BOOTSTRAPS = {"bi": "superset-init"}
CORE = ("catalog-db", "storage", "catalog", "trino", "worker", "demo-api")
PUBLIC_CONFIGS = {
    "prometheus-config": ROOT / "infra/monitoring/prometheus.yml",
    "monitoring-rules": ROOT / "infra/monitoring/rules/foundation.yml",
    "grafana-datasource": ROOT / "infra/monitoring/grafana-datasource.yml",
    "loki-config": ROOT / "infra/monitoring/loki/loki.yaml",
}


class OperatorError(ValueError):
    """Only fixed codes cross the command/log boundary."""


def require(condition, code="APPROVAL_CHANGED"):
    if not condition:
        raise OperatorError(code)


def file_bytes(path, *, private=False, limit=LIMIT):
    """Read through no-follow descriptors, including every path component."""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        handle = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        with os.fdopen(handle, "rb") as source:
            info = os.fstat(source.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_size <= limit)
            if private:
                require(
                    info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) == 0o600
                )
            result = source.read(limit + 1)
            require(len(result) <= limit)
            return result
    finally:
        os.close(fd)


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def selected_path(value):
    """Reject lexical aliases before pathlib can erase their spelling."""
    raw = os.fspath(value)
    require(
        raw.startswith("/")
        and "//" not in raw
        and not {".", ".."} & set(raw.split("/"))
        and not any(character in raw for character in "\x00\r\n")
    )
    return Path(raw)


def directory_ancestors(path):
    """Physical parent identities prevent a same-inode alias authority root."""
    result = []
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        info = os.fstat(fd)
        result.append((info.st_dev, info.st_ino))
        for part in selected_path(path).parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            # Root-owned sticky temporary parents protect their owned child
            # names. Every other traversed parent must be non-writable to
            # unrelated users; a root/owner-controlled ancestor is required.
            require(
                info.st_uid in {0, os.getuid()}
                and (
                    not info.st_mode & 0o022
                    or info.st_uid == 0
                    and info.st_mode & stat.S_ISVTX
                )
            )
            result.append((info.st_dev, info.st_ino))
        return result
    finally:
        os.close(fd)


def public_parent_state(info, secured):
    """Let a private owner ancestor protect group-writable children."""
    require(info.st_uid in {0, os.getuid()})
    require(
        not info.st_mode & 0o022
        or info.st_uid == 0
        and info.st_mode & stat.S_ISVTX
        or secured
        and not info.st_mode & 0o002
    )
    return secured or (info.st_uid == os.getuid() and not info.st_mode & 0o077)


def public_config_parents(path):
    """Inspect trust boundaries without following links or changing modes."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    secured = False
    try:
        for part in selected_path(path).parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
            secured = public_parent_state(os.fstat(fd), secured)
        return secured
    finally:
        os.close(fd)


def equivalent_public_config(expected, actual):
    """Allow a fixed public config from a previous checkout, by exact bytes.

    This exception never applies to writable data, workspace, log spool, named
    volumes, arbitrary CA files or an unknown config target. It accommodates
    deploying reviewed code from a new checkout without adopting data mounts.
    """
    expected = Path(expected)
    allowed = {
        *PUBLIC_CONFIGS.values(),
        ROOT / "infra/proxy/empty-ca.pem",
        # Only this repository-owned inert placeholder is a public alias.
        # The openobserve-vector-token key and actual private tokens retain
        # their existing0600 secret classification when frozen.
        ROOT / "infra/monitoring/vector/openobserve-secrets.example.json",
    }
    if expected not in allowed:
        return False
    if not isinstance(actual, str):
        return False
    try:
        selected = selected_path(actual)
        suffix = "/" + expected.relative_to(ROOT).as_posix()
        if not actual.endswith(suffix):
            return False
        secured = public_config_parents(selected.parent)
        info = selected.lstat()
        require(stat.S_ISREG(info.st_mode))
        public_parent_state(info, secured)
        return (
            hashlib.sha256(file_bytes(selected)).digest()
            == hashlib.sha256(file_bytes(expected)).digest()
        )
    except (OSError, ValueError, TypeError):
        return False


class Commands:
    """Bound output/deadlines and never reveal Docker output or raw errors."""

    def __init__(self, *, sudo=False):
        self.prefix = ("sudo", "-n") if sudo else ()

    def __call__(self, arguments, *, environment=None, timeout=20):
        # Temporary regular files avoid unbounded communicate() allocations.
        import tempfile

        with (
            tempfile.TemporaryFile() as output,
            tempfile.TemporaryFile() as error,
        ):
            try:
                process = subprocess.Popen(
                    (*self.prefix, *DOCKER, *arguments),
                    cwd=ROOT,
                    env=environment or SAFE_ENV,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=error,
                )
                deadline = time.monotonic() + timeout
                while process.poll() is None:
                    if (
                        time.monotonic() >= deadline
                        or os.fstat(output.fileno()).st_size > LIMIT
                        or os.fstat(error.fileno()).st_size > LIMIT
                    ):
                        process.kill()
                        process.wait()
                        raise OperatorError("OPERATOR_COMMAND_FAILED")
                    time.sleep(0.05)
                require(process.returncode == 0, "OPERATOR_COMMAND_FAILED")
                output.seek(0)
                result = output.read(LIMIT + 1)
                require(len(result) <= LIMIT, "OPERATOR_COMMAND_FAILED")
                return result
            except (OSError, subprocess.SubprocessError):
                raise OperatorError("OPERATOR_COMMAND_FAILED") from None


def container(commands, project, service):
    """Find one fixed-project Compose service, including stopped services."""
    output = commands(
        (
            "ps",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            "label=com.docker.compose.project=" + project,
            "--filter",
            "label=com.docker.compose.service=" + service,
        )
    )
    identifiers = output.decode("ascii").split()
    require(len(identifiers) <= 1, "STATE_UNAVAILABLE")
    if not identifiers:
        return None
    require(
        bool(re.fullmatch(r"[a-f0-9]{64}", identifiers[0])),
        "STATE_UNAVAILABLE",
    )
    rows = json.loads(
        commands(("inspect", "--type", "container", identifiers[0]))
    )
    require(isinstance(rows, list) and len(rows) == 1, "STATE_UNAVAILABLE")
    value = rows[0]
    labels = value.get("Config", {}).get("Labels", {})
    require(
        labels.get("com.docker.compose.project") == project
        and labels.get("com.docker.compose.service") == service
        and labels.get("app.owner") == "lakehouse-foundation",
        "STATE_UNAVAILABLE",
    )
    return value


def healthy(value):
    """Running without an advertised healthcheck is liveness, not app proof."""
    state = (value or {}).get("State", {})
    return (
        state.get("Running") is True
        and state.get("Health", {}).get("Status", "healthy") == "healthy"
    )


def verify_api(commands, project, port, private):
    """Prove project labels/loopback/private mount before ANY token is read.

    This verifies the local daemon's identity, not an untrusted remote daemon.
    No Docker environment/context override is honored by Commands.
    """
    value = container(commands, project, "demo-api")
    require(healthy(value), "OPERATOR_AUTH_REQUIRED")
    bindings = (
        value.get("NetworkSettings", {}).get("Ports", {}).get("8000/tcp")
    )
    require(
        bindings == [{"HostIp": "127.0.0.1", "HostPort": str(port)}],
        "OPERATOR_AUTH_REQUIRED",
    )
    mounts = value.get("Mounts", [])
    require(
        len(
            [
                item
                for item in mounts
                if item.get("Destination") == "/private"
                and item.get("Type") == "bind"
                and item.get("Source") == str(private)
                and item.get("RW") is True
            ]
        )
        == 1,
        "OPERATOR_AUTH_REQUIRED",
    )
    require(
        not value.get("HostConfig", {}).get("Privileged")
        and not any(
            "docker.sock" in item.get("Source", "") for item in mounts
        ),
        "OPERATOR_AUTH_REQUIRED",
    )
    return {"id": value["Id"], "image": value["Image"]}


class Client:
    """Use loopback only; never follow redirects or reveal error bodies."""

    def __init__(self, context, owner_token, runner_token=None):
        self.context = context
        self.owner_token = owner_token
        self.runner_token = runner_token

    def call(self, path, payload=None, *, operator=False):
        require(
            path
            in {
                "/settings",
                "/lifecycle/runner/register",
                "/lifecycle/runner/heartbeat",
                "/lifecycle/runner/claim",
                "/lifecycle/runner/result",
                "/lifecycle/runner/reconcile",
                "/lifecycle/runner/permit",
            },
            "STATE_UNAVAILABLE",
        )
        self.context.identity()
        token = self.runner_token if operator else self.owner_token
        require(bool(token), "OPERATOR_AUTH_REQUIRED")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.context.port, timeout=8
        )
        try:
            body = canonical(payload) if payload is not None else None
            connection.request(
                "POST" if body is not None else "GET",
                path,
                body=body,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            require(200 <= response.status < 300, "OPERATOR_AUTH_REQUIRED")
            raw = response.read(LIMIT + 1)
            require(len(raw) <= LIMIT, "STATE_UNAVAILABLE")
            return json.loads(raw)
        except (OSError, ValueError, http.client.HTTPException):
            raise OperatorError("STATE_UNAVAILABLE") from None
        finally:
            connection.close()


class Context:
    """Owner-selected authority. No API request can add to these arguments."""

    def __init__(
        self,
        *,
        env_file,
        manifest,
        project,
        port,
        groups,
        overlays=(),
        commands=None,
    ):
        require(re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", project) is not None)
        require(1024 <= port <= 65535)
        require(
            groups
            and len(set(groups)) == len(groups)
            and set(groups) <= GROUPS.keys()
        )
        self.env_file = selected_path(env_file)
        self.manifest = selected_path(manifest)
        self.project, self.port = project, port
        self.groups = tuple(sorted(groups))
        self.overlays = tuple(selected_path(path) for path in overlays)
        self.commands = commands or Commands()
        self.values = None
        self.frozen = None
        # The entire selected env parent is writable by the API. Authority must
        # be outside it, not merely outside .lifecycle, and outside checkout.
        require(
            not self.manifest.is_relative_to(self.env_file.parent)
            and not self.env_file.parent.is_relative_to(self.manifest.parent)
            and not self.manifest.is_relative_to(ROOT)
            and not ROOT.is_relative_to(self.manifest.parent)
        )

    def identity(self):
        return verify_api(
            self.commands, self.project, self.port, self.env_file.parent
        )

    def load(self):
        self.identity()  # Must precede settings.load and runner.token access.
        file_bytes(self.env_file, private=True)
        private = directory_ancestors(self.env_file.parent)
        authority = directory_ancestors(self.manifest.parent)
        require(private[-1] not in authority and authority[-1] not in private)
        self.values = settings.load(self.env_file)
        require(
            self.values["COMPOSE_PROJECT_NAME"] == self.project
            and self.values.get("BIND_IP", "127.0.0.1") == "127.0.0.1"
            and int(self.values.get("DEMO_API_PORT", "8502")) == self.port
            and self.values.get("CONTROL_SETTINGS_DIR")
            == str(self.env_file.parent)
            and int(self.values["WORKER_UID"]) == os.getuid()
            and os.getuid() != 0,
            "OPERATOR_AUTH_REQUIRED",
        )
        return self.values

    def compose(self):
        if self.frozen is not None:
            return (
                "compose",
                "--project-name",
                self.project,
                "--file",
                str(self.frozen),
            )
        result = [
            "compose",
            "--project-name",
            self.project,
            "--env-file",
            str(self.env_file),
        ]
        for path in (*[ROOT / name for name in FILES], *self.overlays):
            result.extend(("--file", str(path)))
        return tuple(result)

    def environment(self, profiles=None):
        values = dict(self.values)
        if profiles is not None:
            values["COMPOSE_PROFILES"] = ",".join(sorted(profiles))
        # settings.environment preserves canonical proxy/CA behavior; strip the
        # inherited shell so stale COMPOSE_FILE/DOCKER_HOST cannot retarget us.
        with_environment = settings.environment(values)
        if self.frozen is not None:
            # Frozen model has already resolved secrets, proxy/CA, volume and
            # bind identities. Original env/overlays cannot affect execution.
            return {**SAFE_ENV, "COMPOSE_PROFILES": values["COMPOSE_PROFILES"]}
        return {
            **SAFE_ENV,
            **{
                key: with_environment[key]
                for key in values
                if key in with_environment
            },
        }

    def configuration(self):
        profiles = profile_set(self.values) | {
            profile
            for name in self.groups
            for profile in GROUPS[name].profiles
        }
        rendered = json.loads(
            self.commands(
                (*self.compose(), "config", "--format", "json"),
                environment=self.environment(profiles),
            )
        )
        require(rendered.get("name") == self.project)
        return rendered

    def snapshot(self, *, approval=None):
        """Hash configuration, sources, images and exact bind/volume identity.

        No secret values appear in the private manifest, only digests. All
        sources are pinned even if a group does not currently use them.
        """
        self.load()
        require(
            self.values.get("DATA_VOLUME_PREFIX", "") in {"", self.project}
        )
        if approval is not None:
            # A digest check followed by re-reading mutable Compose inputs is
            # not a trust boundary. Revalidation NEVER gives the original
            # Web-writable env/overlay to Docker, even when hashes match.
            require(
                digest(
                    {
                        key: value
                        for key, value in self.values.items()
                        if key != "COMPOSE_PROFILES"
                    }
                )
                == approval["settings_hash"]
            )
            require(
                approval["files"]
                == [str(ROOT / name) for name in FILES]
                + [str(path) for path in self.overlays]
            )
            for source, expected_hash in approval["sources"].items():
                require(
                    hashlib.sha256(file_bytes(source)).hexdigest()
                    == expected_hash
                )
            rendered = approved_source_model(self, approval)
        else:
            # Parsing operator-selected original inputs is limited to the
            # explicit read-only plan / initial approval boundary.
            saved_frozen, self.frozen = self.frozen, None
            try:
                rendered = self.configuration()
            finally:
                self.frozen = saved_frozen
        self.rendered = rendered
        # Profiles can change only through successful fixed group operations.
        # Vector's profile-switch environment is accounted for independently.
        normalized = json.loads(json.dumps(rendered))
        for definition in normalized["services"].values():
            definition.get("environment", {}).pop("COMPOSE_PROFILES", None)
        sources = {
            str(path): hashlib.sha256(file_bytes(path)).hexdigest()
            for path in (*[ROOT / name for name in FILES], *self.overlays)
        }
        bindings = {}
        images = {}
        selected = {
            service for name in self.groups for service in GROUPS[name].starts
        }
        if "openobserve-lab" in self.groups:
            selected.add("vector")
        for service in sorted(selected):
            definition = rendered["services"][service]
            require(
                not definition.get("privileged")
                and definition.get("network_mode") != "host"
                and not definition.get("pid")
                and not definition.get("devices")
                and not definition.get("use_api_socket")
                and (
                    not definition.get("cap_add")
                    or service == "spark-volumes-init"
                    and set(definition["cap_add"]) <= {"CHOWN", "FOWNER"}
                )
            )
            for port in definition.get("ports", []):
                require(port.get("host_ip") == "127.0.0.1")
            image = definition["image"]
            # Missing images never trigger a pull, build or credential prompt.
            rows = json.loads(self.commands(("image", "inspect", image)))
            require(
                len(rows) == 1
                and re.fullmatch(r"sha256:[a-f0-9]{64}", rows[0]["Id"]),
                "INITIAL_SETUP_REQUIRED",
            )
            images[image] = rows[0]["Id"]
            for item in definition.get("volumes", []):
                source = item.get("source", "")
                require("docker.sock" not in source)
                if item["type"] == "bind":
                    path = Path(source)
                    require(path.is_absolute())
                    approved_binds = {
                        "vector": (
                            self.values.get("MONITORING_LOG_DIR"),
                            "/var/lib/lakehouse-log-spool",
                            True,
                        ),
                        "jupyter": (
                            self.values.get("NOTEBOOKS_DIR"),
                            "/workspace",
                            False,
                        ),
                        "n8n": (
                            self.values.get("CORPORATE_CA_FILE")
                            or str(ROOT / "infra/proxy/empty-ca.pem"),
                            "/etc/n8n/corporate-ca.pem",
                            True,
                        ),
                    }
                    require(
                        (
                            source,
                            item.get("target"),
                            item.get("read_only", False),
                        )
                        == approved_binds.get(service)
                    )
                    metadata = path.lstat()
                    require(not stat.S_ISLNK(metadata.st_mode))
                    bindings[source] = {
                        "device": metadata.st_dev,
                        "inode": metadata.st_ino,
                        "mode": metadata.st_mode,
                        "uid": metadata.st_uid,
                        "gid": metadata.st_gid,
                    }
                    if stat.S_ISREG(metadata.st_mode):
                        sources[source] = hashlib.sha256(
                            file_bytes(path)
                        ).hexdigest()
                elif item["type"] == "volume":
                    specification = rendered["volumes"][source]
                    volume = specification["name"]
                    require(
                        specification.get("external") in (None, False)
                        and volume == self.project + "_" + source
                        and re.fullmatch(
                            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", volume
                        )
                    )
                    rows = json.loads(
                        self.commands(("volume", "inspect", volume))
                    )
                    require(
                        len(rows) == 1
                        and rows[0]["Name"] == volume
                        and isinstance(rows[0].get("Labels"), dict)
                        and rows[0]["Labels"].get("com.docker.compose.project")
                        == self.project
                        and rows[0]["Labels"].get("com.docker.compose.volume")
                        == source,
                        "INITIAL_SETUP_REQUIRED",
                    )
                    bindings["volume:" + volume] = {
                        key: rows[0].get(key)
                        for key in (
                            "Name",
                            "CreatedAt",
                            "Driver",
                            "Options",
                            "Labels",
                        )
                    }
            for item in definition.get("configs", []):
                configuration = rendered["configs"][item["source"]]
                if configuration.get("file"):
                    path = Path(configuration["file"])
                    sources[str(path)] = hashlib.sha256(
                        file_bytes(path)
                    ).hexdigest()
        self.validate_mounts(approval=approval)
        return {
            "version": 1,
            "manager": "operator-compose",
            "project": self.project,
            "api_port": self.port,
            "env_file": str(self.env_file),
            "groups": list(self.groups),
            "files": [str(ROOT / name) for name in FILES]
            + [str(path) for path in self.overlays],
            "config_hash": digest(normalized),
            "sources": sources,
            "settings_hash": digest(
                {
                    k: v
                    for k, v in self.values.items()
                    if k != "COMPOSE_PROFILES"
                }
            ),
            "images": images,
            "bindings": bindings,
        }

    def validate_mounts(self, *, approval=None, services=None):
        """Reject adoption or drift of another project's mounted resources.

        Docker's volume label identifies its Compose owner. A matching path or
        project prefix alone does not establish ownership. The protected model
        may substitute only an approved frozen config/CA file, never a volume.
        """
        selected = (
            set(services)
            if services is not None
            else {
                service
                for name in self.groups
                for service in GROUPS[name].starts
            }
        )
        if services is None and "openobserve-lab" in self.groups:
            selected.add("vector")
        rendered = self.rendered
        for service in sorted(selected):
            definition = rendered["services"][service]
            expected = {}
            for item in definition.get("volumes", []):
                source = item["source"]
                kind = item["type"]
                require(kind in {"volume", "bind"})
                target = item["target"]
                require(target not in expected)
                name = (
                    rendered["volumes"][source]["name"]
                    if kind == "volume"
                    else source
                )
                expected[target] = (
                    kind,
                    name,
                    not item.get("read_only", False),
                )
            for item in definition.get("configs", []):
                config = rendered["configs"][item["source"]]
                require("file" in config)
                target = item.get("target", "/" + item["source"])
                require(target not in expected)
                expected[target] = ("config", config["file"], False)
            value = container(self.commands, self.project, service)
            require(value is not None, "INITIAL_SETUP_REQUIRED")
            actual = value.get("Mounts")
            require(isinstance(actual, list))
            # Engine versions can expose declared tmpfs separately in
            # HostConfig.Tmpfs or among Mounts. Neither is a named volume or
            # permission to accept an unknown host bind.
            temporary = {}
            for declaration in definition.get("tmpfs", []):
                target, _, options = declaration.partition(":")
                require(target.startswith("/") and target not in expected)
                temporary[target] = "ro" not in options.split(",")
            listed = {
                item.get("Destination"): item
                for item in actual
                if item.get("Type") == "tmpfs"
            }
            host_temporary = value.get("HostConfig", {}).get("Tmpfs", {}) or {}
            require(set(host_temporary) | set(listed) == set(temporary))
            for target, writable in temporary.items():
                if target in listed:
                    require(
                        type(listed[target].get("RW")) is bool
                        and listed[target]["RW"] is writable
                    )
                if target in host_temporary:
                    require(
                        ("ro" not in host_temporary[target].split(","))
                        is writable
                    )
            actual = [item for item in actual if item.get("Type") != "tmpfs"]
            require(len(actual) == len(expected))
            seen = set()
            for item in actual:
                target = item.get("Destination")
                require(target in expected and target not in seen)
                seen.add(target)
                kind, source, writable = expected[target]
                require(
                    type(item.get("RW")) is bool and item["RW"] is writable
                )
                if kind == "volume":
                    require(
                        item.get("Type") == "volume"
                        and item.get("Name") == source
                    )
                    # Re-read ownership immediately before each mutation too.
                    key = next(
                        key
                        for key, spec in rendered["volumes"].items()
                        if spec["name"] == source
                    )
                    rows = json.loads(
                        self.commands(
                            (
                                "volume",
                                "inspect",
                                source,
                            )
                        )
                    )
                    require(
                        len(rows) == 1
                        and rows[0].get("Name") == source
                        and isinstance(rows[0].get("Labels"), dict)
                        and rows[0]["Labels"].get("com.docker.compose.project")
                        == self.project
                        and rows[0]["Labels"].get("com.docker.compose.volume")
                        == key
                    )
                else:
                    allowed_sources = {source}
                    if approval:
                        prefix = "config-" if kind == "config" else "bind-"
                        name = (
                            prefix
                            + hashlib.sha256(source.encode()).hexdigest()
                            + ".data"
                        )
                        if name in approval["frozen_files"]:
                            allowed_sources.add(
                                str(self.manifest.parent / name)
                            )
                    require(
                        item.get("Type") == "bind"
                        and (
                            item.get("Source") in allowed_sources
                            or not writable
                            and equivalent_public_config(
                                source, item.get("Source")
                            )
                        )
                    )

    def readiness(self):
        """Inspect preparation only; no SQL bootstrap, chown or repair."""
        blocked = {}
        for name in self.groups:
            group = GROUPS[name]
            # Existing containers attest that first-use volume/credential
            # preparation happened through the owning lifecycle manager.
            for service in group.starts:
                value = container(self.commands, self.project, service)
                if value is None:
                    blocked[name] = "INITIAL_SETUP_REQUIRED"
                if service == "spark-volumes-init" and (
                    value is None
                    or value.get("State", {}).get("ExitCode") != 0
                    or value.get("State", {}).get("Status") != "exited"
                ):
                    blocked[name] = "INITIAL_SETUP_REQUIRED"
            if name in BOOTSTRAPS:
                value = container(
                    self.commands, self.project, BOOTSTRAPS[name]
                )
                if (
                    value is None
                    or value.get("State", {}).get("Status") != "exited"
                    or value["State"].get("ExitCode") != 0
                ):
                    blocked[name] = "INITIAL_SETUP_REQUIRED"
            if name == "notebooks":
                values = {**self.values, "COMPOSE_PROFILES": "notebooks"}
                if not notebook_workspace.inspect_workspace(
                    values, root=ROOT, settings_path=self.env_file
                )["ready"]:
                    blocked[name] = "INITIAL_SETUP_REQUIRED"
            if name in {"openobserve-lab", "sail-lab"}:
                try:
                    available = next(
                        line.split()[1]
                        for line in Path("/proc/meminfo")
                        .read_text()
                        .splitlines()
                        if line.startswith("MemAvailable:")
                    )
                    if int(available) * 1024 < 3 * 1024**3:
                        blocked[name] = "MEMORY_BUDGET"
                except (OSError, ValueError, StopIteration):
                    blocked[name] = "MEMORY_BUDGET"
        return blocked

    def observations(self):
        result = {}
        for name in self.groups:
            group = GROUPS[name]
            values = [
                container(self.commands, self.project, service)
                for service in group.daemons
            ]
            state = (
                "running"
                if all(healthy(item) for item in values)
                else "stopped"
                if all(
                    item and not item.get("State", {}).get("Running")
                    for item in values
                )
                else "unknown"
            )
            for card in group.cards:
                result[card] = state
        return result


def manifest_read(context):
    fd = open_directory(context.manifest.parent)
    try:
        return json.loads(private_bytes(fd, context.manifest.name))
    finally:
        os.close(fd)


def approve(context):
    """Save exclusive approval files; never run service commands."""
    snapshot = context.snapshot()
    require(not context.readiness(), "INITIAL_SETUP_REQUIRED")
    fd = open_directory(context.manifest.parent)
    try:
        frozen_files = {}
        content = canonical(context.rendered)
        write_exclusive(fd, "rendered-source.json", content)
        frozen_files["rendered-source.json"] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "mode": 0o600,
        }
        # Referenced configuration files become immutable private copies. The
        # Web container cannot replace these between preflight and Docker use.
        model = json.loads(json.dumps(context.rendered))
        selected = {
            service
            for name in context.groups
            for service in GROUPS[name].starts
        }
        if "openobserve-lab" in context.groups:
            selected.add("vector")
        frozen_configs = set()
        for service in sorted(selected):
            definition = model["services"][service]
            definition["image"] = snapshot["images"][definition["image"]]
            definition.pop("build", None)
            definition["pull_policy"] = "never"
            for item in definition.get("configs", []):
                if item["source"] in frozen_configs:
                    continue
                config = model["configs"][item["source"]]
                if "file" in config:
                    source = Path(config["file"])
                    public = item["source"] in PUBLIC_CONFIGS
                    if public:
                        require(source == PUBLIC_CONFIGS[item["source"]])
                    else:
                        require(
                            item["source"] == "openobserve-vector-token"
                            and service == "vector"
                        )
                    name = (
                        "config-"
                        + hashlib.sha256(str(source).encode()).hexdigest()
                        + ".data"
                    )
                    content = file_bytes(source)
                    # Public static config/CA must remain readable by fixed
                    # Prometheus/Grafana/Loki image UIDs. The owner-only parent
                    # protects the host copy; actual secrets remain0600.
                    mode = 0o644 if public else 0o600
                    write_exclusive(fd, name, content, mode=mode)
                    frozen_files[name] = {
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "mode": mode,
                    }
                    config["file"] = str(context.manifest.parent / name)
                frozen_configs.add(item["source"])
            for item in definition.get("volumes", []):
                if item["type"] == "bind":
                    source = Path(item["source"])
                    if stat.S_ISREG(source.lstat().st_mode):
                        name = (
                            "bind-"
                            + hashlib.sha256(str(source).encode()).hexdigest()
                            + ".data"
                        )
                        content = file_bytes(source)
                        require(
                            service == "n8n"
                            and item["target"] == "/etc/n8n/corporate-ca.pem"
                        )
                        write_exclusive(fd, name, content, mode=0o644)
                        frozen_files[name] = {
                            "sha256": hashlib.sha256(content).hexdigest(),
                            "mode": 0o644,
                        }
                        item["source"] = str(context.manifest.parent / name)
        for lab in (False, True):
            variant = json.loads(json.dumps(model))
            vector = variant["services"].get("vector")
            if vector:
                existing = profile_set(context.values) - {"openobserve-lab"}
                if lab:
                    existing.add("openobserve-lab")
                vector.setdefault("environment", {})["COMPOSE_PROFILES"] = (
                    ",".join(sorted(existing))
                )
            name = "compose-lab.json" if lab else "compose-normal.json"
            content = canonical(escape_compose(variant))
            write_exclusive(fd, name, content)
            frozen_files[name] = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "mode": 0o600,
            }
        approval = {**snapshot, "frozen_files": frozen_files}
        handle = os.open(
            context.manifest.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=fd,
        )
        with os.fdopen(handle, "wb") as target:
            target.write(canonical(approval))
            target.flush()
            os.fsync(target.fileno())
        os.fsync(fd)
    finally:
        os.close(fd)
    context.identity()
    fd = open_directory(context.env_file.parent / ".lifecycle", create=True)
    try:
        try:
            handle = os.open(
                "runner.token",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=fd,
            )
        except FileExistsError:
            token = private_bytes(fd, "runner.token", maximum=256).decode(
                "ascii"
            )
            require(re.fullmatch(r"[a-f0-9]{64}", token))
        else:
            with os.fdopen(handle, "w") as target:
                target.write(secrets.token_hex(32))
                target.flush()
                os.fsync(target.fileno())
            os.fsync(fd)
    finally:
        os.close(fd)
    return approval


def frozen_bytes(fd, name, mode):
    handle = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
    )
    with os.fdopen(handle, "rb") as source:
        info = os.fstat(source.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == mode
            and info.st_size <= LIMIT
        )
        result = source.read(LIMIT + 1)
        require(len(result) <= LIMIT)
        return result


def approved_source_model(context, approval):
    """Read the protected original resolved model; never call Compose here."""
    expected = approval["frozen_files"]["rendered-source.json"]
    require(expected["mode"] == 0o600)
    fd = open_directory(context.manifest.parent)
    try:
        content = frozen_bytes(fd, "rendered-source.json", 0o600)
    finally:
        os.close(fd)
    require(hashlib.sha256(content).hexdigest() == expected["sha256"])
    result = json.loads(content)
    require(isinstance(result, dict) and result.get("name") == context.project)
    return result


def write_exclusive(fd, name, content, *, mode=0o600):
    """No existing approval/config artifact is silently overwritten."""
    try:
        handle = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=fd,
        )
    except FileExistsError:
        require(frozen_bytes(fd, name, mode) == content)
        return
    with os.fdopen(handle, "wb") as output:
        os.fchmod(output.fileno(), mode)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def escape_compose(value):
    """Preserve dollar signs when Compose parses the resolved model again."""
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [escape_compose(item) for item in value]
    if isinstance(value, dict):
        return {key: escape_compose(item) for key, item in value.items()}
    return value


def verify_approval(context, approval):
    expected = {
        key: value for key, value in approval.items() if key != "frozen_files"
    }
    require(digest(context.snapshot(approval=approval)) == digest(expected))
    fd = open_directory(context.manifest.parent)
    try:
        for name, expected in approval["frozen_files"].items():
            require(
                re.fullmatch(
                    r"(?:rendered-source\.json|compose-(?:normal|lab)\.json|"
                    r"(?:config|bind)-[a-f0-9]{64}\.data)",
                    name,
                )
            )
            require(expected["mode"] in {0o600, 0o644})
            if name.startswith("compose-") or name == "rendered-source.json":
                require(expected["mode"] == 0o600)
            require(
                hashlib.sha256(
                    frozen_bytes(fd, name, expected["mode"])
                ).hexdigest()
                == expected["sha256"]
            )
    finally:
        os.close(fd)


def select_frozen(context, profiles):
    name = (
        "compose-lab.json"
        if "openobserve-lab" in profiles
        else "compose-normal.json"
    )
    context.frozen = context.manifest.parent / name


def operation_argv(context, operation, profiles):
    """Construct only fixed group actions; cannot encode arbitrary commands."""
    require(set(operation) >= {"group_id", "service_id", "action"})
    name = operation["group_id"]
    require(name in context.groups and name in GROUPS)
    group = GROUPS[name]
    require(operation["service_id"] in group.cards)
    action = operation["action"]
    require(action in {"start", "restart", "stop"})
    if action == "stop" and name == "compute":
        require(not profiles & {"notebooks", "sail-lab"}, "PROFILE_DEPENDENCY")
    if action == "stop" and name == "monitoring":
        require("openobserve-lab" not in profiles, "PROFILE_DEPENDENCY")
    if action == "start" and name in {"sail-lab", "openobserve-lab"}:
        other = "sail-lab" if name == "openobserve-lab" else "openobserve-lab"
        require(other not in profiles, "MEMORY_BUDGET")
    if action == "start":
        profiles = profiles | set(group.profiles)
        suffix = (
            "up",
            "--detach",
            "--no-build",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            "120",
            *group.starts,
        )
    elif action == "restart":
        suffix = ("restart", "--no-deps", "--timeout", "30", *group.daemons)
    else:
        profiles = profiles - {name}
        suffix = ("stop", "--timeout", "30", *group.daemons)
    select_frozen(context, profiles)
    steps = [(context.compose() + suffix, context.environment(profiles))]
    if name == "openobserve-lab":
        # Profiles do not conditionally remove a Vector sink. Recreate only
        # Vector so its versioned entrypoint selects the correct fanout.
        steps.append(
            (
                context.compose()
                + (
                    "up",
                    "--detach",
                    "--no-build",
                    "--no-deps",
                    "--force-recreate",
                    "--wait",
                    "--wait-timeout",
                    "120",
                    "vector",
                ),
                context.environment(profiles),
            )
        )
    return steps


def execute(context, approval, operation, *, permit=None):
    """One claim -> one bounded execution. Uncertain outcomes never replay."""
    try:
        verify_approval(context, approval)
        reason = context.readiness().get(operation["group_id"])
        require(
            not reason
            or reason == "MEMORY_BUDGET"
            and operation["action"] == "stop",
            "INITIAL_SETUP_REQUIRED",
        )
        require(
            all(
                healthy(container(context.commands, context.project, service))
                for service in CORE
            ),
            "INITIAL_SETUP_REQUIRED",
        )
        profiles = profile_set(context.values)
        steps = operation_argv(context, operation, profiles)
    except (OperatorError, OSError, ValueError, KeyError, TypeError):
        return {
            "status": "failed",
            "error_code": "APPROVAL_CHANGED",
            "observed_state": "unknown",
        }
    try:
        for arguments, environment in steps:
            require(permit is not None, "OPERATION_UNKNOWN")
            lease = permit()
            group = GROUPS[operation["group_id"]]
            targets = (
                group.starts
                if operation["action"] == "start"
                else group.daemons
            )
            if operation["group_id"] == "openobserve-lab":
                targets = (*targets, "vector")
            context.validate_mounts(approval=approval, services=targets)
            # A fresh nonce-validated server lease is required for EACH Docker
            # mutation; heartbeat alone never grants execution authority.
            require(
                lease["valid_until"] >= time.time() + 140, "OPERATION_UNKNOWN"
            )
            context.commands(arguments, environment=environment, timeout=135)
        observed = context.observations()[operation["service_id"]]
        expected = "stopped" if operation["action"] == "stop" else "running"
        require(observed == expected, "STATE_UNAVAILABLE")
        return {
            "status": "succeeded",
            "error_code": None,
            "observed_state": observed,
        }
    except Exception:
        # Compose could have changed some services before failure or timeout.
        return {
            "status": "unknown",
            "error_code": "OPERATION_UNKNOWN",
            "observed_state": "unknown",
        }


def runner_observations(context, *, clock=None):
    """Date scan start; heartbeat cannot renew the observation timestamp."""
    started = (clock or time.time)()
    observations = context.observations()
    return {"observed_at": started, "observations": observations}


def run(context, *, once=False, reconcile=None):
    """Register/poll in foreground, with independent bounded heartbeat."""
    approval = manifest_read(context)
    verify_approval(context, approval)
    context.identity()
    fd = open_directory(context.env_file.parent / ".lifecycle")
    try:
        token = private_bytes(fd, "runner.token", maximum=256).decode("ascii")
    finally:
        os.close(fd)
    require(re.fullmatch(r"[a-f0-9]{64}", token))
    client = Client(context, context.values["CONTROL_API_TOKEN"], token)
    if reconcile:
        require(re.fullmatch(r"[a-f0-9]{24}", reconcile))
        return client.call(
            "/lifecycle/runner/reconcile",
            {
                "operation_id": reconcile,
                "confirmation": reconcile,
            },
            operator=True,
        )
    runner_id = secrets.token_hex(16)
    state = {
        "project": context.project,
        "runner_id": runner_id,
        "manifest_digest": digest(approval),
        "config_digest": public_settings_digest(client.call("/settings")),
        "groups": list(context.groups),
        "blocked_groups": context.readiness(),
        **runner_observations(context),
    }
    state_lock = threading.Lock()

    def state_copy():
        # Each sender gets an immutable payload snapshot. No heartbeat can
        # combine a new timestamp with the preceding scan's observations.
        with state_lock:
            return json.loads(canonical(state))

    client.call("/lifecycle/runner/register", state, operator=True)
    stop = threading.Event()
    lost = threading.Event()

    def heartbeat():
        while not stop.wait(5):
            try:
                client.call(
                    "/lifecycle/runner/heartbeat", state_copy(), operator=True
                )
            except Exception:
                lost.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        while not lost.is_set():
            verify_approval(context, approval)
            refreshed = {
                **state_copy(),
                "config_digest": public_settings_digest(
                    client.call("/settings")
                ),
                "blocked_groups": context.readiness(),
                **runner_observations(context),
            }
            with state_lock:
                state = refreshed
            client.call(
                "/lifecycle/runner/heartbeat", state_copy(), operator=True
            )
            operation = client.call(
                "/lifecycle/runner/claim",
                {
                    "runner_id": runner_id,
                },
                operator=True,
            )["operation"]
            if operation:
                require(operation["manifest_digest"] == digest(approval))
                outcome = execute(
                    context,
                    approval,
                    operation,
                    permit=lambda: client.call(
                        "/lifecycle/runner/permit",
                        {
                            "operation_id": operation["id"],
                            "lease_id": operation["lease_id"],
                        },
                        operator=True,
                    ),
                )
                client.call(
                    "/lifecycle/runner/result",
                    {
                        "operation_id": operation["id"],
                        "lease_id": operation["lease_id"],
                        **outcome,
                    },
                    operator=True,
                )
            if once:
                return {"ok": True, "state": "poll_completed"}
            stop.wait(2)
        raise OperatorError("STATE_UNAVAILABLE")
    finally:
        stop.set()
        thread.join(timeout=10)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        required=True,
        help="Selected existing private site.env; never echoed.",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help=(
            "New0600 approval file in an existing owner-only0700 directory. "
            "Keep it outside the entire Web bind and checkout."
        ),
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Existing Compose project; checked against demo-api labels.",
    )
    parser.add_argument(
        "--api-port",
        required=True,
        type=int,
        help="Existing loopback demo-api port1024..65535; no new listener.",
    )
    parser.add_argument(
        "--overlay",
        action="append",
        default=[],
        help=("Existing private Compose overlay; repeat in deployment order."),
    )
    parser.add_argument(
        "--groups",
        default=",".join(GROUPS),
        help=(
            "Comma-separated fixed groups; unprepared groups block approval."
        ),
    )
    parser.add_argument(
        "--sudo",
        action="store_true",
        help="Use only existing noninteractive sudo authorization.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--approve",
        action="store_true",
        help="Save frozen authority and runner token; no service mutation.",
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help=(
            "Execute approved requests in foreground. "
            "Do not run alongside Dokploy."
        ),
    )
    mode.add_argument(
        "--reconcile",
        metavar="OPERATION_ID",
        help="Acknowledge a manually checked unknown operation; never replay.",
    )
    options = parser.parse_args(argv)
    try:
        context = Context(
            env_file=options.env_file,
            manifest=options.manifest,
            project=options.project,
            port=options.api_port,
            groups=options.groups.split(","),
            overlays=options.overlay,
            commands=Commands(sudo=options.sudo),
        )
        if options.approve:
            approval = approve(context)
            report = {
                "ok": True,
                "state": "approved",
                "manifest_digest": digest(approval),
                "service_changes": False,
            }
        elif options.run or options.reconcile:
            report = run(context, reconcile=options.reconcile)
        else:
            snapshot = context.snapshot()
            report = {
                "ok": True,
                "state": "plan",
                "mutated": False,
                "manager": "operator-compose",
                "groups": context.groups,
                "blocked_groups": context.readiness(),
                "manifest_digest": digest(snapshot),
            }
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        print('{"ok":false,"state":"stopped","reason":"OPERATION_UNKNOWN"}')
        return 130
    except Exception:
        print(
            '{"ok":false,"state":"blocked",'
            '"reason":"OPERATOR_PREFLIGHT_FAILED"}'
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
