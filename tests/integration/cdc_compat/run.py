"""Opt-in GitHub-hosted synthetic connector compatibility, never deployment.

No site settings, bind mounts, existing volumes or external database addresses.
Only this job's labelled resources can be removed. Raw responses stay private.
Generic sa fixture only: no fixed-demo/least-privilege certification,
no real Iceberg end-to-end and no comparison to the unavailable old image.
Runtime remains unverified until the exact hosted candidate passes.
"""

import argparse
import contextlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[3]
SQL_IMAGE = (
    "mcr.microsoft.com/mssql/server:2022-latest@sha256:"
    "11927f5edc5122658e90b3a80ee36e8aef779310bf771415c1d89271859c73fd"
)
LABEL = "lakehouse.synthetic.cdc-compat"
TOPIC = "fixture.lh_cdc_fixture.dbo.items"
MAPPING = {
    "items": {
        "source_schema": "dbo",
        "source_table": "items",
        "columns": {"id": "id", "value": "value"},
    }
}

STAGES = frozenset(
    {
        "bootstrap",
        "imports",
        "network",
        "sql_start",
        "sql_port",
        "sql_readiness",
        "sql_schema",
        "kafka_start",
        "kafka_readiness",
        "connect_start",
        "connect_readiness",
        "registration",
        "snapshot",
        "insert",
        "update",
        "delete",
        "offset_commit",
        "restart",
        "resume",
        "replay",
        "cleanup",
    }
)
SAFE_ERRORS = frozenset(
    {
        "HOSTED_RUNNER_REQUIRED",
        "RUN_ID_REQUIRED",
        "COMMAND_FAILED",
        "DOCKER_IMAGE_UNAVAILABLE",
        "DOCKER_PLATFORM_UNAVAILABLE",
        "DOCKER_PORT_UNAVAILABLE",
        "DOCKER_NAME_CONFLICT",
        "DOCKER_NETWORK_UNAVAILABLE",
        "DOCKER_RESOURCE_EXHAUSTED",
        "DOCKER_DAEMON_UNAVAILABLE",
        "READINESS_TIMEOUT",
        "EVENT_FORMAT",
        "EVENT_POSITION",
        "RESTART_RESNAPSHOT_OR_NO_PROGRESS",
        "RESTART_DELIVERY_GAP",
        "IMMUTABLE_IMAGES_REQUIRED",
        "OWNED_CLEANUP_INCOMPLETE",
        "LOOPBACK_BIND_REQUIRED",
        "COMMITTED_OFFSET_REQUIRED",
        "REPLAY_STATE_MISMATCH",
    }
)


def failure_report(stage, error):
    """Emit only enumerated codes; never exception bodies or object repr."""
    code = "UNEXPECTED_ERROR"
    if isinstance(error, FixtureError) and error.args:
        candidate = error.args[0]
        if isinstance(candidate, str) and candidate in SAFE_ERRORS:
            code = candidate
    elif isinstance(error, subprocess.TimeoutExpired):
        code = "PROCESS_TIMEOUT"
    elif isinstance(error, ImportError):
        code = "IMPORT_ERROR"
    elif isinstance(error, OSError):
        code = "OS_ERROR"
    elif isinstance(error, ValueError):
        code = "VALUE_ERROR"
    return {
        "status": "failed",
        "code": "CDC_COMPAT_FAILED",
        "stage": stage if stage in STAGES else "bootstrap",
        "reason": code,
    }


class FixtureError(RuntimeError):
    """Only fixed stage codes may reach public output."""


def owner(environment):
    """Refuse local/self-hosted execution and arbitrary resource names."""
    if (
        environment.get("GITHUB_ACTIONS") != "true"
        or environment.get("RUNNER_ENVIRONMENT") != "github-hosted"
    ):
        raise FixtureError("HOSTED_RUNNER_REQUIRED")
    parts = [
        environment.get(k, "") for k in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")
    ]
    if not all(re.fullmatch(r"[0-9]{1,20}", p) for p in parts):
        raise FixtureError("RUN_ID_REQUIRED")
    return "lh-cdc-compat-" + "-".join(parts)


def docker_failure(stderr):
    """Classify captured Docker stderr without returning any of its text."""
    text = stderr.lower()
    patterns = (
        (
            "DOCKER_IMAGE_UNAVAILABLE",
            ("manifest unknown", "manifest not found", "pull access denied"),
        ),
        ("DOCKER_PLATFORM_UNAVAILABLE", ("no matching manifest",)),
        (
            "DOCKER_PORT_UNAVAILABLE",
            ("port is already allocated", "no public port"),
        ),
        ("DOCKER_NAME_CONFLICT", ("container name", "already in use")),
        ("DOCKER_NETWORK_UNAVAILABLE", ("network not found",)),
        ("DOCKER_RESOURCE_EXHAUSTED", ("no space left on device",)),
        (
            "DOCKER_DAEMON_UNAVAILABLE",
            ("cannot connect to the docker daemon",),
        ),
    )
    for code, phrases in patterns:
        if code == "DOCKER_NAME_CONFLICT":
            if all(phrase in text for phrase in phrases):
                return code
        elif any(phrase in text for phrase in phrases):
            return code
    return "COMMAND_FAILED"


def command(parts, *, environment=None, optional=False, timeout=120):
    """Never forward command output/errors that might contain credentials."""
    result = subprocess.run(
        parts,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode and not optional:
        code = (
            docker_failure(result.stderr)
            if parts and parts[0] == "docker"
            else "COMMAND_FAILED"
        )
        raise FixtureError(code)
    return result


def wait_for(check, seconds=180):
    """Bound readiness without treating failure as a successful skip."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (OSError, ValueError):
            pass
        time.sleep(2)
    raise FixtureError("READINESS_TIMEOUT")


def records(output):
    """Decode console output: partition, offset, JSON key, JSON value."""
    result = []
    for line in output.splitlines():
        fields = line.split("\t", 3)
        if len(fields) != 4:
            raise FixtureError("EVENT_FORMAT")
        partition, offset, key, value = fields
        if not partition.startswith("Partition:") or not offset.startswith(
            "Offset:"
        ):
            raise FixtureError("EVENT_POSITION")
        result.append(
            {
                "topic": TOPIC,
                "partition": int(partition.split(":")[1]),
                "offset": int(offset.split(":")[1]),
                "key": None if key == "null" else key,
                "value": None if value == "null" else value,
                "ingested_at": "2026-01-01T00:00:00Z",
            }
        )
    return result


def observed(events, operation, identity, value=None):
    """Match the actual connector's event, not just a topic count."""
    for event in events:
        if event["value"] is None:
            continue
        envelope = json.loads(event["value"])
        row = (
            envelope.get("before")
            if operation == "d"
            else envelope.get("after")
        )
        if (
            envelope.get("op") == operation
            and isinstance(row, dict)
            and row.get("id") == identity
            and (value is None or row.get("value") == value)
        ):
            return True
    return False


def resumed_events(events, boundary):
    """Reject new snapshots even when final materialized rows would match."""
    following = [e for e in events if e["offset"] > boundary]
    if not following or any(
        e["value"] and json.loads(e["value"]).get("op") == "r"
        for e in following
    ):
        raise FixtureError("RESTART_RESNAPSHOT_OR_NO_PROGRESS")
    if sorted({e["offset"] for e in following}) != list(
        range(boundary + 1, max(e["offset"] for e in following) + 1)
    ):
        raise FixtureError("RESTART_DELIVERY_GAP")
    return following


class Fixture:
    """Owned network and containers; no named volumes or host mounts."""

    def __init__(self, prefix):
        self.stage = "bootstrap"
        self.prefix = prefix
        self.network = prefix + "-net"
        self.names = {
            name: prefix + "-" + name for name in ("sql", "kafka", "connect")
        }
        self.environment = {
            k: v
            for k, v in os.environ.items()
            if k in ("PATH", "HOME", "DOCKER_CONFIG")
        }
        self.password = secrets.token_hex(24) + "Aa1!"
        print("::add-mask::" + self.password, flush=True)
        self.environment.update(
            MSSQL_SA_PASSWORD=self.password,
            CDC_SQLSERVER_PASSWORD=self.password,
        )
        lock = json.loads((ROOT / "infra/cdc/images.lock.json").read_text())
        self.images = {k: lock[k]["pinned"] for k in ("kafka", "debezium")}
        if any(
            not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", p)
            for p in self.images.values()
        ):
            raise FixtureError("IMMUTABLE_IMAGES_REQUIRED")

    def mark(self, stage):
        """Record fixed stage entry; not a claim of whole-test success."""
        if stage not in STAGES:
            raise ValueError("Invalid diagnostic stage.")
        self.stage = stage
        print(json.dumps({"status": "started", "stage": stage}), flush=True)

    def docker(self, *parts, **kwargs):
        return command(
            ["docker", *parts], environment=self.environment, **kwargs
        )

    def cleanup(self):
        """Check exact names and labels; never prune other resources."""
        failed = False
        for kind, names in (
            ("container", list(self.names.values())),
            ("network", [self.network]),
        ):
            for name in names:
                result = self.docker(kind, "inspect", name, optional=True)
                if result.returncode:
                    args = [kind, "ls"]
                    if kind == "container":
                        args += ["--all"]
                    args += [
                        "--filter",
                        "name=" + name,
                        "--format",
                        "{{.Names}}" if kind == "container" else "{{.Name}}",
                    ]
                    listing = self.docker(*args, optional=True)
                    if (
                        listing.returncode
                        or name in listing.stdout.splitlines()
                    ):
                        failed = True
                    continue
                item = json.loads(result.stdout)[0]
                labels = (
                    item.get("Config", {}).get("Labels", {})
                    if kind == "container"
                    else item.get("Labels", {})
                )
                if labels.get(LABEL) != self.prefix:
                    failed = True
                    continue
                args = (
                    ["rm", "--force", "--volumes", name]
                    if kind == "container"
                    else ["network", "rm", name]
                )
                if self.docker(*args, optional=True).returncode:
                    failed = True
        if failed:
            raise FixtureError("OWNED_CLEANUP_INCOMPLETE")

    def start(self, name, image, environment, port=None, alias=None):
        parts = [
            "run",
            "--platform",
            "linux/amd64",
            "--detach",
            "--name",
            self.names[name],
            "--network",
            self.network,
            "--label",
            LABEL + "=" + self.prefix,
        ]
        if alias:
            parts += ["--network-alias", alias]
        if port:
            parts += ["--publish", "127.0.0.1::" + str(port)]
        for key, value in environment.items():
            parts += ["--env", key if value is None else key + "=" + value]
        parts.append(image)
        self.docker(*parts, timeout=240)

    def port(self, name, port):
        value = self.docker(
            "port", self.names[name], str(port) + "/tcp"
        ).stdout.strip()
        if not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", value):
            raise FixtureError("LOOPBACK_BIND_REQUIRED")
        return int(value.rsplit(":", 1)[1])

    def sql(self, query, database="master"):
        import pymssql

        with pymssql.connect(
            server="127.0.0.1",
            port=str(self.sql_port),
            user="sa",
            password=self.password,
            database=database,
            login_timeout=5,
            timeout=30,
            autocommit=True,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query)
        return True

    def get(self, path):
        with build_opener(ProxyHandler({})).open(
            self.endpoint + path, timeout=5
        ) as response:
            return json.load(response)

    def events(self):
        result = self.docker(
            "exec",
            self.names["kafka"],
            "/opt/kafka/bin/kafka-console-consumer.sh",
            "--bootstrap-server",
            "cdc-kafka:9092",
            "--topic",
            TOPIC,
            "--from-beginning",
            "--timeout-ms",
            "4000",
            "--property",
            "print.partition=true",
            "--property",
            "print.offset=true",
            "--property",
            "print.key=true",
            optional=True,
            timeout=20,
        )
        # Console consumer normally exits nonzero after its bounded timeout.
        # A command failure is never a pass: event assertions still must hold.
        return records(result.stdout)

    def require_event(self, operation, identity, value=None):
        return wait_for(
            lambda: (
                e
                if observed(e := self.events(), operation, identity, value)
                else None
            )
        )

    def committed(self, minimum):
        """Require durable source position, not a timer or topic presence."""
        entries = self.get("/connectors/fixture-source/offsets").get(
            "offsets", []
        )
        return (
            entries
            if any(
                isinstance(e.get("offset", {}).get("commit_lsn"), str)
                and e["offset"]["commit_lsn"] >= minimum
                for e in entries
            )
            else None
        )

    def run(self):
        self.mark("imports")
        sys.path.insert(0, str(ROOT / "infra/cdc"))
        import connector
        import state

        self.mark("network")
        self.docker(
            "network",
            "create",
            "--internal",
            "--label",
            LABEL + "=" + self.prefix,
            self.network,
        )
        self.mark("sql_start")
        self.start(
            "sql",
            SQL_IMAGE,
            {
                "ACCEPT_EULA": "Y",
                "MSSQL_PID": "Developer",
                "MSSQL_AGENT_ENABLED": "true",
                "MSSQL_SA_PASSWORD": None,
            },
            port=1433,
            alias="fixture-sql",
        )
        self.mark("sql_port")
        self.sql_port = self.port("sql", 1433)

        def sql_ready():
            try:
                return self.sql("SELECT 1")
            except Exception:
                return False

        self.mark("sql_readiness")
        wait_for(sql_ready, 240)
        self.mark("sql_schema")
        self.sql("CREATE DATABASE lh_cdc_fixture")
        self.sql(
            """
EXEC sys.sp_cdc_enable_db;
CREATE TABLE dbo.items (id int PRIMARY KEY, value nvarchar(40) NOT NULL);
EXEC sys.sp_cdc_enable_table @source_schema=N'dbo', @source_name=N'items',
 @role_name=N'fixture_cdc_reader', @supports_net_changes=0;
INSERT dbo.items VALUES (1,N'initial');
""",
            "lh_cdc_fixture",
        )
        # Disposable sa isolates image compatibility, not permission safety.
        # It does NOT certify production least-privilege or fixed-demo guards.
        self.mark("kafka_start")
        self.start(
            "kafka",
            self.images["kafka"],
            {
                "CLUSTER_ID": "MkU3OEVBNTcwNTJENDM2Qk",
                "KAFKA_NODE_ID": "1",
                "KAFKA_PROCESS_ROLES": "broker,controller",
                "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
                "KAFKA_ADVERTISED_LISTENERS": "PLAINTEXT://cdc-kafka:9092",
                "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": (
                    "PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT"
                ),
                "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
                "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@cdc-kafka:9093",
                "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1",
                "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
                "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1",
                "KAFKA_NUM_PARTITIONS": "1",
                "KAFKA_HEAP_OPTS": "-Xms256m -Xmx512m",
            },
            alias="cdc-kafka",
        )
        self.mark("kafka_readiness")
        wait_for(
            lambda: (
                self.docker(
                    "exec",
                    self.names["kafka"],
                    "/opt/kafka/bin/kafka-topics.sh",
                    "--bootstrap-server",
                    "cdc-kafka:9092",
                    "--list",
                    optional=True,
                    timeout=15,
                ).returncode
                == 0
            )
        )
        self.mark("connect_start")
        self.start(
            "connect",
            self.images["debezium"],
            {
                "BOOTSTRAP_SERVERS": "cdc-kafka:9092",
                "GROUP_ID": "fixture-connect",
                "CONFIG_STORAGE_TOPIC": "_fixture_configs",
                "OFFSET_STORAGE_TOPIC": "_fixture_offsets",
                "STATUS_STORAGE_TOPIC": "_fixture_status",
                "CONNECT_CONFIG_STORAGE_REPLICATION_FACTOR": "1",
                "CONNECT_OFFSET_STORAGE_REPLICATION_FACTOR": "1",
                "CONNECT_STATUS_STORAGE_REPLICATION_FACTOR": "1",
                "CONNECT_OFFSET_FLUSH_INTERVAL_MS": "1000",
                "CONNECT_CONFIG_PROVIDERS": "env",
                "CONNECT_CONFIG_PROVIDERS_ENV_CLASS": (
                    "org.apache.kafka.common.config.provider."
                    "EnvVarConfigProvider"
                ),
                "CDC_SQLSERVER_USER": "sa",
                "CDC_SQLSERVER_PASSWORD": None,
                "HEAP_OPTS": "-Xms256m -Xmx512m",
            },
            port=8083,
        )
        self.endpoint = "http://127.0.0.1:" + str(self.port("connect", 8083))
        self.mark("connect_readiness")
        wait_for(lambda: isinstance(self.get("/connectors"), list))
        values = {
            "CDC_SOURCE_MODE": "generic",
            "CDC_SQLSERVER_HOST": "fixture-sql",
            "CDC_SQLSERVER_PORT": "1433",
            "CDC_SQLSERVER_DATABASE": "lh_cdc_fixture",
            "CDC_CONNECTOR_NAME": "fixture-source",
            "CDC_TOPIC_PREFIX": "fixture",
            "CDC_TEST_TRUST_SERVER_CERTIFICATE": "true",
        }
        self.mark("registration")
        with contextlib.redirect_stdout(io.StringIO()):
            connector.register(values, MAPPING, self.endpoint)
            connector.register(values, MAPPING, self.endpoint)
        self.mark("snapshot")
        self.require_event("r", 1, "initial")
        self.mark("insert")
        self.sql("INSERT dbo.items VALUES (2,N'inserted')", "lh_cdc_fixture")
        self.require_event("c", 2, "inserted")
        self.mark("update")
        self.sql(
            "UPDATE dbo.items SET value=N'updated' WHERE id=2",
            "lh_cdc_fixture",
        )
        self.require_event("u", 2, "updated")
        self.mark("delete")
        self.sql("DELETE dbo.items WHERE id=2", "lh_cdc_fixture")
        deleted = self.require_event("d", 2)
        delete_lsn = max(
            json.loads(e["value"])["source"]["commit_lsn"]
            for e in deleted
            if e["value"] and json.loads(e["value"]).get("op") == "d"
        )
        self.mark("offset_commit")
        offsets = wait_for(lambda: self.committed(delete_lsn))
        if not offsets:
            raise FixtureError("COMMITTED_OFFSET_REQUIRED")
        boundary = max(e["offset"] for e in self.events())
        self.mark("restart")
        self.docker("restart", self.names["connect"])
        wait_for(
            lambda: (
                self.get("/connectors/fixture-source/status")
                .get("connector", {})
                .get("state")
                == "RUNNING"
            )
        )
        wait_for(lambda: self.committed(delete_lsn))
        self.mark("resume")
        self.sql("INSERT dbo.items VALUES (3,N'resumed')", "lh_cdc_fixture")
        events = self.require_event("c", 3, "resumed")
        resumed_events(events, boundary)
        # Replay actual envelopes through production SQLite LSN/version logic.
        self.mark("replay")
        with tempfile.TemporaryDirectory(prefix="cdc-compat-") as directory:
            store = state.State(
                Path(directory) / "state.sqlite3", {"source_mode": "generic"}
            )
            try:
                for batch in (events, events, list(reversed(events))):
                    for event in batch:
                        store.stage(
                            event, "items", MAPPING["items"], ["id", "value"]
                        )
                    rows = [
                        {k: row[k] for k in ("id", "value")}
                        for row in store.rows("items")
                    ]
                    if rows != [
                        {"id": "1", "value": "initial"},
                        {"id": "3", "value": "resumed"},
                    ]:
                        raise FixtureError("REPLAY_STATE_MISMATCH")
            finally:
                store.connection.close()


def main():
    """No runtime work without explicit consent and hosted-runner guard."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if not args.run and not args.cleanup:
        print('{"status":"plan","runtime":"not_run"}')
        return 0
    fixture = None
    try:
        fixture = Fixture(owner(os.environ))
        if args.run:
            try:
                fixture.run()
            finally:
                fixture.cleanup()
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "scope": "generic-connector-and-sqlite-replay",
                        "iceberg_e2e": "not_tested",
                        "fixed_demo_permissions": "not_tested",
                    }
                )
            )
        else:
            fixture.cleanup()
        return 0
    except Exception as error:
        # Never print raw driver errors, configuration, events or tokens.
        stage = getattr(fixture, "stage", "bootstrap")
        if isinstance(error, FixtureError) and error.args == (
            "OWNED_CLEANUP_INCOMPLETE",
        ):
            stage = "cleanup"
        print(json.dumps(failure_report(stage, error)), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
