"""Private reservation files and ASGI; synthetic locks/worker idle only."""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT / "infra/demo"))
from whole_maintenance import install_whole_maintenance  # noqa: E402

TOKEN, RESERVATION = "a" * 64, "b" * 64


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        private = self.root / ".whole-maintenance"
        private.mkdir(mode=0o700)
        fd = os.open(
            private / "runner.token",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "w") as target:
            target.write(TOKEN)
        self.jobs = SimpleNamespace(operation=threading.Lock())
        self.worker_idle = True
        self.store = SimpleNamespace(path=self.root / "synthetic.env")
        self.app = self.application()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://synthetic",
        )
        self.addAsyncCleanup(self.client.aclose)
        self.headers = {"Authorization": "Bearer " + TOKEN}

    def application(self):
        app = FastAPI()
        install_whole_maintenance(
            app, self.store, self.jobs, worker_idle=lambda: self.worker_idle
        )

        @app.post("/synthetic-write")
        async def write():
            return {"accepted": True}

        return app

    async def reserve(self, **extra):
        return await self.client.post(
            "/whole-maintenance/reserve",
            headers=self.headers,
            json={"reservation_id": RESERVATION, **extra},
        )

    async def test_root_token_required_no_owner_token_fallback(self):
        self.assertEqual(
            (
                await self.client.post(
                    "/whole-maintenance/reserve",
                    json={"reservation_id": RESERVATION},
                )
            ).status_code,
            401,
        )
        self.assertEqual(
            (
                await self.client.post(
                    "/whole-maintenance/reserve",
                    headers={"Authorization": "Bearer owner-token"},
                    json={"reservation_id": RESERVATION},
                )
            ).status_code,
            401,
        )

    async def test_reserve_blocks_write_and_releases_only_its_lock(self):
        result = await self.reserve()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["drained"])
        self.assertTrue(self.jobs.operation.locked())
        self.assertEqual(
            (await self.client.post("/synthetic-write", json={})).status_code,
            409,
        )
        result = await self.client.post(
            "/whole-maintenance/release",
            headers=self.headers,
            json={"reservation_id": RESERVATION},
        )
        self.assertEqual(result.status_code, 200)
        self.assertFalse(self.jobs.operation.locked())
        self.assertEqual(
            (await self.client.post("/synthetic-write", json={})).status_code,
            200,
        )

    async def test_busy_job_requires_ack_and_never_has_its_lock_released(self):
        self.jobs.operation.acquire()
        self.assertEqual((await self.reserve()).status_code, 409)
        result = await self.reserve(allow_interruption=True)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["drained"])
        self.assertTrue(result.json()["interruption_possible"])
        await self.client.post(
            "/whole-maintenance/release",
            headers=self.headers,
            json={"reservation_id": RESERVATION},
        )
        self.assertTrue(self.jobs.operation.locked())
        self.jobs.operation.release()

    async def test_worker_busy_is_not_health_equals_idle(self):
        self.worker_idle = False
        self.assertEqual((await self.reserve()).status_code, 409)
        result = await self.reserve(allow_interruption=True)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()["drained"])

    async def test_restart_preserves_reservation_no_automatic_unlock(self):
        self.assertEqual((await self.reserve()).status_code, 200)
        app2 = self.application()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app2),
            base_url="http://synthetic",
        ) as client:
            self.assertEqual(
                (await client.post("/synthetic-write", json={})).status_code,
                409,
            )
            status = await client.get(
                "/whole-maintenance/status", headers=self.headers
            )
            self.assertTrue(status.json()["reserved"])

    async def test_foreign_reservation_cannot_release_or_replace(self):
        await self.reserve()
        for action in ("reserve", "release"):
            result = await self.client.post(
                "/whole-maintenance/" + action,
                headers=self.headers,
                json={"reservation_id": "c" * 64},
            )
            self.assertEqual(result.status_code, 409)

    async def test_missing_token_is_not_generated_or_bypassed(self):
        (self.root / ".whole-maintenance/runner.token").unlink()
        self.assertEqual((await self.reserve()).status_code, 401)
        self.assertFalse(
            (self.root / ".whole-maintenance/runner.token").exists()
        )

    async def test_feature_off_preserves_existing_write_routes(self):
        (self.root / ".whole-maintenance/runner.token").unlink()
        (self.root / ".whole-maintenance").rmdir()
        response = await self.client.post("/synthetic-write", json={})
        self.assertEqual(response.status_code, 200)
        self.assertFalse((self.root / ".whole-maintenance").exists())

    async def test_enabled_missing_or_corrupt_journal_blocks_writes(self):
        self.assertEqual(
            (await self.client.post("/synthetic-write", json={})).status_code,
            409,
        )
        await self.reserve()
        (self.root / ".whole-maintenance/journal.json").write_text("corrupt")
        self.assertEqual(
            (await self.client.post("/synthetic-write", json={})).status_code,
            409,
        )

    async def test_explicit_initialization_is_not_false_idle(self):
        path = self.root / ".whole-maintenance/journal.json"
        status = await self.client.get(
            "/whole-maintenance/status", headers=self.headers
        )
        self.assertFalse(status.json()["initialized"])
        self.assertFalse(path.exists())
        self.assertEqual(
            (await self.client.post("/synthetic-write", json={})).status_code,
            409,
        )
        response = await self.client.post(
            "/whole-maintenance/initialize",
            headers=self.headers,
            json={"reservation_id": RESERVATION},
        )
        self.assertEqual(
            response.json(),
            {"initialized": True, "scope": "state_only_no_drain"},
        )
        self.assertTrue(path.exists())
        self.assertEqual(
            (await self.client.post("/synthetic-write", json={})).status_code,
            200,
        )


if __name__ == "__main__":
    unittest.main()
