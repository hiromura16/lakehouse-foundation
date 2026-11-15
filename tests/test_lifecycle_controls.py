"""Exercise durable GUI/operator contracts with synthetic files and ASGI only.

No test in this module connects to Docker, a bound HTTP port or any database.
Mocked command proofs verify argv/trust boundaries, not Compose runtime health.
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/demo"))
sys.path.insert(0, str(ROOT / "scripts"))
from lifecycle_api import install_lifecycle  # noqa: E402
from lifecycle_catalog import GROUPS  # noqa: E402
from lifecycle_store import (  # noqa: E402
    Journal,
    public_settings_digest,
)

SPEC = importlib.util.spec_from_file_location(
    "lifecycle_operator_subject", ROOT / "scripts/lifecycle_operator.py"
)
OPERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OPERATOR)


def private_file(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)


class Store:
    """Keep test settings synthetic, without network or real credentials."""

    def __init__(self, path):
        self.path = path
        self.values = {
            "COMPOSE_PROJECT_NAME": "synthetic-poc",
            "COMPOSE_PROFILES": "demo",
            "SAFE_VALUE": "sample",
        }
        self.applied = dict(self.values)

    def public(self):
        return {
            "values": dict(self.values),
            "configured_secrets": ["CONTROL_API_TOKEN"],
        }

    def read(self):
        return dict(self.values)

    def save(self, values):
        self.values.update(values)
        return self.public()


class LifecycleApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lifecycle-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = Store(self.directory / "site.env")
        self.jobs = SimpleNamespace(operation=threading.Lock())
        self.idle = mock.Mock(return_value=True)
        self.app = FastAPI()

        def authorize(authorization: str = Header(default="")):
            if authorization != "Bearer synthetic-owner":
                raise HTTPException(401, "Unauthorized")

        install_lifecycle(
            self.app, self.store, self.jobs, authorize, worker_idle=self.idle
        )

        @self.app.post("/jobs/test")
        def existing_write():
            return {"ok": True}

        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.journal = self.app.state.lifecycle_journal
        self.clock = mock.Mock(return_value=1000)
        self.journal.clock = self.clock
        self.owner = {"Authorization": "Bearer synthetic-owner"}
        self.runner = {"Authorization": "Bearer " + "a" * 64}
        self.state = {
            "project": "synthetic-poc",
            "runner_id": "b" * 32,
            "manifest_digest": "c" * 64,
            "config_digest": public_settings_digest(self.store.public()),
            "groups": list(GROUPS),
            "blocked_groups": {},
            "observations": {"grafana": "stopped"},
            "observed_at": 1000,
        }

    def call(self, method, path, payload=None, *, runner=False):
        options = {"headers": self.runner if runner else self.owner}
        if payload is not None:
            options["json"] = payload
        return self.client.request(method, "/lifecycle" + path, **options)

    def register(self):
        self.journal.directory.mkdir(mode=0o700)
        private_file(self.journal.directory / "runner.token", b"a" * 64)
        result = self.call("POST", "/runner/register", self.state, runner=True)
        self.assertEqual(result.status_code, 200, result.text)

    def submit(
        self, *, service="grafana", action="start", key="synthetic-key-1"
    ):
        plan = self.call(
            "POST", "/plans", {"service_id": service, "action": action}
        )
        self.assertEqual(plan.status_code, 200, plan.text)
        request = {
            "plan_id": plan.json()["id"],
            "confirmation": plan.json()["confirmation"],
            "idempotency_key": key,
        }
        response = self.call("POST", "/operations", request)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json(), request

    def claim(self):
        response = self.call(
            "POST", "/runner/claim", {"runner_id": "b" * 32}, runner=True
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["operation"]

    def test_disconnected_get_is_mutation_free_and_auth_is_required(self):
        self.assertEqual(
            self.client.get("/lifecycle/capabilities").status_code, 401
        )
        result = self.call("GET", "/capabilities").json()
        self.assertFalse(result["connected"])
        self.assertFalse(self.journal.directory.exists())
        self.assertTrue(
            all(not item["allowed_actions"] for item in result["services"])
        )
        self.assertEqual(
            self.call(
                "POST",
                "/plans",
                {
                    "service_id": "grafana",
                    "action": "start",
                },
            ).status_code,
            409,
        )

    def test_owner_cannot_impersonate_operator_or_read_runner_secret(self):
        self.register()
        result = self.call("POST", "/runner/claim", {"runner_id": "b" * 32})
        self.assertEqual(result.status_code, 401)
        self.assertNotIn("a" * 64, self.call("GET", "/capabilities").text)
        self.assertNotIn(
            "/lifecycle/runner/", self.client.get("/openapi.json").text
        )

    def test_allowlist_protects_self_core_cdc_host_and_arbitrary_commands(
        self,
    ):
        self.register()
        for service in (
            "demo",
            "demo-api",
            "catalog",
            "worker",
            "trino",
            "cdc-consumer",
            "cadvisor",
            "arbitrary-service",
        ):
            with self.subTest(service=service):
                result = self.call(
                    "POST", "/plans", {"service_id": service, "action": "stop"}
                )
                self.assertEqual(result.status_code, 409)
        self.assertEqual(
            self.call(
                "POST",
                "/plans",
                {
                    "service_id": "grafana",
                    "action": "start",
                    "command": "synthetic",
                },
            ).status_code,
            422,
        )

    def test_full_queue_claim_result_and_profiles_do_not_change_applied(self):
        self.register()
        operation, request = self.submit()
        self.assertTrue(self.jobs.operation.locked())
        self.assertEqual(self.client.post("/jobs/test").status_code, 409)
        claimed = self.claim()
        self.assertEqual(claimed["id"], operation["id"])
        self.assertIsNone(self.claim())
        result = self.call(
            "POST",
            "/runner/result",
            {
                "operation_id": operation["id"],
                "lease_id": claimed["lease_id"],
                "status": "succeeded",
                "observed_state": "running",
            },
            runner=True,
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["status"], "succeeded")
        self.assertEqual(self.store.applied["COMPOSE_PROFILES"], "demo")
        self.assertIn("monitoring", self.store.values["COMPOSE_PROFILES"])
        self.assertFalse(self.jobs.operation.locked())
        self.clock.return_value += 1000
        repeated = self.call("POST", "/operations", request)
        self.assertEqual(repeated.json()["id"], operation["id"])
        history = self.call("GET", "/operations?limit=10").json()["operations"]
        self.assertEqual([item["id"] for item in history], [operation["id"]])
        self.assertEqual(
            self.call("GET", "/operations/by-key/synthetic-key-1").json()[
                "id"
            ],
            operation["id"],
        )
        self.assertNotIn("lease_id", json.dumps(history))

    def test_expired_claim_is_unknown_until_reconciled(
        self,
    ):
        self.register()
        operation, _ = self.submit()
        claimed = self.claim()
        self.clock.return_value += 301
        observed = self.call("GET", "/operations/" + operation["id"]).json()
        self.assertEqual(observed["status"], "unknown")
        self.assertIsNone(
            self.call("POST", "/runner/register", self.state, runner=True)
            .json()
            .get("operation")
        )
        self.assertIsNone(self.claim())
        stale = self.call(
            "POST",
            "/runner/result",
            {
                "operation_id": operation["id"],
                "lease_id": claimed["lease_id"],
                "status": "succeeded",
                "observed_state": "running",
            },
            runner=True,
        )
        self.assertEqual(stale.status_code, 409)
        self.assertNotIn("monitoring", self.store.values["COMPOSE_PROFILES"])
        reconciled = self.call(
            "POST",
            "/runner/reconcile",
            {
                "operation_id": operation["id"],
                "confirmation": operation["id"],
            },
            runner=True,
        )
        self.assertEqual(reconciled.json()["status"], "failed")
        self.assertFalse(self.jobs.operation.locked())

    def test_jobs_worker_readiness_digests_dependencies_and_confirmation(self):
        self.register()
        self.idle.return_value = False
        self.assertEqual(
            self.call(
                "POST", "/plans", {"service_id": "grafana", "action": "start"}
            ).status_code,
            409,
        )
        self.idle.return_value = True
        self.store.values["COMPOSE_PROFILES"] = "demo,notebooks"
        self.assertEqual(
            self.call(
                "POST",
                "/plans",
                {"service_id": "spark-master", "action": "stop"},
            ).status_code,
            409,
        )
        self.store.values["COMPOSE_PROFILES"] = "demo,openobserve-lab"
        self.assertEqual(
            self.call(
                "POST", "/plans", {"service_id": "grafana", "action": "stop"}
            ).status_code,
            409,
        )
        self.store.values["COMPOSE_PROFILES"] = "demo"
        plan = self.call(
            "POST", "/plans", {"service_id": "grafana", "action": "start"}
        ).json()
        request = {
            "plan_id": plan["id"],
            "confirmation": "d" * 32,
            "idempotency_key": "synthetic-confirm",
        }
        self.assertEqual(
            self.call("POST", "/operations", request).status_code, 409
        )
        self.assertFalse(self.jobs.operation.locked())
        self.store.values["SAFE_VALUE"] = "changed"
        request["confirmation"] = plan["confirmation"]
        self.assertEqual(
            self.call("POST", "/operations", request).status_code, 409
        )
        self.assertFalse(self.jobs.operation.locked())

    def test_queue_restarts_and_symlink_state_fail_closed(self):
        self.register()
        operation, _ = self.submit()
        second = Journal(self.journal.directory, clock=self.clock)
        with second.transaction(create=False) as data:
            self.assertEqual(
                data["operations"][operation["id"]]["status"], "queued"
            )
        path = self.journal.directory / "journal.json"
        saved = path.read_bytes()
        path.unlink()
        target = self.directory / "unrelated.json"
        private_file(target, saved)
        path.symlink_to(target)
        self.assertEqual(self.call("GET", "/capabilities").status_code, 409)
        self.assertEqual(target.read_bytes(), saved)

    def test_permit_binds_generation_and_never_revives_expired_lease(
        self,
    ):
        self.register()
        operation, _ = self.submit()
        claimed = self.claim()
        payload = {
            "operation_id": operation["id"],
            "lease_id": claimed["lease_id"],
        }
        self.assertEqual(
            self.call(
                "POST", "/runner/permit", payload, runner=True
            ).status_code,
            200,
        )
        self.clock.return_value += 31
        replacement = {**self.state, "runner_id": "d" * 32}
        self.assertEqual(
            self.call(
                "POST", "/runner/register", replacement, runner=True
            ).status_code,
            200,
        )
        self.assertEqual(
            self.call(
                "POST", "/runner/permit", payload, runner=True
            ).status_code,
            409,
        )
        self.assertEqual(
            self.call(
                "POST",
                "/runner/result",
                {
                    **payload,
                    "status": "succeeded",
                    "observed_state": "running",
                },
                runner=True,
            ).status_code,
            409,
        )
        self.clock.return_value += 301
        self.assertEqual(
            self.call(
                "POST", "/runner/permit", payload, runner=True
            ).status_code,
            409,
        )
        self.assertEqual(
            self.call("GET", "/operations/" + operation["id"]).json()[
                "status"
            ],
            "unknown",
        )

    def test_low_memory_preserves_stop_but_blocks_lab_start(self):
        self.state["blocked_groups"] = {"sail-lab": "MEMORY_BUDGET"}
        self.register()
        rows = self.call("GET", "/capabilities").json()["services"]
        sail = next(row for row in rows if row["service_id"] == "sail-server")
        self.assertEqual(sail["allowed_actions"], ["stop"])
        self.assertIn("MEMORY_BUDGET", sail["blocked_reasons"])
        for action, expected in (
            ("start", 409),
            ("restart", 409),
            ("stop", 200),
        ):
            response = self.call(
                "POST",
                "/plans",
                {
                    "service_id": "sail-server",
                    "action": action,
                },
            )
            self.assertEqual(response.status_code, expected, response.text)

    def test_heartbeat_does_not_refresh_stale_service_observations(self):
        self.register()
        for now, expected in ((1020, "stopped"), (1041, "unknown")):
            self.clock.return_value = now
            response = self.call(
                "POST", "/runner/heartbeat", self.state, runner=True
            )
            self.assertEqual(response.status_code, 200)
            capability = self.call("GET", "/capabilities").json()
            self.assertTrue(capability["connected"])
            self.assertEqual(capability["last_seen"], now)
            row = next(
                row
                for row in capability["services"]
                if row["service_id"] == "grafana"
            )
            self.assertEqual(row["last_seen"], 1000)
            self.assertEqual(row["observed_state"], expected)

    def test_missing_future_or_malformed_observation_time(self):
        self.state.pop("observed_at")
        self.register()
        capability = self.call("GET", "/capabilities").json()
        row = next(
            row
            for row in capability["services"]
            if row["service_id"] == "grafana"
        )
        self.assertIsNone(row["last_seen"])
        self.assertEqual(row["observed_state"], "unknown")
        for value, status in (
            ("1000", 422),
            (True, 422),
            (-1, 422),
            (1001, 409),
        ):
            response = self.call(
                "POST",
                "/runner/heartbeat",
                {
                    **self.state,
                    "observed_at": value,
                },
                runner=True,
            )
            self.assertEqual(response.status_code, status, response.text)

    def test_result_never_renews_other_cards_observation_timestamp(self):
        self.state["observations"]["sail-server"] = "running"
        self.register()
        operation, _ = self.submit()
        claimed = self.claim()
        self.clock.return_value = 1010
        response = self.call(
            "POST",
            "/runner/result",
            {
                "operation_id": operation["id"],
                "lease_id": claimed["lease_id"],
                "status": "succeeded",
                "observed_state": "running",
            },
            runner=True,
        )
        self.assertEqual(response.status_code, 200, response.text)
        rows = self.call("GET", "/capabilities").json()["services"]
        sail = next(row for row in rows if row["service_id"] == "sail-server")
        self.assertEqual(sail["last_seen"], 1000)


class GatewayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """An owner browser cannot access any operator route through the BFF."""

    async def test_runner_routes_never_forward_even_get_or_encoded_paths(self):
        import portal_gateway

        temporary = tempfile.TemporaryDirectory(prefix="lifecycle-bff-test-")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        (directory / "index.html").write_text("synthetic")
        calls = []

        async def backend(request):
            if request.url.path == "/portal-auth/verify":
                return httpx.Response(
                    200,
                    json={
                        "valid": True,
                        "revision": "b" * 64,
                    },
                )
            if request.url.path == "/portal-auth/status":
                return httpx.Response(
                    200,
                    json={
                        "configured": False,
                        "revision": "b" * 64,
                    },
                )
            calls.append(request)
            return httpx.Response(200, json={"synthetic": True})

        application = portal_gateway.create_app(
            {
                "CONTROL_API_TOKEN": "a" * 32,
                "PORTAL_DIST_DIR": str(directory),
                "PORTAL_PROJECT_NAME": "synthetic-poc",
            },
            transport=httpx.MockTransport(backend),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://localhost:8501",
        ) as client:
            headers = {"Origin": "http://localhost:8501", "X-Poc-Request": "1"}
            login = await client.post(
                "/api/session", headers=headers, json={"token": "a" * 32}
            )
            self.assertEqual(login.status_code, 200)
            for method in ("GET", "POST", "PATCH", "DELETE"):
                for path in (
                    "lifecycle/runner/register",
                    "lifecycle/runner",
                    "lifecycle/%72unner/claim",
                    "lifecycle%2frunner/result",
                ):
                    response = await client.request(
                        method,
                        "/api/control/" + path,
                        headers=headers,
                    )
                    self.assertIn(response.status_code, {400, 403})
            self.assertEqual(calls, [])
            response = await client.get("/api/control/lifecycle/capabilities")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(calls), 1)


class OperatorContractTests(unittest.TestCase):
    """Pure/mock host-runner tests; never open a Docker socket or HTTP port."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="lifecycle-runner-synthetic-"
        )
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "private").mkdir(mode=0o700)
        (self.root / "operator").mkdir(mode=0o700)
        self.context = OPERATOR.Context(
            env_file=self.root / "private/site.env",
            manifest=self.root / "operator/approval.json",
            project="synthetic-poc",
            port=28502,
            groups=(
                "monitoring",
                "automation",
                "compute",
                "notebooks",
                "openobserve-lab",
                "sail-lab",
            ),
            commands=mock.Mock(),
        )
        self.context.values = {
            "COMPOSE_PROJECT_NAME": "synthetic-poc",
            "COMPOSE_PROFILES": "demo",
            "HTTP_PROXY": "http://synthetic.invalid:8080",
            "NO_PROXY": "worker,localhost",
            "CONTROL_API_TOKEN": "synthetic-secret",
        }

    def test_execution_uses_only_frozen_model_and_fixed_groups(self):
        for group in GROUPS.values():
            if group.id == "bi":
                continue
            for action in ("start", "restart", "stop"):
                operation = {
                    "group_id": group.id,
                    "service_id": group.cards[0],
                    "action": action,
                }
                profiles = (
                    {"demo", "openobserve-lab"}
                    if group.id == "openobserve-lab" and action == "restart"
                    else {"demo"}
                )
                steps = OPERATOR.operation_argv(
                    self.context, operation, profiles
                )
                for arguments, environment in steps:
                    self.assertEqual(
                        arguments[:4],
                        (
                            "compose",
                            "--project-name",
                            "synthetic-poc",
                            "--file",
                        ),
                    )
                    self.assertNotIn("--env-file", arguments)
                    self.assertNotIn(str(self.context.env_file), arguments)
                    self.assertIn(
                        str(self.context.manifest.parent), arguments[4]
                    )
                    self.assertFalse(
                        {"build", "pull", "down", "rm", "prune", "exec"}
                        & set(arguments)
                    )
                    self.assertNotIn("CONTROL_API_TOKEN", environment)
                    if action == "stop":
                        self.assertNotIn("n8n-db", arguments)
                        self.assertNotIn("spark-volumes-init", arguments)
                if group.id == "openobserve-lab":
                    self.assertEqual(len(steps), 2)
                    self.assertEqual(steps[-1][0][-1], "vector")
                    model = (
                        "compose-normal.json"
                        if action == "stop"
                        else "compose-lab.json"
                    )
                    self.assertTrue(steps[-1][0][4].endswith(model))

    def test_reject_arbitrary_targets_and_shared_dependency_stops(self):
        for value in ("worker", "demo-api", "unknown"):
            with self.assertRaises(OPERATOR.OperatorError):
                OPERATOR.operation_argv(
                    self.context,
                    {"group_id": value, "service_id": value, "action": "stop"},
                    set(),
                )
        for group, profiles in (
            ("compute", {"notebooks"}),
            ("compute", {"sail-lab"}),
            ("monitoring", {"openobserve-lab"}),
        ):
            with self.assertRaises(OPERATOR.OperatorError):
                OPERATOR.operation_argv(
                    self.context,
                    {
                        "group_id": group,
                        "service_id": GROUPS[group].cards[0],
                        "action": "stop",
                    },
                    profiles,
                )
        self.assertNotIn(
            "spark-check",
            {service for group in GROUPS.values() for service in group.starts},
        )
        self.assertNotIn(
            "sail-benchmark",
            {service for group in GROUPS.values() for service in group.starts},
        )

    def test_api_identity_checked_before_loading_any_credential(self):
        self.context.identity = mock.Mock(
            side_effect=OPERATOR.OperatorError("OPERATOR_AUTH_REQUIRED")
        )
        with mock.patch.object(OPERATOR.settings, "load") as load:
            with self.assertRaises(OPERATOR.OperatorError):
                self.context.load()
            load.assert_not_called()
        client = OPERATOR.Client(self.context, "synthetic-owner", "a" * 64)
        with mock.patch.object(
            OPERATOR.http.client, "HTTPConnection"
        ) as connection:
            with self.assertRaises(OPERATOR.OperatorError):
                client.call("/lifecycle/runner/claim", {}, operator=True)
            connection.assert_not_called()

    def test_api_identity_requires_loopback_project_and_private_mount(self):
        value = {
            "Id": "e" * 64,
            "Image": "sha256:" + "f" * 64,
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "NetworkSettings": {
                "Ports": {
                    "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "28502"}]
                }
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(self.context.env_file.parent),
                    "Destination": "/private",
                    "RW": True,
                }
            ],
            "HostConfig": {},
        }
        with mock.patch.object(OPERATOR, "container", return_value=value):
            self.assertEqual(
                OPERATOR.verify_api(
                    self.context.commands,
                    "synthetic-poc",
                    28502,
                    self.context.env_file.parent,
                )["id"],
                "e" * 64,
            )
            value["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostIp"] = (
                "0.0.0.0"
            )
            with self.assertRaises(OPERATOR.OperatorError):
                OPERATOR.verify_api(
                    self.context.commands,
                    "synthetic-poc",
                    28502,
                    self.context.env_file.parent,
                )

    def test_manifest_outside_web_bind_and_frozen_dollar_preservation(self):
        with self.assertRaises(OPERATOR.OperatorError):
            OPERATOR.Context(
                env_file=self.context.env_file,
                manifest=self.context.env_file.parent / "approval.json",
                project="synthetic-poc",
                port=28502,
                groups=("monitoring",),
            )
        self.assertEqual(
            OPERATOR.escape_compose({"secret": "synthetic$a$$b"}),
            {"secret": "synthetic$$a$$$$b"},
        )
        path = self.root / "target"
        private_file(path, b"synthetic")
        (self.root / "link").symlink_to(path)
        with self.assertRaises(OSError):
            OPERATOR.file_bytes(self.root / "link")

    def test_approval_verifies_frozen_bytes_and_rejects_source_changes(self):
        snapshot = {"version": 1, "project": "synthetic-poc"}
        model = b'{"services":{}}'
        private_file(
            self.context.manifest.parent / "compose-normal.json", model
        )
        approval = {
            **snapshot,
            "frozen_files": {
                "compose-normal.json": {
                    "sha256": OPERATOR.hashlib.sha256(model).hexdigest(),
                    "mode": 0o600,
                }
            },
        }
        self.context.snapshot = mock.Mock(return_value=snapshot)
        OPERATOR.verify_approval(self.context, approval)
        self.context.snapshot.return_value = {
            **snapshot,
            "project": "unexpected",
        }
        with self.assertRaises(OPERATOR.OperatorError):
            OPERATOR.verify_approval(self.context, approval)
        self.context.snapshot.return_value = snapshot
        (self.context.manifest.parent / "compose-normal.json").write_bytes(
            b"changed"
        )
        with self.assertRaises(OPERATOR.OperatorError):
            OPERATOR.verify_approval(self.context, approval)

    def test_partial_command_failure_is_unknown_never_success_or_retry(self):
        operation = {
            "group_id": "monitoring",
            "service_id": "grafana",
            "action": "start",
        }
        self.context.readiness = mock.Mock(return_value={})
        self.context.validate_mounts = mock.Mock()
        self.context.commands.side_effect = OPERATOR.OperatorError(
            "OPERATOR_COMMAND_FAILED"
        )
        with (
            mock.patch.object(OPERATOR, "verify_approval"),
            mock.patch.object(
                OPERATOR,
                "container",
                return_value={"State": {"Running": True}},
            ),
        ):
            result = OPERATOR.execute(
                self.context,
                {},
                operation,
                permit=lambda: {"valid_until": OPERATOR.time.time() + 300},
            )
        self.assertEqual(
            result,
            {
                "status": "unknown",
                "error_code": "OPERATION_UNKNOWN",
                "observed_state": "unknown",
            },
        )
        self.assertEqual(self.context.commands.call_count, 1)

    def test_double_slash_aliases_are_rejected_in_both_directions(self):
        for env_file, manifest in (
            ("//tmp/synthetic/site.env", "/tmp/synthetic/approval.json"),
            ("/tmp/synthetic/site.env", "//tmp/synthetic/approval.json"),
        ):
            with self.assertRaises(OPERATOR.OperatorError):
                OPERATOR.Context(
                    env_file=env_file,
                    manifest=manifest,
                    project="synthetic-poc",
                    port=28502,
                    groups=("monitoring",),
                )
        with self.assertRaises(OPERATOR.OperatorError):
            OPERATOR.Context(
                env_file=self.context.env_file,
                manifest=self.context.manifest,
                project="synthetic-poc",
                port=28502,
                groups=("monitoring",),
                overlays=("//tmp/synthetic-overlay.yaml",),
            )

    def test_lease_expiry_prevents_delayed_new_mutation(self):
        operation = {
            "group_id": "monitoring",
            "service_id": "grafana",
            "action": "start",
        }
        self.context.readiness = mock.Mock(return_value={})
        self.context.validate_mounts = mock.Mock()
        with (
            mock.patch.object(OPERATOR, "verify_approval"),
            mock.patch.object(
                OPERATOR,
                "container",
                return_value={"State": {"Running": True}},
            ),
        ):
            result = OPERATOR.execute(
                self.context,
                {},
                operation,
                permit=lambda: {"valid_until": OPERATOR.time.time() - 1},
            )
        self.assertEqual(result["status"], "unknown")
        self.context.commands.assert_not_called()

    def test_public_configs_readable_and_secret_snapshots_remain_private(self):
        fd = OPERATOR.open_directory(self.context.manifest.parent)
        try:
            OPERATOR.write_exclusive(
                fd, "public.data", b"synthetic public", mode=0o644
            )
            OPERATOR.write_exclusive(
                fd, "secret.data", b"synthetic secret", mode=0o600
            )
            self.assertEqual(
                OPERATOR.frozen_bytes(fd, "public.data", 0o644),
                b"synthetic public",
            )
            self.assertEqual(
                OPERATOR.frozen_bytes(fd, "secret.data", 0o600),
                b"synthetic secret",
            )
            public_mode = (
                (self.context.manifest.parent / "public.data").stat().st_mode
            )
            secret_mode = (
                (self.context.manifest.parent / "secret.data").stat().st_mode
            )
            self.assertTrue(public_mode & 0o004)
            self.assertFalse(secret_mode & 0o077)
            self.assertFalse(
                self.context.manifest.parent.stat().st_mode & 0o077
            )
        finally:
            os.close(fd)

    def test_freeze_shared_config_once_and_pin_all_selected_service_images(
        self,
    ):
        self.context.groups = ("monitoring",)
        snapshot = {
            "version": 1,
            "images": {"synthetic-image:local": "sha256:" + "f" * 64},
        }
        services = {
            name: {
                "image": "synthetic-image:local",
                "build": {"context": "/synthetic"},
                "pull_policy": "build",
            }
            for name in GROUPS["monitoring"].starts
        }
        for name in ("prometheus", "grafana"):
            services[name]["configs"] = [{"source": "prometheus-config"}]
        services["vector"]["configs"] = [
            {
                "source": "openobserve-vector-token",
            }
        ]
        self.context.rendered = {
            "name": "synthetic-poc",
            "services": services,
            "configs": {
                "prometheus-config": {
                    "file": str(OPERATOR.PUBLIC_CONFIGS["prometheus-config"])
                },
                "openobserve-vector-token": {
                    "file": str(
                        ROOT / "infra/monitoring/vector/"
                        "openobserve-secrets.example.json"
                    )
                },
            },
        }
        self.context.snapshot = mock.Mock(return_value=snapshot)
        self.context.readiness = mock.Mock(return_value={})
        self.context.identity = mock.Mock()
        approval = OPERATOR.approve(self.context)
        source = self.context.manifest.parent / "rendered-source.json"
        self.assertEqual(source.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads(source.read_bytes()), self.context.rendered
        )
        self.assertEqual(
            len(
                [
                    name
                    for name in approval["frozen_files"]
                    if name.startswith("config-")
                ]
            ),
            2,
        )
        model = json.loads(
            (self.context.manifest.parent / "compose-normal.json").read_bytes()
        )
        for definition in model["services"].values():
            self.assertEqual(definition["image"], "sha256:" + "f" * 64)
            self.assertEqual(definition["pull_policy"], "never")
            self.assertNotIn("build", definition)
        target = Path(model["configs"]["prometheus-config"]["file"])
        self.assertTrue(target.is_relative_to(self.context.manifest.parent))
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        token = Path(model["configs"]["openobserve-vector-token"]["file"])
        self.assertEqual(token.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("openobserve-vector-token", OPERATOR.PUBLIC_CONFIGS)
        self.assertEqual(
            (self.context.manifest.parent / "compose-normal.json")
            .stat()
            .st_mode
            & 0o777,
            0o600,
        )

    def test_low_memory_does_not_prevent_fixed_lab_stop(self):
        operation = {
            "group_id": "sail-lab",
            "service_id": "sail-server",
            "action": "stop",
        }
        self.context.readiness = mock.Mock(
            return_value={
                "sail-lab": "MEMORY_BUDGET",
            }
        )
        self.context.observations = mock.Mock(
            return_value={
                "sail-server": "stopped",
            }
        )
        self.context.validate_mounts = mock.Mock()
        with (
            mock.patch.object(OPERATOR, "verify_approval"),
            mock.patch.object(
                OPERATOR,
                "container",
                return_value={"State": {"Running": True}},
            ),
        ):
            result = OPERATOR.execute(
                self.context,
                {},
                operation,
                permit=lambda: {"valid_until": OPERATOR.time.time() + 300},
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.context.commands.call_count, 1)
        self.assertIn("stop", self.context.commands.call_args.args[0])

    def volume_fixture(self):
        """Supply an ordinary owned Compose volume model, never a daemon."""
        self.context.groups = ("automation",)
        self.context.load = mock.Mock(return_value=self.context.values)
        rendered = {
            "name": "synthetic-poc",
            "services": {},
            "volumes": {},
        }
        mounts = {}
        volumes = {}
        for service, target in (
            ("n8n-db", "/var/lib/postgresql"),
            ("n8n", "/home/node/.n8n"),
        ):
            name = "synthetic-poc_" + service
            rendered["services"][service] = {
                "image": "synthetic-image:local",
                "volumes": [
                    {"type": "volume", "source": service, "target": target}
                ],
            }
            rendered["volumes"][service] = {"name": name}
            mounts[service] = [
                {
                    "Type": "volume",
                    "Name": name,
                    "Destination": target,
                    "RW": True,
                }
            ]
            volumes[name] = {
                "Name": name,
                "Labels": {
                    "com.docker.compose.project": "synthetic-poc",
                    "com.docker.compose.volume": service,
                },
            }

        def commands(arguments, **_):
            if arguments[:2] == ("image", "inspect"):
                return OPERATOR.canonical([{"Id": "sha256:" + "e" * 64}])
            if arguments[:2] == ("volume", "inspect"):
                return OPERATOR.canonical([volumes[arguments[2]]])
            self.fail("Unexpected mocked command")

        self.context.commands = mock.Mock(side_effect=commands)
        self.context.configuration = mock.Mock(return_value=rendered)
        self.context.rendered = rendered
        return rendered, mounts, volumes

    def test_snapshot_requires_owned_volume_names_and_labels(self):
        rendered, mounts, volumes = self.volume_fixture()
        with mock.patch.object(
            OPERATOR,
            "container",
            side_effect=lambda _, __, service: {"Mounts": mounts[service]},
        ):
            self.assertIn(
                "volume:synthetic-poc_n8n-db",
                self.context.snapshot()["bindings"],
            )
            for labels in (
                None,
                {},
                {
                    "com.docker.compose.project": "other-poc",
                    "com.docker.compose.volume": "n8n-db",
                },
                {
                    "com.docker.compose.project": "synthetic-poc",
                    "com.docker.compose.volume": "unrelated",
                },
            ):
                with self.subTest(labels=labels):
                    volumes["synthetic-poc_n8n-db"]["Labels"] = labels
                    with self.assertRaises(OPERATOR.OperatorError):
                        self.context.snapshot()
            volumes["synthetic-poc_n8n-db"]["Labels"] = {
                "com.docker.compose.project": "synthetic-poc",
                "com.docker.compose.volume": "n8n-db",
            }
            rendered["volumes"]["n8n-db"]["external"] = True
            with self.assertRaises(OPERATOR.OperatorError):
                self.context.snapshot()
            rendered["volumes"]["n8n-db"].pop("external")
            rendered["volumes"]["n8n-db"]["name"] = "other-poc_n8n-db"
            with self.assertRaises(OPERATOR.OperatorError):
                self.context.snapshot()

    def source_model_fixture(self):
        """Persist a synthetic protected model after one explicit rendering."""
        rendered, mounts, volumes = self.volume_fixture()
        overlay = self.root / "private/overlay.yaml"
        private_file(overlay, b"services: {}\n")
        self.context.overlays = (overlay,)
        with mock.patch.object(
            OPERATOR,
            "container",
            side_effect=lambda _, __, service: {"Mounts": mounts[service]},
        ):
            snapshot = self.context.snapshot()
        content = OPERATOR.canonical(rendered)
        private_file(
            self.context.manifest.parent / "rendered-source.json", content
        )
        approval = {
            **snapshot,
            "frozen_files": {
                "rendered-source.json": {
                    "sha256": OPERATOR.hashlib.sha256(content).hexdigest(),
                    "mode": 0o600,
                },
            },
        }
        self.context.configuration.reset_mock()
        self.context.commands.reset_mock()
        return rendered, mounts, volumes, approval

    def test_revalidation_never_renders_original_mutable_inputs(self):
        _, mounts, _, approval = self.source_model_fixture()
        with mock.patch.object(
            OPERATOR,
            "container",
            side_effect=lambda _, __, service: {"Mounts": mounts[service]},
        ):
            self.context.snapshot(approval=approval)
            self.context.configuration.assert_not_called()
            self.assertFalse(
                any(
                    call.args[0][0] == "compose"
                    for call in self.context.commands.call_args_list
                )
            )
            self.context.values["COMPOSE_PROFILES"] = "demo,automation"
            self.context.snapshot(approval=approval)
            self.context.configuration.assert_not_called()

    def test_changed_env_or_overlay_rejects_before_any_compose_config(self):
        _, mounts, _, approval = self.source_model_fixture()
        with mock.patch.object(
            OPERATOR,
            "container",
            side_effect=lambda _, __, service: {"Mounts": mounts[service]},
        ):
            self.context.values["CONTROL_API_TOKEN"] = "synthetic-changed"
            with self.assertRaises(OPERATOR.OperatorError):
                self.context.snapshot(approval=approval)
            self.context.configuration.assert_not_called()
            self.context.commands.assert_not_called()
            self.context.values["CONTROL_API_TOKEN"] = "synthetic-secret"
            self.context.overlays[0].write_bytes(b"synthetic changed overlay")
            with self.assertRaises(OPERATOR.OperatorError):
                self.context.snapshot(approval=approval)
            self.context.configuration.assert_not_called()
            self.context.commands.assert_not_called()

    def test_protected_source_model_tamper_permissions_and_collision(self):
        _, _, _, approval = self.source_model_fixture()
        target = self.context.manifest.parent / "rendered-source.json"
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        target.write_bytes(b'{"name":"synthetic-poc","services":{}}')
        with self.assertRaises(OPERATOR.OperatorError):
            self.context.snapshot(approval=approval)
        self.context.configuration.assert_not_called()
        self.context.commands.assert_not_called()
        fd = OPERATOR.open_directory(self.context.manifest.parent)
        try:
            with self.assertRaises(OPERATOR.OperatorError):
                OPERATOR.write_exclusive(
                    fd, "rendered-source.json", b"synthetic unrelated model"
                )
            self.assertIn(b"services", target.read_bytes())
        finally:
            os.close(fd)

    def test_snapshot_rejects_foreign_volume_prefix_even_if_rendered_is_owned(
        self,
    ):
        _, mounts, _ = self.volume_fixture()
        with mock.patch.object(
            OPERATOR,
            "container",
            side_effect=lambda _, __, service: {"Mounts": mounts[service]},
        ):
            for prefix in ("", "synthetic-poc"):
                self.context.values["DATA_VOLUME_PREFIX"] = prefix
                self.context.snapshot()
            self.context.values["DATA_VOLUME_PREFIX"] = "other-poc"
            with self.assertRaises(OPERATOR.OperatorError):
                self.context.snapshot()

    def test_existing_mount_type_name_target_and_write_mode_must_match(self):
        _, mounts, _ = self.volume_fixture()
        original = dict(mounts["n8n-db"][0])
        with mock.patch.object(
            OPERATOR,
            "container",
            side_effect=lambda _, __, service: {"Mounts": mounts[service]},
        ):
            self.context.snapshot()
            for field, value in (
                ("Name", "other-poc_n8n-db"),
                ("Type", "bind"),
                ("RW", False),
                ("Destination", "/unrelated"),
            ):
                with self.subTest(field=field):
                    mounts["n8n-db"][0] = {**original, field: value}
                    with self.assertRaises(OPERATOR.OperatorError):
                        self.context.snapshot()
            mounts["n8n-db"] = [
                original,
                {
                    "Type": "volume",
                    "Name": "unknown",
                    "RW": True,
                    "Destination": "/extra",
                },
            ]
            with self.assertRaises(OPERATOR.OperatorError):
                self.context.snapshot()

    def test_mounts_are_rechecked_before_each_step_no_late_adoption(self):
        operation = {
            "group_id": "openobserve-lab",
            "service_id": "openobserve",
            "action": "start",
        }
        self.context.readiness = mock.Mock(return_value={})
        self.context.validate_mounts = mock.Mock(
            side_effect=[
                None,
                OPERATOR.OperatorError("APPROVAL_CHANGED"),
            ]
        )
        with (
            mock.patch.object(OPERATOR, "verify_approval"),
            mock.patch.object(
                OPERATOR,
                "container",
                return_value={"State": {"Running": True}},
            ),
        ):
            result = OPERATOR.execute(
                self.context,
                {},
                operation,
                permit=lambda: {"valid_until": OPERATOR.time.time() + 300},
            )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.context.validate_mounts.call_count, 2)
        self.assertEqual(self.context.commands.call_count, 1)

    def test_bind_mount_source_and_readonly_must_match_approved_model(self):
        rendered, mounts, _ = self.volume_fixture()
        source = str(ROOT / "infra/proxy/empty-ca.pem")
        target = "/etc/n8n/corporate-ca.pem"
        rendered["services"]["n8n"]["volumes"].append(
            {
                "type": "bind",
                "source": source,
                "target": target,
                "read_only": True,
            }
        )
        original = {
            "Type": "bind",
            "Source": source,
            "Destination": target,
            "RW": False,
        }
        mounts["n8n"].append(original)
        with mock.patch.object(
            OPERATOR,
            "container",
            side_effect=lambda _, __, service: {"Mounts": mounts[service]},
        ):
            self.context.snapshot()
            for field, value in (
                ("Source", "/synthetic/unapproved-ca.pem"),
                ("RW", True),
                ("Type", "volume"),
            ):
                mounts["n8n"][-1] = {**original, field: value}
                with self.assertRaises(OPERATOR.OperatorError):
                    self.context.validate_mounts()
            name = (
                "bind-"
                + OPERATOR.hashlib.sha256(source.encode()).hexdigest()
                + ".data"
            )
            mounts["n8n"][-1] = {
                **original,
                "Source": str(self.context.manifest.parent / name),
            }
            with self.assertRaises(OPERATOR.OperatorError):
                self.context.validate_mounts()
            self.context.validate_mounts(
                approval={
                    "frozen_files": {
                        name: {"sha256": "e" * 64, "mode": 0o644}
                    },
                }
            )

    def test_declared_tmpfs_is_not_an_unknown_persistent_volume(self):
        rendered, mounts, _ = self.volume_fixture()
        rendered["services"]["n8n"]["tmpfs"] = ["/tmp:rw,noexec,nosuid"]
        host = {"Tmpfs": {"/tmp": "rw,noexec,nosuid"}}
        with mock.patch.object(
            OPERATOR,
            "container",
            side_effect=lambda _, __, service: {
                "Mounts": mounts[service],
                "HostConfig": host if service == "n8n" else {},
            },
        ):
            self.context.validate_mounts()
            mounts["n8n"].append(
                {
                    "Type": "tmpfs",
                    "Destination": "/tmp",
                    "RW": True,
                }
            )
            self.context.validate_mounts()
            mounts["n8n"][-1]["Destination"] = "/unapproved"
            with self.assertRaises(OPERATOR.OperatorError):
                self.context.validate_mounts()

    def test_previous_public_config_requires_fixed_path_and_bytes(self):
        source = OPERATOR.PUBLIC_CONFIGS["prometheus-config"]
        relative = source.relative_to(ROOT)
        previous = self.root / "previous-checkout" / relative
        previous.parent.mkdir(parents=True)
        previous.write_bytes(source.read_bytes())
        # Sandbox /tmp owner mapping is not evidence of host parent trust.
        # Test the content/leaf contract here and parent trust independently.
        with mock.patch.object(
            OPERATOR, "public_config_parents", return_value=True
        ):
            self.assertTrue(
                OPERATOR.equivalent_public_config(source, str(previous))
            )
            previous.write_bytes(b"synthetic changed config")
            self.assertFalse(
                OPERATOR.equivalent_public_config(source, str(previous))
            )
            previous.unlink()
            previous.symlink_to(source)
            self.assertFalse(
                OPERATOR.equivalent_public_config(source, str(previous))
            )
        self.assertFalse(
            OPERATOR.equivalent_public_config(
                self.root / "workspace",
                str(self.root / "different-workspace"),
            )
        )

    def test_private_ancestor_protects_group_writable_checkout_only(self):
        metadata = SimpleNamespace(st_uid=1000, st_mode=0o40775)
        with mock.patch.object(OPERATOR.os, "getuid", return_value=1000):
            with self.assertRaises(OPERATOR.OperatorError):
                OPERATOR.public_parent_state(metadata, False)
            private = SimpleNamespace(st_uid=1000, st_mode=0o40700)
            secured = OPERATOR.public_parent_state(private, False)
            self.assertTrue(OPERATOR.public_parent_state(metadata, secured))
            metadata.st_mode = 0o40777
            with self.assertRaises(OPERATOR.OperatorError):
                OPERATOR.public_parent_state(metadata, secured)
            metadata.st_uid = 65534
            metadata.st_mode = 0o41777
            with self.assertRaises(OPERATOR.OperatorError):
                OPERATOR.public_parent_state(metadata, False)

    def test_inert_token_placeholder_alias_never_includes_private_tokens(self):
        source = ROOT / (
            "infra/monitoring/vector/openobserve-secrets.example.json"
        )
        previous = self.root / "previous-checkout" / source.relative_to(ROOT)
        previous.parent.mkdir(parents=True)
        previous.write_bytes(source.read_bytes())
        with mock.patch.object(
            OPERATOR, "public_config_parents", return_value=True
        ):
            self.assertTrue(
                OPERATOR.equivalent_public_config(source, str(previous))
            )
            private = self.root / "private/private-ingest-token.json"
            private_file(private, source.read_bytes())
            self.assertFalse(
                OPERATOR.equivalent_public_config(private, str(previous))
            )
            previous.write_bytes(b'{"token":"synthetic-changed"}')
            self.assertFalse(
                OPERATOR.equivalent_public_config(source, str(previous))
            )
            previous.unlink()
            previous.symlink_to(source)
            self.assertFalse(
                OPERATOR.equivalent_public_config(source, str(previous))
            )
        previous.unlink()
        previous.write_bytes(source.read_bytes())
        with mock.patch.object(
            OPERATOR,
            "public_config_parents",
            side_effect=OPERATOR.OperatorError("APPROVAL_CHANGED"),
        ):
            self.assertFalse(
                OPERATOR.equivalent_public_config(source, str(previous))
            )

    def test_observation_timestamp_precedes_entire_scan(self):
        clock = mock.Mock(return_value=1000)

        def scan():
            clock.return_value = 1050
            return {"grafana": "running"}

        self.context.observations = mock.Mock(side_effect=scan)
        result = OPERATOR.runner_observations(self.context, clock=clock)
        self.assertEqual(
            result,
            {"observed_at": 1000, "observations": {"grafana": "running"}},
        )

    def test_observation_time_rejects_nonfinite_values(self):
        from lifecycle_api import RunnerState

        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                RunnerState.model_validate(
                    {
                        "project": "synthetic-poc",
                        "runner_id": "a" * 32,
                        "manifest_digest": "b" * 64,
                        "config_digest": "c" * 64,
                        "groups": [],
                        "observed_at": value,
                    }
                )


if __name__ == "__main__":
    unittest.main()
