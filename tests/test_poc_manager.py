"""Synthetic manager API/journal/runner tests. No Docker, SSH or database."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from infra.demo.portal_auth import AuthProblem
from infra.management.api import Settings, create_app
from infra.management.journal import Journal, StateError
from infra.management.runner import (
    Docker,
    Runner,
    RunnerError,
    digest,
    enroll,
)

OWNER, WORKER = "1" * 64, "2" * 64


def root_namespace_fixture(test):
    """Model target / and /tmp ownership under the sandbox UID mapping.

    The test's own files and all no-follow/mode/inode checks remain real.
    This is synthetic root-namespace metadata, not host permission acceptance.
    """
    actual = os.fstat
    identities = {
        (p.stat().st_dev, p.stat().st_ino) for p in (Path("/"), Path("/tmp"))
    }

    def selected(fd):
        info = actual(fd)
        if (info.st_dev, info.st_ino) in identities:
            fields = {
                name: getattr(info, name)
                for name in dir(info)
                if name.startswith("st_")
            }
            fields["st_uid"] = 0
            return SimpleNamespace(**fields)
        return info

    fixture = patch("infra.management.journal.os.fstat", side_effect=selected)
    fixture.start()
    test.addCleanup(fixture.stop)


def spec():
    return {
        "schema_version": 1,
        "independent_storage_verified": True,
        "runner_id": "a" * 32,
        "pocs": {
            key: {
                "project": key + "-example",
                "services": {"catalog-db": [], "demo-api": ["catalog-db"]},
                "completed_bootstraps": [],
                "allow_interruption": True,
                "maintenance": {
                    "token_file": "/synthetic/" + key + ".token",
                    "reservation_id": ("b" if key == "foundation" else "c")
                    * 64,
                    "network": key + "-example_default",
                },
            }
            for key in ("foundation", "vision")
        },
    }


class SyntheticDocker(Docker):
    def __init__(self):
        self.raw, self.events, self.failure = {}, [], None
        for index, (poc, service) in enumerate(
            (p, s)
            for p in ("foundation", "vision")
            for s in ("catalog-db", "demo-api")
        ):
            identifier = format(index + 1, "064x")
            project = poc + "-example"
            self.raw[identifier] = {
                "id": identifier,
                "image": "sha256:" + str(index + 1) * 64,
                "project": project,
                "service": service,
                "owner": "lakehouse-foundation",
                "oneoff": "False",
                "state": "running",
                "exit_code": 0,
                "health": "healthy",
                "mounts": [
                    {
                        "Type": "volume",
                        "Name": project + "-" + service,
                        "Source": "/synthetic/" + project + "/" + service,
                        "Destination": "/data",
                        "RW": True,
                        "Propagation": "",
                        "Mode": "rw",
                    }
                ],
                "runtime": {"memory": 512 * 1024**2, "privileged": False},
                "networks": {
                    project + "_default": {
                        "IPAddress": "172.20.0." + str(index + 2)
                    }
                },
            }

    def ids(self, project):
        return sorted(
            key for key, row in self.raw.items() if row["project"] == project
        )

    def inspect(self, identifier):
        return copy.deepcopy(self.raw[identifier])

    def transition(self, identifier, action):
        self.events.append((identifier, action))
        if self.failure:
            raise RunnerError(self.failure)
        self.raw[identifier]["state"] = (
            "running" if action == "start" else "exited"
        )
        self.raw[identifier]["health"] = (
            "healthy" if action == "start" else None
        )


class Maintenance:
    def __init__(self):
        self.events, self.reservations = [], set()
        self.initialized = {"foundation", "vision"}

    def idle(self, inventory, raw):
        return all(row["state"] == "stopped" for row in inventory)

    def check_config(self, poc_id):
        pass

    def call(self, poc_id, raw, action, **kwargs):
        self.events.append((poc_id, action, kwargs))
        if action == "reserve":
            self.initialized.add(poc_id)
            self.reservations.add(poc_id)
            return {
                "reserved": True,
                "scope": "demo_jobs_and_worker_only",
                "drained": False,
                "interruption_possible": True,
            }
        if action == "release":
            self.reservations.discard(poc_id)
            return {"released": True}
        if action == "initialize":
            self.initialized.add(poc_id)
            return {"initialized": True, "scope": "state_only_no_drain"}
        return {
            "reserved": poc_id in self.reservations,
            "initialized": poc_id in self.initialized,
            "scope": "demo_jobs_and_worker_only",
        }


class LocalClient:
    """Real unprivileged journal methods behind a network-free client."""

    def __init__(self, journal):
        self.journal = journal

    def call(self, action, value):
        with self.journal.transaction() as data:
            if action == "report":
                self.journal.report(data, value)
                return {"accepted": True}
            if action == "claim":
                return {
                    "operation": self.journal.claim(
                        data, value["runner_id"], value["manifest_digest"]
                    )
                }
            if action == "heartbeat":
                self.journal.heartbeat(
                    data,
                    value["runner_id"],
                    value["operation_id"],
                    value["lease_id"],
                )
                return {"accepted": True}
            if action == "result":
                return {
                    "operation": self.journal.finish(
                        data, value["runner_id"], value
                    )
                }
            if action == "reconcile":
                return {
                    "operation": self.journal.reconcile(
                        data,
                        value["runner_id"],
                        value["operation_id"],
                        value["lease_id"],
                    )
                }
        raise AssertionError(action)


class Context:
    def __init__(self, path):
        self.path, self.now = Path(path), 1000.0
        (self.path / "api").mkdir(mode=0o700)
        (self.path / "root-intents").mkdir(mode=0o700)
        self.journal = Journal(self.path / "api", clock=lambda: self.now)
        self.docker = SyntheticDocker()
        self.manifest = enroll(spec(), self.docker)
        self.maintenance = Maintenance()
        self.runner = Runner(
            self.manifest,
            LocalClient(self.journal),
            self.docker,
            self.path / "root-intents",
            self.maintenance,
            memory=lambda: True,
            clock=lambda: self.now,
            sleep=lambda _: None,
        )

    def queue(self, action="stop", ack=True, key="synthetic-key-123"):
        self.runner.report()
        with self.journal.transaction() as data:
            saved = self.journal.create_plan(
                data, "foundation", action, "owner"
            )
        with self.journal.transaction() as data:
            return self.journal.submit(
                data,
                saved["plan_id"],
                saved["confirmation"],
                key,
                "owner",
                ack,
            )


class JournalRunner(unittest.TestCase):
    def setUp(self):
        root_namespace_fixture(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.context = Context(self.temporary.name)

    def test_restart_order_and_private_intents(self):
        c = self.context
        c.queue("restart")
        result = c.runner.run_once()
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(
            [a for _, a in c.docker.events], ["stop", "stop", "start", "start"]
        )
        self.assertEqual(
            c.docker.raw[c.docker.events[0][0]]["service"], "demo-api"
        )
        self.assertFalse(c.maintenance.reservations)
        for path in (c.path / "root-intents").iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_stop_retains_reservation_and_volumes(self):
        c = self.context
        before = {
            k: copy.deepcopy(v["mounts"]) for k, v in c.docker.raw.items()
        }
        c.queue()
        self.assertEqual(c.runner.run_once()["state"], "succeeded")
        self.assertIn("foundation", c.maintenance.reservations)
        self.assertEqual(
            {k: v["mounts"] for k, v in c.docker.raw.items()}, before
        )

    def test_unacknowledged_interruption_cannot_submit(self):
        with self.assertRaisesRegex(StateError, "INTERRUPTION_ACK_REQUIRED"):
            self.context.queue(ack=False)
        self.assertEqual(self.context.docker.events, [])

    def test_idempotency_is_receipt_lookup_not_reexecution(self):
        c = self.context
        c.runner.report()
        with c.journal.transaction() as data:
            saved = c.journal.create_plan(data, "foundation", "stop", "owner")
            first = c.journal.submit(
                data,
                saved["plan_id"],
                saved["confirmation"],
                "same-key-123",
                "owner",
                True,
            )
            second = c.journal.submit(
                data,
                saved["plan_id"],
                saved["confirmation"],
                "same-key-123",
                "owner",
                True,
            )
        self.assertEqual(first, second)

    def test_unknown_lease_never_replays(self):
        c = self.context
        c.queue()
        with c.journal.transaction() as data:
            claimed = c.journal.claim(
                data, c.manifest["runner_id"], digest(c.manifest)
            )
        c.now += 301
        with c.journal.transaction() as data:
            row = next(iter(data["operations"].values()))
            self.assertEqual(row["status"], "unknown")
            self.assertIsNone(
                c.journal.claim(
                    data, c.manifest["runner_id"], digest(c.manifest)
                )
            )
        self.assertIsNotNone(claimed)
        self.assertEqual(c.docker.events, [])

    def test_root_rejects_changed_snapshot(self):
        c = self.context
        c.queue()
        next(iter(c.docker.raw.values()))["state"] = "exited"
        self.assertEqual(c.runner.run_once()["state"], "failed")
        self.assertEqual(c.docker.events, [])

    def test_denied_mutation_not_retried_and_remains_unknown(self):
        c = self.context
        c.queue()
        c.docker.failure = "DOCKER_PERMISSION_DENIED"
        self.assertEqual(c.runner.run_once()["state"], "unknown")
        self.assertEqual(len(c.docker.events), 1)
        with c.journal.transaction() as data:
            self.assertEqual(
                next(iter(data["operations"].values()))["status"], "unknown"
            )

    def test_root_unknown_barrier_survives_web_journal_reset(self):
        c = self.context
        c.queue()
        c.docker.failure = "DOCKER_PERMISSION_DENIED"
        c.runner.run_once()
        with c.journal.transaction() as data:
            data["operations"], data["keys"] = {}, {}
        c.queue(key="new-key-1234")
        with self.assertRaisesRegex(RunnerError, "OPERATION_UNKNOWN"):
            c.runner.run_once()
        self.assertEqual(len(c.docker.events), 1)

    def test_root_global_lock_does_not_trust_api_serialization(self):
        c = self.context
        c.queue()
        directory, lock = c.runner.authority()
        try:
            with self.assertRaisesRegex(RunnerError, "OPERATION_UNKNOWN"):
                c.runner.run_once()
        finally:
            os.close(lock)
            os.close(directory)
        self.assertEqual(c.docker.events, [])

    def test_explicit_root_reconcile_observes_partial_state_without_retry(
        self,
    ):
        c = self.context
        row = c.queue()
        c.docker.failure = "DOCKER_PERMISSION_DENIED"
        c.runner.run_once()
        before = list(c.docker.events)
        result = c.runner.reconcile(row["id"])
        self.assertEqual(result["state"], "operator_reconciled")
        self.assertEqual(c.docker.events, before)
        self.assertIn("foundation", c.maintenance.reservations)

    def test_mutated_identity_rejected_before_any_stop(self):
        c = self.context
        next(iter(c.docker.raw.values()))["runtime"]["memory"] = 1
        with self.assertRaises(Exception):
            c.runner.report()
        self.assertEqual(c.docker.events, [])

    def test_unknown_container_is_not_adopted(self):
        c = self.context
        row = copy.deepcopy(next(iter(c.docker.raw.values())))
        row["id"] = "f" * 64
        c.docker.raw[row["id"]] = row
        with self.assertRaises(RunnerError):
            c.runner.report()

    def test_low_memory_still_permits_stop(self):
        c = self.context
        c.runner.memory = lambda: False
        c.queue()
        self.assertEqual(c.runner.run_once()["state"], "succeeded")

    def test_writable_bind_ancestor_overlap_rejected_at_enrollment(self):
        c = self.context
        rows = list(c.docker.raw.values())
        rows[0]["mounts"][0]["Source"] = "/synthetic/shared"
        rows[2]["mounts"][0]["Type"] = "bind"
        rows[2]["mounts"][0]["Source"] = "/synthetic/shared/child"
        with self.assertRaisesRegex(RunnerError, "WRITABLE_STORES_OVERLAP"):
            enroll(spec(), c.docker)

    def test_projection_does_not_collect_env_command_or_full_labels(self):
        from infra.management.runner import PROJECTION

        self.assertNotIn(".Config.Env", PROJECTION)
        self.assertNotIn(".Config.Cmd", PROJECTION)
        self.assertNotIn("json .Config.Labels", PROJECTION)

    def test_lost_result_and_response_keep_barrier(self):
        for server_accepted in (False, True):
            with (
                self.subTest(server_accepted=server_accepted),
                tempfile.TemporaryDirectory() as temporary,
            ):
                c = Context(temporary)
                operation = c.queue()
                original = c.runner.client.call
                calls = []

                def lose_result(action, payload):
                    if action == "result":
                        calls.append(action)
                        if server_accepted:
                            original(action, payload)
                        raise RunnerError("STATE_UNAVAILABLE")
                    return original(action, payload)

                c.runner.client.call = lose_result
                result = c.runner.run_once()
                self.assertEqual(result["state"], "unknown")
                self.assertEqual(result["local_outcome"], "succeeded")
                self.assertEqual(calls, ["result"])
                authority = json.loads(
                    (c.path / "root-intents/authority.json").read_text()
                )
                self.assertIsNotNone(authority["active"])
                before = list(c.docker.events)
                c.runner.client.call = original
                c.now += 301
                reconciled = c.runner.reconcile(operation["id"])
                self.assertEqual(reconciled["local_outcome"], "succeeded")
                self.assertEqual(c.docker.events, before)
                self.assertIsNone(
                    json.loads(
                        (c.path / "root-intents/authority.json").read_text()
                    )["active"]
                )

    def test_final_snapshot_rejects_restarted_earlier_member(self):
        c = self.context
        c.queue()
        transition = c.docker.transition

        def restarted(identifier, action):
            transition(identifier, action)
            if len(c.docker.events) == 2:
                first = c.docker.events[0][0]
                c.docker.raw[first].update(state="running", health="healthy")

        c.docker.transition = restarted
        result = c.runner.run_once()
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["error_code"], "HEALTH_TIMEOUT")

    def test_noop_start_rejects_unhealthy_running_stack(self):
        c = self.context
        next(iter(c.docker.raw.values()))["health"] = "unhealthy"
        c.queue("start", ack=False)
        self.assertEqual(c.runner.run_once()["state"], "failed")
        self.assertEqual(c.docker.events, [])

    def test_storage_attestation_precedes_inventory(self):
        selected = spec()
        selected["independent_storage_verified"] = False
        docker = Mock()
        with self.assertRaisesRegex(
            RunnerError, "STORAGE_SEPARATION_APPROVAL_REQUIRED"
        ):
            enroll(selected, docker)
        docker.ids.assert_not_called()

    def test_double_leading_slash_bind_alias_rejected(self):
        c = self.context
        rows = list(c.docker.raw.values())
        rows[0]["mounts"][0]["Source"] = "/synthetic/shared"
        rows[2]["mounts"][0]["Source"] = "//synthetic/shared"
        with self.assertRaises(RunnerError):
            enroll(spec(), c.docker)

    def test_stopped_start_initializes_real_asgi_maintenance(self):
        import asyncio
        import sys
        import threading

        from fastapi import FastAPI

        from infra.management.runner import Maintenance as NativeMaintenance

        sys.path.insert(
            0, str(Path(__file__).absolute().parents[1] / "infra/demo")
        )
        from whole_maintenance import install_whole_maintenance

        c = self.context
        enabled = c.path / ".whole-maintenance"
        enabled.mkdir(mode=0o700)
        credentials = c.path / "credentials"
        credentials.mkdir(mode=0o700)
        for path in (
            enabled / "runner.token",
            credentials / "maintenance.token",
        ):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as target:
                target.write("d" * 64)
        backend = FastAPI()
        install_whole_maintenance(
            backend,
            SimpleNamespace(path=c.path / "unused.env"),
            SimpleNamespace(operation=threading.Lock()),
            worker_idle=lambda: True,
        )

        @backend.post("/synthetic-write")
        async def write():
            return {"ok": True}

        requests = []

        def forward(request):
            requests.append(request.url.path)

            async def invoke():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=backend)
                ) as client:
                    response = await client.request(
                        request.method,
                        str(request.url),
                        headers=request.headers,
                        content=request.content,
                    )
                    return httpx.Response(
                        response.status_code, content=response.content
                    )

            return asyncio.run(invoke())

        c.manifest["maintenance"]["foundation"]["token_file"] = str(
            credentials / "maintenance.token"
        )
        for row in c.docker.raw.values():
            if row["project"] == "foundation-example":
                row.update(state="exited", health=None)
        c.runner = Runner(
            c.manifest,
            c.runner.client,
            c.docker,
            c.path / "root-intents",
            NativeMaintenance(
                c.manifest, transport=httpx.MockTransport(forward)
            ),
            memory=lambda: True,
            clock=lambda: c.now,
        )
        c.queue("start", ack=False)
        result = c.runner.run_once()
        self.assertEqual(result["state"], "succeeded", result)
        self.assertIn("/whole-maintenance/initialize", requests)
        self.assertTrue((enabled / "journal.json").exists())
        response = forward(
            httpx.Request("POST", "http://synthetic/synthetic-write", json={})
        )
        self.assertEqual(response.status_code, 200)

    def test_release_requires_exact_backend_receipt(self):
        from infra.management import runner as subject
        from infra.management.runner import Maintenance as NativeMaintenance

        c = self.context
        _, raw = c.docker.snapshot(c.manifest)
        target = NativeMaintenance(
            c.manifest,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={})
            ),
        )
        with (
            patch.object(subject, "private_directory", return_value=123),
            patch.object(subject, "private_read", return_value=b"d" * 64),
            patch.object(subject.os, "close"),
            self.assertRaisesRegex(RunnerError, "WORKLOAD_BUSY"),
        ):
            target.call("foundation", raw["foundation"], "release")


class CLI(unittest.TestCase):
    def setUp(self):
        root_namespace_fixture(self)

    def test_default_cli_has_no_private_or_network_or_docker_io(self):
        import io
        from contextlib import redirect_stdout

        from scripts import poc_manager

        output = io.StringIO()
        with (
            patch.object(
                poc_manager, "token_file", side_effect=AssertionError
            ),
            patch.object(poc_manager, "Docker", side_effect=AssertionError),
            patch.object(poc_manager, "api_call", side_effect=AssertionError),
            redirect_stdout(output),
        ):
            self.assertEqual(poc_manager.main([]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "plan_only")

    def test_new_tokens_are_private_distinct_and_never_printed(self):
        import io
        from contextlib import redirect_stdout

        from scripts import poc_manager

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "new-private"
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    poc_manager.main(["init", str(destination)]), 0
                )
            values = [
                (destination / name).read_text().strip()
                for name in ("api.token", "runner.token")
            ]
            self.assertNotEqual(*values)
            self.assertTrue(
                all(
                    len(value) == 64 and value not in output.getvalue()
                    for value in values
                )
            )
            self.assertTrue(
                all(
                    path.stat().st_mode & 0o777 == 0o600
                    for path in destination.iterdir()
                )
            )

    def test_serve_stops_on_unknown_without_retry(self):
        import io
        from contextlib import redirect_stdout

        from scripts import poc_manager

        actor = Mock()
        actor.run_once.side_effect = [{"state": "idle"}, {"state": "unknown"}]
        with (
            patch.object(poc_manager.os, "getuid", return_value=0),
            patch.object(poc_manager.os, "geteuid", return_value=0),
            patch.object(poc_manager, "private_json", return_value={}),
            patch.object(poc_manager, "manifest_value", return_value={}),
            patch.object(poc_manager, "token_file", return_value=WORKER),
            patch.object(poc_manager, "Client"),
            patch.object(poc_manager, "Docker"),
            patch.object(poc_manager, "Maintenance"),
            patch.object(poc_manager, "Runner", return_value=actor),
            patch.object(poc_manager.time, "sleep") as sleep,
            redirect_stdout(io.StringIO()),
        ):
            code = poc_manager.main(
                [
                    "--token-file",
                    "/synthetic/token",
                    "serve",
                    "--manifest",
                    "/synthetic/manifest",
                    "--intent-dir",
                    "/synthetic/intents",
                ]
            )
        self.assertEqual(code, 1)
        self.assertEqual(actor.run_once.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_checkout_private_state_is_rejected_before_any_open(self):
        from infra.management import journal as subject

        with (
            patch.object(subject.os, "open", side_effect=AssertionError),
            self.assertRaisesRegex(
                StateError, "PRIVATE_STATE_INSIDE_CHECKOUT"
            ),
        ):
            subject.private_directory(
                subject.CHECKOUT_ROOT / "private-test", create=True
            )


class ManagerAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root_namespace_fixture(self)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.context = Context(self.temporary.name)
        self.context.runner.report()
        self.settings = Settings(
            Path(self.temporary.name) / "api",
            Path("/unused/token"),
            Path("/unused/runner"),
        )
        self.app = create_app(
            self.settings,
            journal=self.context.journal,
            tokens=(OWNER, WORKER),
            clock=lambda: self.context.now,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url=self.settings.public_origin,
        )
        self.addAsyncCleanup(self.client.aclose)
        self.owner = {"Authorization": "Bearer " + OWNER}
        self.worker = {"Authorization": "Bearer " + WORKER}

    async def test_unauthenticated_cannot_read_or_mutate(self):
        self.assertEqual((await self.client.get("/api/pocs")).status_code, 401)
        self.assertEqual(
            (
                await self.client.post(
                    "/api/pocs/foundation/plans", json={"action": "stop"}
                )
            ).status_code,
            403,
        )

    async def test_role_boundary_runner_and_browser_separate(self):
        self.assertEqual(
            (
                await self.client.get("/api/pocs", headers=self.worker)
            ).status_code,
            401,
        )
        self.assertEqual(
            (
                await self.client.post(
                    "/internal/runner/claim",
                    headers=self.owner,
                    json={"runner_id": "a" * 32, "manifest_digest": "b" * 64},
                )
            ).status_code,
            401,
        )

    async def test_token_login_cookie_csrf_and_no_secret_echo(self):
        response = await self.client.post(
            "/api/session",
            headers={"Origin": self.settings.public_origin},
            json={"token": OWNER},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(OWNER, response.text)
        self.assertTrue(
            (await self.client.get("/api/session")).json()["authenticated"]
        )
        self.assertEqual(
            (
                await self.client.post(
                    "/api/pocs/foundation/plans", json={"action": "stop"}
                )
            ).status_code,
            403,
        )
        self.assertEqual(
            (
                await self.client.delete(
                    "/api/session",
                    headers={"Origin": self.settings.public_origin},
                )
            ).status_code,
            200,
        )

    async def test_ack_and_readonly_by_key_recovery(self):
        preview = (
            await self.client.post(
                "/api/pocs/foundation/plans",
                headers=self.owner,
                json={"action": "stop"},
            )
        ).json()
        self.assertTrue(preview["requires_interruption_ack"])
        payload = {
            "plan_id": preview["plan_id"],
            "confirmation": preview["confirmation"],
            "idempotency_key": "request-key-123",
        }
        self.assertEqual(
            (
                await self.client.post(
                    "/api/operations", headers=self.owner, json=payload
                )
            ).status_code,
            409,
        )
        response = await self.client.post(
            "/api/operations",
            headers=self.owner,
            json=dict(payload, interruption_ack=True),
        )
        self.assertEqual(response.status_code, 202, response.text)
        recovered = await self.client.get(
            "/api/operations/by-key/request-key-123", headers=self.owner
        )
        self.assertEqual(recovered.json(), response.json())

    async def test_extra_fields_and_duplicate_json_do_not_echo_input(self):
        response = await self.client.post(
            "/api/pocs/foundation/plans",
            headers=self.owner,
            json={"action": "stop", "token": "PRIVATE_SYNTHETIC"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("PRIVATE_SYNTHETIC", response.text)
        response = await self.client.post(
            "/api/pocs/foundation/plans",
            headers=self.owner,
            content='{"action":"stop","action":"start"}',
        )
        self.assertNotEqual(response.status_code, 200)

    async def test_only_fixed_targets_and_no_raw_inspect(self):
        self.assertEqual(
            (
                await self.client.post(
                    "/api/pocs/dokploy/plans",
                    headers=self.owner,
                    json={"action": "stop"},
                )
            ).status_code,
            404,
        )
        response = await self.client.get("/api/pocs", headers=self.owner)
        self.assertNotIn("container_id", response.text)
        self.assertNotIn("mount_identity", response.text)
        self.assertEqual(
            {r["poc_id"] for r in response.json()["pocs"]},
            {"foundation", "vision"},
        )

    async def test_swagger_uses_local_assets_and_requires_auth(self):
        self.assertEqual((await self.client.get("/docs")).status_code, 401)
        response = await self.client.get("/docs", headers=self.owner)
        self.assertIn("/swagger/swagger-ui-bundle.js", response.text)
        self.assertNotIn("cdn.jsdelivr", response.text)

    async def test_sso_binds_session_and_rechecks_every_request(self):
        verifier = Mock()

        async def verify(headers):
            if headers.getlist("cf-access-jwt-assertion") != [
                "synthetic-signed"
            ]:
                raise AuthProblem(401, "access_identity_required")
            return "d" * 64

        verifier.verify = Mock(side_effect=verify)
        app = create_app(
            Settings(
                self.settings.state_dir,
                self.settings.token_file,
                self.settings.runner_token_file,
                auth_mode="cloudflare-access",
            ),
            journal=self.context.journal,
            tokens=(OWNER, WORKER),
            verifier=verifier,
            clock=lambda: self.context.now,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=self.settings.public_origin,
        ) as client:
            headers = {
                "Origin": self.settings.public_origin,
                "Cf-Access-Jwt-Assertion": "synthetic-signed",
            }
            self.assertEqual(
                (
                    await client.post(
                        "/api/session", headers=headers, json={"token": OWNER}
                    )
                ).status_code,
                422,
            )
            self.assertEqual(
                (
                    await client.post(
                        "/api/session",
                        headers=headers,
                        json={"method": "cloudflare-access"},
                    )
                ).status_code,
                200,
            )
            self.assertEqual(
                (await client.get("/api/pocs", headers=headers)).status_code,
                200,
            )
            self.assertEqual((await client.get("/api/pocs")).status_code, 401)
            self.assertGreaterEqual(verifier.verify.call_count, 3)

    async def test_public_sso_and_fixed_loopback_runner_channel(self):
        from infra.management.runner import Client

        selected = Settings(
            self.settings.state_dir,
            self.settings.token_file,
            self.settings.runner_token_file,
            public_origin="https://control.example.invalid",
            auth_mode="cloudflare-access",
        )
        verifier = Mock()
        verifier.verify = Mock(
            side_effect=AssertionError("machine channel must use runner token")
        )
        app = create_app(
            selected,
            journal=self.context.journal,
            tokens=(OWNER, WORKER),
            verifier=verifier,
            clock=lambda: self.context.now,
        )
        captured = []

        def capture(request):
            captured.append(request)
            return httpx.Response(200, json={"operation": None})

        channel = Client(
            "http://127.0.0.1:8010",
            WORKER,
            public_origin=selected.public_origin,
            transport=httpx.MockTransport(capture),
        )
        channel.call(
            "claim",
            {
                "runner_id": self.context.manifest["runner_id"],
                "manifest_digest": digest(self.context.manifest),
            },
        )
        request = captured[0]
        self.assertEqual(request.url.host, "127.0.0.1")
        self.assertEqual(request.headers["host"], "control.example.invalid")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8010",
        ) as client:
            result = await client.post(
                "/internal/runner/claim",
                headers=request.headers,
                content=request.content,
            )
            self.assertEqual(result.status_code, 200, result.text)
            rejected = await client.post(
                "/internal/runner/claim",
                headers=self.worker,
                json={"runner_id": "a" * 32, "manifest_digest": "b" * 64},
            )
            self.assertEqual(rejected.status_code, 400)
        verifier.verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
