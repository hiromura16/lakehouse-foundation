"""React BFFの同origin・session・固定proxy・dist境界を合成ASGIで検証する。"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
with mock.patch.object(sys, "path", [str(ROOT / "infra/demo"), *sys.path]):
    import portal_entrypoint
    import portal_gateway as gateway

TOKEN = "a" * 32
LOCAL_ORIGIN = "http://localhost:8501"
UNSAFE = {"Origin": LOCAL_ORIGIN, "X-Poc-Request": "1"}


class Chunks(httpx.AsyncByteStream):
    """closeと有界streamの挙動を、実接続なしで記録する。"""

    def __init__(self, values, delay=0):
        self.values = values
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for value in self.values:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield value

    async def aclose(self):
        self.closed = True


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    """ASGITransportで検証し、実networkとTestClientのthreadを使わない。"""

    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="react-gateway-unit-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.dist = self.directory / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "index.html").write_text(
            '<html><script src="/assets/app.js"></script></html>'
        )
        (self.dist / "assets/app.js").write_text("console.log('synthetic');")
        self.now = [0]
        self.calls = []
        self.upstream = None
        self.values = {
            "CONTROL_API_TOKEN": TOKEN,
            "PORTAL_DIST_DIR": str(self.dist),
            "PORTAL_PROJECT_NAME": "unit-project",
        }
        self.app = self.make_app()
        self.client = self.make_client(self.app)

    def make_app(self, **overrides):
        return gateway.create_app(
            {**self.values, **overrides},
            transport=httpx.MockTransport(self.backend),
            clock=lambda: self.now[0],
        )

    def make_client(self, app, base_url=LOCAL_ORIGIN):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=base_url
        )
        self.addAsyncCleanup(client.aclose)
        return client

    async def backend(self, request):
        self.calls.append(request)
        if isinstance(self.upstream, Exception):
            raise self.upstream
        if self.upstream is not None:
            return self.upstream
        return httpx.Response(200, json={"synthetic": True})

    async def login(self, client=None, origin=LOCAL_ORIGIN):
        client = client or self.client
        response = await client.post(
            "/api/session",
            json={"token": TOKEN},
            headers={"Origin": origin, "X-Poc-Request": "1"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    async def test_session_cookie_is_opaque_scoped_and_revocable(self):
        response = await self.client.get("/api/session")
        self.assertEqual(response.json(), {"authenticated": False})
        response = await self.login()
        cookie = response.headers["set-cookie"]
        self.assertIn("poc_session_unit-project=", cookie)
        for attribute in (
            "HttpOnly",
            "SameSite=strict",
            "Path=/api",
            "Max-Age=28800",
        ):
            self.assertIn(attribute, cookie)
        self.assertNotIn("Domain=", cookie)
        self.assertNotIn(TOKEN, cookie + response.text)
        identifier = self.client.cookies.get("poc_session_unit-project")
        self.assertEqual(len(identifier), 43)
        self.assertTrue(
            (await self.client.get("/api/session")).json()["authenticated"]
        )
        response = await self.client.delete("/api/session", headers=UNSAFE)
        self.assertFalse(response.json()["authenticated"])
        self.assertFalse(self.app.state.sessions.authenticated(identifier))

    async def test_expiry_and_restart_do_not_preserve_authorization(self):
        await self.login()
        identifier = self.client.cookies.get("poc_session_unit-project")
        self.assertFalse(
            self.make_app().state.sessions.authenticated(identifier)
        )
        self.now[0] = gateway.SESSION_SECONDS + 1
        self.assertFalse(
            (await self.client.get("/api/session")).json()["authenticated"]
        )
        self.assertEqual(len(self.app.state.sessions.sessions), 0)

    async def test_login_rotates_cookie_and_projects_have_separate_names(self):
        await self.login()
        before = self.client.cookies.get("poc_session_unit-project")
        await self.login()
        after = self.client.cookies.get("poc_session_unit-project")
        self.assertNotEqual(before, after)
        self.assertFalse(self.app.state.sessions.authenticated(before))
        other = self.make_client(self.make_app(PORTAL_PROJECT_NAME="other"))
        response = await self.login(other)
        self.assertIn("poc_session_other=", response.headers["set-cookie"])
        self.assertFalse(other.cookies.get("poc_session_unit-project"))

    async def test_login_requires_exact_origin_header_and_host(self):
        for headers in (
            {},
            {"Origin": LOCAL_ORIGIN},
            {"X-Poc-Request": "1"},
            {"Origin": "https://attacker.example.com", "X-Poc-Request": "1"},
            {**UNSAFE, "Host": "attacker.example.com"},
            {**UNSAFE, "Host": "127.1:8501"},
        ):
            response = await self.client.post(
                "/api/session", json={"token": TOKEN}, headers=headers
            )
            self.assertEqual(response.status_code, 403)
            self.assertNotIn(TOKEN, response.text)

    async def test_public_origin_requires_exact_authority_and_secure_cookie(
        self,
    ):
        app = self.make_app(
            PORTAL_LINK_MODE="public",
            PORTAL_PUBLIC_ORIGIN="https://portal.example.com/",
        )
        client = self.make_client(app, "https://portal.example.com")
        response = await self.login(client, "https://portal.example.com")
        self.assertIn("Secure", response.headers["set-cookie"])
        response = await client.get(
            "/api/session", headers={"Host": "localhost:8501"}
        )
        self.assertEqual(response.status_code, 403)
        response = await client.get(
            "/api/session", headers={"Origin": "https://else.example.com"}
        )
        self.assertEqual(response.status_code, 403)
        # Dockerの内部probeは公開Hostに偽装しなくても動く。
        response = await client.get(
            "/_stcore/health", headers={"Host": "127.0.0.1:8501"}
        )
        self.assertEqual(response.status_code, 200)

    async def test_explicit_https_default_port_uses_browser_origin(self):
        app = self.make_app(
            PORTAL_LINK_MODE="public",
            PORTAL_PUBLIC_ORIGIN="https://portal.example.com:443/",
        )
        client = self.make_client(app, "https://portal.example.com")
        await self.login(client, "https://portal.example.com")

    async def test_login_failure_rate_limit_and_no_error_reflection(self):
        for _ in range(gateway.LOGIN_ATTEMPTS):
            response = await self.client.post(
                "/api/session",
                json={"token": "synthetic-wrong-token"},
                headers=UNSAFE,
            )
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("synthetic-wrong-token", response.text)
        response = await self.client.post(
            "/api/session", json={"token": TOKEN}, headers=UNSAFE
        )
        self.assertEqual(response.status_code, 429)
        self.now[0] += 61
        await self.login()

    async def test_login_body_and_query_token_are_rejected_safely(self):
        for content in (b'{"token":', b'{"token":1}', b"[]"):
            response = await self.client.post(
                "/api/session", content=content, headers=UNSAFE
            )
            self.assertEqual(response.status_code, 400)
        response = await self.client.post(
            "/api/session", json={"token": "x" * 5000}, headers=UNSAFE
        )
        self.assertEqual(response.status_code, 413)
        response = await self.client.get("/api/session?access_token=synthetic")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("synthetic", response.text)
        response = await self.client.get("/health?token=synthetic")
        self.assertEqual(response.status_code, 400)

    async def test_proxy_authentication_and_outbound_header_allowlist(self):
        response = await self.client.get("/api/control/poc/services")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.calls, [])
        await self.login()
        response = await self.client.get(
            "/api/control/poc/services?scope=unit",
            headers={
                "Authorization": "Bearer browser-value",
                "X-Forwarded-Host": "attacker.example.com",
            },
        )
        self.assertEqual(response.status_code, 200)
        request = self.calls[-1]
        self.assertEqual(
            str(request.url), "http://demo-api:8000/poc/services?scope=unit"
        )
        self.assertEqual(request.headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(request.headers["Accept-Encoding"], "identity")
        self.assertNotIn("cookie", request.headers)
        self.assertNotIn("x-forwarded-host", request.headers)
        self.assertNotIn("browser-value", str(request.headers))

    async def test_proxy_preserves_json_and_rejects_unsafe_destinations(
        self,
    ):
        await self.login()
        response = await self.client.patch(
            "/api/control/settings",
            json={"synthetic": "value"},
            headers=UNSAFE,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls[-1].method, "PATCH")
        self.assertEqual(self.calls[-1].content, b'{"synthetic":"value"}')
        calls = len(self.calls)
        for path in (
            "/api/control//attacker.example.com",
            "/api/control/http://attacker.example.com",
            "/api/control/%2e%2e/settings",
            "/api/control/settings?token=synthetic",
        ):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 400)
        response = await self.client.post(
            "/api/control/jobs",
            json={},
            headers={
                "Origin": "https://attacker.example.com",
                "X-Poc-Request": "1",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.calls), calls)

    async def test_proxy_body_size_stream_limit_is_checked_before_backend(
        self,
    ):
        await self.login()

        async def chunks():
            yield b"x" * gateway.REQUEST_LIMIT
            yield b"x"

        response = await self.client.post(
            "/api/control/jobs",
            content=chunks(),
            headers={**UNSAFE, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.calls, [])

    async def test_proxy_drops_redirect_cookie_cors_and_hop_headers(self):
        await self.login()
        self.upstream = httpx.Response(
            307,
            headers={
                "Location": "https://attacker.example.com",
                "Set-Cookie": "backend=synthetic; Path=/",
                "Access-Control-Allow-Origin": "*",
                "Connection": "keep-alive",
                "Content-Type": "text/plain",
            },
            content=b"fixed",
        )
        response = await self.client.get("/api/control/poc/help")
        self.assertEqual(response.status_code, 307)
        self.assertEqual(len(self.calls), 1)
        for key in (
            "location",
            "set-cookie",
            "access-control-allow-origin",
            "connection",
        ):
            self.assertNotIn(key, response.headers)

    async def test_backend_stream_is_bounded_closed_and_errors_are_normalized(
        self,
    ):
        await self.login()
        stream = Chunks([b"a" * 512, b"b" * 1024])
        self.upstream = httpx.Response(200, stream=stream)
        with mock.patch.object(gateway, "RESPONSE_LIMIT", 1024):
            response = await self.client.get("/api/control/poc/help")
        self.assertEqual(response.status_code, 502)
        self.assertTrue(stream.closed)
        self.upstream = httpx.ConnectError("synthetic-private-error")
        response = await self.client.get("/api/control/poc/help")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("synthetic-private-error", response.text)
        self.upstream = httpx.ReadTimeout("synthetic-private-timeout")
        response = await self.client.get("/api/control/poc/help")
        self.assertEqual(response.status_code, 504)
        self.assertNotIn("synthetic-private-timeout", response.text)

    async def test_whole_response_deadline_and_request_deadline(self):
        await self.login()
        real_timeout = asyncio.timeout
        self.upstream = httpx.Response(200, stream=Chunks([b"x"], delay=0.1))
        # 実5/30秒待機はせず、同じtimeout境界を短縮して取消を確認する。
        with mock.patch.object(
            gateway.asyncio,
            "timeout",
            side_effect=lambda _: real_timeout(0.02),
        ):
            response = await self.client.get("/api/control/poc/help")
        self.assertEqual(response.status_code, 504)

        async def slow_body():
            await asyncio.sleep(0.1)
            yield b"{}"

        with mock.patch.object(
            gateway.asyncio,
            "timeout",
            side_effect=lambda _: real_timeout(0.02),
        ):
            response = await self.client.post(
                "/api/control/jobs",
                content=slow_body(),
                headers={**UNSAFE, "Content-Type": "application/json"},
            )
        self.assertEqual(response.status_code, 408)

    async def test_unexpected_compression_is_rejected_before_decoding(self):
        await self.login()
        stream = Chunks([b"not-even-valid-gzip"])
        self.upstream = httpx.Response(
            200, stream=stream, headers={"Content-Encoding": "gzip"}
        )
        response = await self.client.get("/api/control/poc/help")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"], "backend_encoding_rejected"
        )
        self.assertTrue(stream.closed)

    async def test_spa_security_headers_and_no_api_fallback(self):
        for path in ("/", "/services", "/assets/app.js"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 200)
            policy = response.headers["content-security-policy"]
            self.assertIn("script-src 'self'", policy)
            self.assertNotIn("unsafe-eval", policy)
            self.assertNotIn("unsafe-inline", policy)
            self.assertEqual(response.headers["cache-control"], "no-store")
        for path in (
            "/api",
            "/api/unknown",
            "/.env",
            "/assets/app.js.map",
            "/assets/%2e%2e/private.js",
        ):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn("<html>", response.text)
        response = await self.client.options(
            "/api/session", headers={"Origin": LOCAL_ORIGIN}
        )
        self.assertEqual(response.status_code, 405)
        self.assertNotIn("access-control-allow-origin", response.headers)

    async def test_dist_symlinks_and_missing_build_are_not_ready(self):
        secret = self.directory / "private.js"
        secret.write_text("synthetic-private-content")
        (self.dist / "assets/escape.js").symlink_to(secret)
        response = await self.client.get("/assets/escape.js")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("synthetic-private-content", response.text)
        alias = self.directory / "alias"
        alias.symlink_to(self.dist, target_is_directory=True)
        for root in (alias, self.directory / "not-built"):
            client = self.make_client(self.make_app(PORTAL_DIST_DIR=str(root)))
            self.assertEqual((await client.get("/health")).status_code, 200)
            self.assertEqual(
                (await client.get("/_stcore/health")).status_code, 503
            )
            self.assertEqual((await client.get("/")).status_code, 503)


class GatewayConfigurationTests(unittest.TestCase):
    """任意backend、弱い設定、過大なsession表を許可しない。"""

    def test_fail_closed_configuration(self):
        for override in (
            {"CONTROL_API_TOKEN": ""},
            {"PORTAL_LINK_MODE": "public"},
            {"PORTAL_PUBLIC_ORIGIN": "http://portal.example.com"},
            {"PORTAL_LINK_MODE": "bad"},
            {"DEMO_API_URL": "http://attacker.example.com"},
            {"PORTAL_PROJECT_NAME": "../invalid"},
            {"PORTAL_DIST_DIR": "/"},
            {"PORTAL_DIST_DIR": "/tmp/.ssh/dist"},
        ):
            with (
                self.subTest(keys=tuple(override)),
                self.assertRaises(ValueError),
            ):
                gateway.create_app({"CONTROL_API_TOKEN": TOKEN, **override})

    def test_session_capacity_and_expiry_are_bounded(self):
        now = [0]
        store = gateway.Sessions(lambda: now[0])
        for _ in range(gateway.SESSION_LIMIT):
            store.issue()
        with self.assertRaises(gateway.GatewayProblem):
            store.issue()
        self.assertEqual(len(store.sessions), 128)
        now[0] = gateway.SESSION_SECONDS + 1
        store.issue()
        self.assertEqual(len(store.sessions), 1)

    def test_entrypoint_preserves_streamlit_and_uses_single_react_worker(self):
        streamlit = portal_entrypoint.command({})
        self.assertEqual(
            streamlit[:3], ["streamlit", "run", "infra/demo/app.py"]
        )
        react = portal_entrypoint.command({"PORTAL_UI_MODE": "react"})
        self.assertIn("portal_gateway:create_app", react)
        self.assertEqual(react[react.index("--port") + 1], "8501")
        self.assertEqual(react[react.index("--workers") + 1], "1")
        self.assertIn("--no-access-log", react)
        self.assertIn("--no-proxy-headers", react)
        self.assertNotIn(TOKEN, " ".join(react))
        with self.assertRaises(ValueError):
            portal_entrypoint.command({"PORTAL_UI_MODE": "automatic"})
