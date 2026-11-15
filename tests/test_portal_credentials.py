"""Exercise private human-key rotation with synthetic files and ASGI only."""

import contextlib
import importlib.util
import io
import json
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
with mock.patch.object(sys, "path", [str(ROOT / "infra/demo"), *sys.path]):
    from portal_credentials import CredentialError, CredentialStore

BOOTSTRAP = "b" * 48
NEW = "n" * 48


class CredentialStoreTests(unittest.TestCase):
    """No daemon, private operator settings or source database is contacted."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="portal-key-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.store = CredentialStore(self.root, BOOTSTRAP)

    def test_read_only_bootstrap_and_constant_time_proof_contract(self):
        self.assertFalse(self.store.status()["configured"])
        self.assertTrue(self.store.verify(BOOTSTRAP)["valid"])
        self.assertFalse(self.store.verify("incorrect")["valid"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_rotation_persists_hash_and_revokes_bootstrap_after_restart(self):
        before = self.store.status()["revision"]
        result = self.store.change(BOOTSTRAP, NEW)
        self.assertTrue(result["changed"])
        self.assertNotEqual(before, result["revision"])
        directory = self.root / ".portal-auth"
        target = directory / "credential.json"
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(NEW, target.read_text())
        self.assertNotIn(BOOTSTRAP, target.read_text())
        restarted = CredentialStore(self.root, BOOTSTRAP)
        self.assertFalse(restarted.verify(BOOTSTRAP)["valid"])
        self.assertTrue(restarted.verify(NEW)["valid"])
        self.assertEqual(restarted.status()["revision"], result["revision"])

    def test_wrong_proof_or_bad_new_key_makes_no_files(self):
        for current, new in (
            ("wrong", NEW),
            (BOOTSTRAP, "short"),
            (BOOTSTRAP, "x" * 129),
            (BOOTSTRAP, "日本語" * 24),
            (BOOTSTRAP, " " * 48),
            (BOOTSTRAP, BOOTSTRAP),
        ):
            with (
                self.subTest(case=len(new)),
                self.assertRaises(CredentialError),
            ):
                self.store.change(current, new)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_symlink_unknown_incomplete_and_corrupt_state_fail_closed(self):
        foreign = self.root / "foreign"
        foreign.mkdir(mode=0o700)
        (self.root / ".portal-auth").symlink_to(foreign)
        with self.assertRaises(OSError):
            self.store.status()
        (self.root / ".portal-auth").unlink()
        (self.root / ".portal-auth").mkdir(mode=0o700)
        with self.assertRaises(FileNotFoundError):
            self.store.verify(BOOTSTRAP)
        target = self.root / ".portal-auth/credential.json"
        target.write_text('{"unexpected":"synthetic"}')
        target.chmod(0o600)
        with self.assertRaises(CredentialError):
            self.store.verify(BOOTSTRAP)
        self.assertIn("synthetic", target.read_text())

    def test_rate_limit_before_scrypt_and_status_not_limited(self):
        for _ in range(10):
            self.assertFalse(self.store.verify("wrong")["valid"])
        with self.assertRaisesRegex(
            CredentialError, "credential_rate_limited"
        ):
            self.store.verify(BOOTSTRAP)
        self.assertFalse(self.store.status()["configured"])

    def test_failed_replace_preserves_previous_hash(self):
        self.store.change(BOOTSTRAP, NEW)
        previous = (self.root / ".portal-auth/credential.json").read_bytes()
        with mock.patch("portal_credentials.os.replace", side_effect=OSError):
            with self.assertRaises(OSError):
                self.store.change(NEW, "z" * 48)
        self.assertEqual(
            (self.root / ".portal-auth/credential.json").read_bytes(), previous
        )
        self.assertTrue(self.store.verify(NEW)["valid"])
        self.assertFalse(
            any((self.root / ".portal-auth").glob(".credential-*"))
        )

    def test_duplicate_record_and_lock_contention_fail_closed(self):
        self.store.change(BOOTSTRAP, NEW)
        target = self.root / ".portal-auth/credential.json"
        original = target.read_text()
        record = json.loads(original)
        target.write_text('{"version":1,' + original.lstrip("{"))
        with self.assertRaises(CredentialError):
            self.store.status()
        target.write_text(original)
        with mock.patch(
            "portal_credentials.fcntl.flock", side_effect=BlockingIOError
        ):
            with self.assertRaises(BlockingIOError):
                self.store.change(NEW, "z" * 48)
        self.assertEqual(json.loads(target.read_text()), record)


class SetupAndCredentialApiTests(unittest.IsolatedAsyncioTestCase):
    """Use the real API router and generated synthetic private settings."""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="portal-setup-api-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.site = self.directory / "private/site.env"
        with mock.patch.object(
            sys,
            "path",
            [str(ROOT / "scripts"), str(ROOT / "infra/demo"), *sys.path],
        ):
            import settings

            with (
                mock.patch.dict(sys.modules, {"settings": settings}),
                mock.patch.object(
                    sys, "argv", ["configure.py", "--output", str(self.site)]
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                runpy.run_path(
                    str(ROOT / "scripts/configure.py"), run_name="__main__"
                )
            specification = importlib.util.spec_from_file_location(
                "portal_api_test_subject", ROOT / "infra/demo/api.py"
            )
            module = importlib.util.module_from_spec(specification)
            specification.loader.exec_module(module)
        self.app = module.create_app(settings_file=self.site, token=BOOTSTRAP)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
            headers={"Authorization": "Bearer " + BOOTSTRAP},
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_setup_is_authenticated_and_never_saves_settings(self):
        before = self.site.read_bytes()
        denied = await self.client.get("/setup", headers={"Authorization": ""})
        self.assertEqual(denied.status_code, 401)
        response = await self.client.get("/setup")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertFalse(result["apply_supported"])
        self.assertEqual(len(result["components"]), 10)
        self.assertEqual(len({row["id"] for row in result["components"]}), 10)
        for row in result["components"]:
            plan = await self.client.post(
                "/setup/plan", json={"component_id": row["id"]}
            )
            self.assertEqual(plan.status_code, 200)
            self.assertFalse(plan.json()["can_apply"])
            self.assertTrue(plan.json()["requires_operator"])
            self.assertIn(row["profile"], plan.json()["profiles_after"])
        self.assertEqual(before, self.site.read_bytes())
        self.assertNotIn(
            ".portal-auth", {p.name for p in self.site.parent.iterdir()}
        )
        self.assertNotIn(BOOTSTRAP, response.text)

    async def test_setup_rejects_arbitrary_component_paths_and_commands(self):
        for body in (
            {"component_id": "unknown"},
            {"component_id": "../../etc"},
            {"component_id": "compute", "command": "synthetic"},
        ):
            response = await self.client.post("/setup/plan", json=body)
            self.assertIn(response.status_code, {404, 422})

    async def test_login_credential_and_server_key_are_separate(self):
        bootstrap = self.app.state.store.applied["PORTAL_LOGIN_TOKEN"]
        status = await self.client.get("/portal-auth/status")
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["configured"])
        proof = await self.client.post(
            "/portal-auth/verify", json={"token": BOOTSTRAP}
        )
        self.assertFalse(proof.json()["valid"])
        self.assertNotIn(bootstrap, status.text)
        # Server authentication remains unchanged by a login-key request.
        self.assertEqual((await self.client.get("/settings")).status_code, 200)

    async def test_real_bff_api_rotation_revokes_two_sessions(self):
        with mock.patch.object(
            sys, "path", [str(ROOT / "infra/demo"), *sys.path]
        ):
            from portal_gateway import create_app

        gateway = create_app(
            {"CONTROL_API_TOKEN": BOOTSTRAP},
            transport=httpx.ASGITransport(app=self.app),
        )
        headers = {"Origin": "http://localhost:8501", "X-Poc-Request": "1"}
        clients = [
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway),
                base_url="http://localhost:8501",
                headers=headers,
            )
            for _ in range(2)
        ]
        for client in clients:
            self.addAsyncCleanup(client.aclose)
        first, second = clients
        old = self.app.state.store.applied["PORTAL_LOGIN_TOKEN"]
        before = self.site.read_bytes()
        for client in clients:
            result = await client.post("/api/session", json={"token": old})
            self.assertEqual(result.status_code, 200)
            self.assertTrue(
                (await client.get("/api/session")).json()["authenticated"]
            )
        result = await first.post(
            "/api/auth/credential",
            json={"current_token": old, "new_token": NEW},
        )
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["changed"])
        self.assertFalse(result.json()["authenticated"])
        for client in (first, second):
            self.assertFalse(
                (await client.get("/api/session")).json()["authenticated"]
            )
            self.assertEqual(
                (await client.get("/api/control/settings")).status_code, 401
            )
        self.assertEqual(
            (
                await first.post("/api/session", json={"token": old})
            ).status_code,
            401,
        )
        self.assertEqual(
            (
                await first.post("/api/session", json={"token": NEW})
            ).status_code,
            200,
        )
        self.assertEqual(
            (await first.get("/api/control/settings")).status_code, 200
        )
        self.assertEqual((await self.client.get("/settings")).status_code, 200)
        self.assertEqual(self.site.read_bytes(), before)

    async def test_trust_and_bootstrap_not_changed_by_generic_settings(self):
        for key in (
            "PORTAL_AUTH_MODE",
            "PORTAL_ACCESS_TEAM_DOMAIN",
            "PORTAL_ACCESS_AUD",
            "PORTAL_LOGIN_TOKEN",
            "CONTROL_API_TOKEN",
        ):
            response = await self.client.patch(
                "/settings", json={"values": {key: "changed"}}
            )
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("changed", response.text)

    async def test_legacy_bootstrap_and_upgrade_preserve_existing_secrets(
        self,
    ):
        original = self.site.read_text()
        filtered = (
            "\n".join(
                line
                for line in original.splitlines()
                if not line.startswith(
                    (
                        "PORTAL_LOGIN_TOKEN=",
                        "PORTAL_AUTH_MODE=",
                        "PORTAL_ACCESS_TEAM_DOMAIN=",
                        "PORTAL_ACCESS_AUD=",
                    )
                )
            )
            + "\n"
        )
        self.site.write_text(filtered)
        with mock.patch.object(
            sys, "path", [str(ROOT / "scripts"), *sys.path]
        ):
            import settings

            with (
                mock.patch.dict(sys.modules, {"settings": settings}),
                mock.patch.object(
                    sys,
                    "argv",
                    ["configure.py", "--output", str(self.site), "--upgrade"],
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                runpy.run_path(
                    str(ROOT / "scripts/configure.py"), run_name="__main__"
                )
            values = settings.load(self.site)
        self.assertEqual(values["PORTAL_LOGIN_TOKEN"], "")
        self.assertEqual(values["PORTAL_AUTH_MODE"], "token")
        old = dict(
            line.split("=", 1)
            for line in filtered.splitlines()
            if line and not line.startswith("#")
        )
        self.assertTrue(
            all(values[key] == value for key, value in old.items())
        )
        self.assertEqual(
            self.site.with_name("site.env.before-upgrade").read_text(),
            filtered,
        )
        legacy = CredentialStore(self.site.parent, BOOTSTRAP)
        self.assertTrue(legacy.verify(BOOTSTRAP)["valid"])

    async def test_access_trust_configuration_fails_closed(self):
        validate = self.app.state.store.public
        # The real shared loader rejects unsupported trust or local SSO.
        original = self.site.read_text()
        for key, value in (
            ("PORTAL_AUTH_MODE", "unknown"),
            ("PORTAL_AUTH_MODE", "cloudflare-access"),
            (
                "PORTAL_ACCESS_TEAM_DOMAIN",
                "https://other.example.invalid/path",
            ),
            ("PORTAL_ACCESS_AUD", "wrong"),
        ):
            self.site.write_text(
                "\n".join(
                    key + "=" + value if line.startswith(key + "=") else line
                    for line in original.splitlines()
                )
                + "\n"
            )
            with self.assertRaises(ValueError):
                validate()
        self.site.write_text(original)

    async def test_input_errors_do_not_reflect_credentials_or_enter_swagger(
        self,
    ):
        marker = "synthetic-sensitive-" * 20
        response = await self.client.post(
            "/portal-auth/change",
            json={"current_token": marker, "new_token": "short"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(marker, response.text)
        paths = (await self.client.get("/openapi.json")).json()["paths"]
        self.assertIn("/setup/plan", paths)
        self.assertFalse(
            any(path.startswith("/portal-auth") for path in paths)
        )


if __name__ == "__main__":
    unittest.main()
