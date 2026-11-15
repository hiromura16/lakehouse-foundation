"""Synthetic signed Access JWTs and mocked transports; no live identities."""

import asyncio
import base64
import json
import os
import ssl
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.datastructures import Headers

ROOT = Path(__file__).resolve().parents[1]
with mock.patch.object(sys, "path", [str(ROOT / "infra/demo"), *sys.path]):
    import portal_auth
    import portal_gateway

AUDIENCE = "a" * 64
TEAM = "synthetic-team.cloudflareaccess.com"
ORIGIN = "https://portal.example.com"
CONTROL = "b" * 32
VALUES = {
    "CONTROL_API_TOKEN": CONTROL,
    "PORTAL_LINK_MODE": "public",
    "PORTAL_PUBLIC_ORIGIN": ORIGIN,
    "PORTAL_AUTH_MODE": "cloudflare-access",
    "PORTAL_ACCESS_TEAM_DOMAIN": TEAM,
    "PORTAL_ACCESS_AUD": AUDIENCE,
}
CSRF = {"Origin": ORIGIN, "X-Poc-Request": "1"}


class SignedAccessTests(unittest.IsolatedAsyncioTestCase):
    """Real RSA signatures, synthetic claims, no Cloudflare/network access."""

    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        cls.jwk = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(cls.private_key.public_key())
        )
        cls.jwk.update({"kid": "synthetic-key", "alg": "RS256", "use": "sig"})

    async def asyncSetUp(self):
        # All Access requests below use MockTransport. Keep even the trust
        # context synthetic: no host CA file is a test fixture or is read.
        trust = mock.patch.object(
            portal_auth.ssl,
            "create_default_context",
            return_value=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )
        trust.start()
        self.addCleanup(trust.stop)
        self.now = [0]
        self.key_requests = []
        self.backend_requests = []
        self.key_result = None

        async def keys(request):
            self.key_requests.append(request)
            if isinstance(self.key_result, Exception):
                raise self.key_result
            if self.key_result is not None:
                return self.key_result
            return httpx.Response(200, json={"keys": [self.jwk]})

        async def backend(request):
            self.backend_requests.append(request)
            return httpx.Response(200, json={"synthetic": True})

        self.keys_transport = httpx.MockTransport(keys)
        self.verifier = portal_auth.AccessVerifier(
            VALUES, transport=self.keys_transport, clock=lambda: self.now[0]
        )
        self.app = portal_gateway.create_app(
            VALUES,
            access_transport=self.keys_transport,
            transport=httpx.MockTransport(backend),
            clock=lambda: self.now[0],
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN
        )
        self.addAsyncCleanup(self.client.aclose)

    def token(self, *, remove=(), headers=None, **overrides):
        now = int(time.time())
        claims = {
            "iss": "https://" + TEAM,
            "aud": [AUDIENCE],
            "iat": now - 1,
            "nbf": now - 1,
            "exp": now + 300,
            "sub": "synthetic-owner",
            "type": "app",
            "email": "owner@example.com",
            **overrides,
        }
        for key in remove:
            claims.pop(key, None)
        return jwt.encode(
            claims,
            self.private_key,
            algorithm="RS256",
            headers={"kid": "synthetic-key", **(headers or {})},
        )

    def assertion(self, token=None):
        return {"Cf-Access-Jwt-Assertion": token or self.token()}

    async def login(self, token=None):
        response = await self.client.post(
            "/api/session",
            headers={**CSRF, **self.assertion(token)},
            json={"method": "cloudflare-access"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response

    async def test_signature_audience_issuer_and_human_claims_are_verified(
        self,
    ):
        binding = await self.verifier.verify(Headers(self.assertion()))
        self.assertRegex(binding, r"^[a-f0-9]{64}$")
        self.assertEqual(len(self.key_requests), 1)
        request = self.key_requests[0]
        self.assertEqual(
            str(request.url), "https://" + TEAM + "/cdn-cgi/access/certs"
        )
        self.assertNotIn("authorization", request.headers)
        self.assertNotIn("cookie", request.headers)
        again = await self.verifier.verify(Headers(self.assertion()))
        self.assertEqual(binding, again)
        self.assertEqual(len(self.key_requests), 1)

    async def test_invalid_claims_never_become_an_owner(self):
        now = int(time.time())
        for overrides in (
            {"iss": "https://other-team.cloudflareaccess.com"},
            {"aud": ["c" * 64]},
            {"exp": now - 1},
            {"iat": now + 60},
            {"nbf": now + 60},
            {"sub": ""},
            {"type": "org"},
            {"email": "invalid"},
            {"common_name": "synthetic-service.access"},
            {"exp": True},
            {"iat": "123"},
            {"nbf": False},
        ):
            with self.subTest(fields=tuple(overrides)):
                with self.assertRaises(portal_auth.AuthProblem):
                    await self.verifier.verify(
                        Headers(self.assertion(self.token(**overrides)))
                    )
        for field in ("iss", "aud", "exp", "iat", "sub", "email", "type"):
            with self.subTest(missing=field):
                with self.assertRaises(portal_auth.AuthProblem):
                    await self.verifier.verify(
                        Headers(self.assertion(self.token(remove=(field,))))
                    )
        # nbf is checked when supplied; JWT does not require its presence.
        await self.verifier.verify(
            Headers(self.assertion(self.token(remove=("nbf",))))
        )

    async def test_spoofed_email_duplicate_or_missing_jwt_are_rejected(self):
        for headers in (
            Headers(
                {
                    "Cf-Access-Authenticated-User-Email": "owner@example.com",
                }
            ),
            Headers(),
            Headers(
                raw=[
                    (b"cf-access-jwt-assertion", self.token().encode()),
                    (b"cf-access-jwt-assertion", self.token().encode()),
                ]
            ),
            Headers(self.assertion("x" * (portal_auth.JWT_LIMIT + 1))),
            Headers(self.assertion("not.a.jwt")),
        ):
            with self.assertRaises(portal_auth.AuthProblem):
                await self.verifier.verify(headers)
        self.assertEqual(self.key_requests, [])

    async def test_algorithm_confusion_and_untrusted_key_urls_are_rejected(
        self,
    ):
        unsigned = jwt.encode(
            {"sub": "synthetic-owner"},
            key=None,
            algorithm="none",
            headers={"kid": "synthetic-key"},
        )
        hmac = jwt.encode(
            {"sub": "synthetic-owner"},
            "synthetic-hmac-key" * 4,
            algorithm="HS256",
            headers={"kid": "synthetic-key"},
        )
        for token in (
            unsigned,
            hmac,
            self.token(headers={"jku": "https://attacker.example.com/keys"}),
            self.token(headers={"x5u": "http://127.0.0.1/private"}),
            self.token(headers={"crit": ["custom"]}),
        ):
            with self.assertRaises(portal_auth.AuthProblem):
                await self.verifier.verify(Headers(self.assertion(token)))
        self.assertEqual(self.key_requests, [])

    async def test_invalid_signature_does_not_create_a_session(self):
        foreign = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        claims = jwt.decode(self.token(), options={"verify_signature": False})
        token = jwt.encode(
            claims,
            foreign,
            algorithm="RS256",
            headers={"kid": "synthetic-key"},
        )
        response = await self.client.post(
            "/api/session",
            headers={**CSRF, **self.assertion(token)},
            json={"method": "cloudflare-access"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.app.state.sessions.sessions, {})
        self.assertNotIn(token, response.text)

    async def test_ambiguous_jwt_json_is_rejected_before_key_lookup(self):
        header = (
            base64.urlsafe_b64encode(b'{"alg":"RS256","kid":"synthetic-key"}')
            .decode()
            .rstrip("=")
        )
        for body in (
            b'{"sub":"first","sub":"second"}',
            b'{"exp":NaN}',
            b'{"exp":Infinity}',
        ):
            payload = base64.urlsafe_b64encode(body).decode().rstrip("=")
            token = header + "." + payload + ".c2lnbmF0dXJl"
            with self.assertRaises(portal_auth.AuthProblem):
                await self.verifier.verify(Headers(self.assertion(token)))
        self.assertEqual(self.key_requests, [])

    async def test_sso_requires_signed_identity_on_every_control_request(
        self,
    ):
        response = await self.login()
        self.assertEqual(response.json()["mode"], "cloudflare-access")
        self.assertFalse(response.json()["credential_change_supported"])
        self.assertEqual(
            response.json()["logout_url"], "/cdn-cgi/access/logout"
        )
        self.assertIn("Secure", response.headers["set-cookie"])
        self.assertNotIn("owner@example.com", response.text)
        self.assertNotIn("synthetic-owner", response.text)
        response = await self.client.get("/api/control/poc/services")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.backend_requests, [])
        response = await self.client.get(
            "/api/control/poc/services", headers=self.assertion()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.backend_requests), 1)
        backend = self.backend_requests[0]
        self.assertEqual(backend.headers["authorization"], "Bearer " + CONTROL)
        self.assertNotIn("cf-access-jwt-assertion", backend.headers)
        self.assertNotIn("cookie", backend.headers)

    async def test_cookie_cannot_move_to_another_access_principal(self):
        await self.login()
        response = await self.client.get(
            "/api/session",
            headers=self.assertion(
                self.token(
                    sub="synthetic-other-owner", email="other@example.com"
                )
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["authenticated"])
        self.assertEqual(self.app.state.sessions.sessions, {})

    async def test_access_expiry_is_not_extended_by_the_local_cookie(self):
        await self.login()
        response = await self.client.get(
            "/api/control/poc/services",
            headers=self.assertion(
                self.token(
                    iat=int(time.time()) - 100, exp=int(time.time()) - 1
                )
            ),
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.backend_requests, [])

    async def test_sso_never_accepts_manual_token_or_rotates_api_credential(
        self,
    ):
        response = await self.client.post(
            "/api/session", headers=CSRF, json={"token": CONTROL}
        )
        self.assertEqual(response.status_code, 400)
        response = await self.client.post(
            "/api/auth/credential",
            headers=CSRF,
            json={"current_token": CONTROL, "new_token": "new" * 16},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.backend_requests, [])

    async def test_logout_clears_only_local_session_and_offers_access_logout(
        self,
    ):
        await self.login()
        response = await self.client.delete("/api/session", headers=CSRF)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["authenticated"])
        self.assertEqual(
            response.json()["logout_url"], "/cdn-cgi/access/logout"
        )
        self.assertEqual(self.app.state.sessions.sessions, {})
        # GET never signs the browser straight back in after logout.
        response = await self.client.get(
            "/api/session", headers=self.assertion()
        )
        self.assertFalse(response.json()["authenticated"])

    async def test_key_cache_expiry_and_refresh_failure_do_not_use_stale_keys(
        self,
    ):
        await self.verifier.verify(Headers(self.assertion()))
        self.now[0] = portal_auth.KEY_CACHE_SECONDS + 1
        self.key_result = httpx.Response(503, text="synthetic-private-error")
        with self.assertRaises(portal_auth.AuthProblem) as raised:
            await self.verifier.verify(Headers(self.assertion()))
        self.assertEqual(raised.exception.code, "access_keys_unavailable")
        self.assertNotIn("synthetic-private-error", str(raised.exception))
        self.assertEqual(len(self.key_requests), 2)
        with self.assertRaises(portal_auth.AuthProblem):
            await self.verifier.verify(Headers(self.assertion()))
        self.assertEqual(len(self.key_requests), 2)

    async def test_unknown_kids_cannot_trigger_unbounded_refresh(self):
        await self.verifier.verify(Headers(self.assertion()))
        for _ in range(3):
            with self.assertRaises(portal_auth.AuthProblem):
                await self.verifier.verify(
                    Headers(
                        self.assertion(
                            self.token(headers={"kid": "unknown-key"})
                        )
                    )
                )
        self.assertEqual(len(self.key_requests), 1)

    async def test_jwks_redirect_encoding_size_and_bad_keys_are_rejected(self):
        for response in (
            httpx.Response(
                302, headers={"Location": "https://else.example.com"}
            ),
            httpx.Response(200, json={"keys": []}),
            httpx.Response(200, json={"keys": [self.jwk, self.jwk]}),
            httpx.Response(200, json={"keys": [{**self.jwk, "kty": "oct"}]}),
            httpx.Response(200, content=b"x" * (portal_auth.JSON_LIMIT + 1)),
            httpx.Response(200, content=b'{"keys":[],"keys":[]}'),
            httpx.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                stream=httpx.ByteStream(b"untrusted-compressed-response"),
            ),
        ):
            self.key_result = response
            self.now[0] += 31
            with self.assertRaises(portal_auth.AuthProblem):
                await self.verifier.verify(Headers(self.assertion()))
        for request in self.key_requests:
            self.assertEqual(request.url.host, TEAM)

    async def test_only_jwks_uses_the_explicit_site_proxy(self):
        proxy = "http://proxy.example.internal:8080"
        verifier = portal_auth.AccessVerifier(
            {**VALUES, "HTTPS_PROXY": proxy, "https_proxy": proxy},
            transport=self.keys_transport,
        )
        real_client = httpx.AsyncClient
        selected = []

        def synthetic_client(**kwargs):
            # Inspect the selected proxy without contacting it. A supplied
            # httpx proxy otherwise creates a real proxy transport, even when
            # a MockTransport was provided for the direct/default route.
            selected.append(kwargs.pop("proxy", None))
            return real_client(**kwargs)

        with mock.patch.object(
            portal_auth.httpx, "AsyncClient", side_effect=synthetic_client
        ):
            await verifier.verify(Headers(self.assertion()))
            backend = portal_auth.CredentialBackend(
                CONTROL,
                httpx.MockTransport(
                    lambda _: httpx.Response(
                        200,
                        json={
                            "configured": False,
                            "revision": "b" * 64,
                        },
                    )
                ),
            )
            await backend.call("status")
        self.assertEqual(selected, [proxy, None])

    async def test_jwks_and_https_proxy_use_only_the_fixed_image_ca_bundle(
        self,
    ):
        for proxy in (
            None,
            "http://proxy.example.internal:8080",
            "https://proxy.example.internal:8443",
        ):
            with self.subTest(
                proxy_scheme=proxy.split(":")[0] if proxy else None
            ):
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                verifier = portal_auth.AccessVerifier(
                    {**VALUES, **({"HTTPS_PROXY": proxy} if proxy else {})},
                    transport=self.keys_transport,
                )
                selected = []
                real_client = httpx.AsyncClient

                def synthetic_client(**kwargs):
                    selected.append(kwargs.copy())
                    # The selected HTTPS proxy object is inspected, then
                    # removed so MockTransport cannot accidentally route
                    # through the real proxy transport. No TLS handshake or
                    # real CA file is used by this synthetic regression.
                    kwargs.pop("proxy", None)
                    return real_client(**kwargs)

                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "SSL_CERT_FILE": "/synthetic/untrusted-ca.pem",
                            "SSL_CERT_DIR": "/synthetic/untrusted-ca-dir",
                        },
                    ),
                    mock.patch.object(
                        portal_auth.ssl,
                        "create_default_context",
                        return_value=context,
                    ) as factory,
                    mock.patch.object(
                        portal_auth.httpx,
                        "AsyncClient",
                        side_effect=synthetic_client,
                    ),
                ):
                    await verifier.verify(Headers(self.assertion()))
                factory.assert_called_once_with(
                    cafile="/etc/ssl/certs/ca-certificates.crt"
                )
                self.assertEqual(len(selected), 1)
                self.assertIs(selected[0]["verify"], context)
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertFalse(selected[0]["trust_env"])
                if proxy and proxy.startswith("https:"):
                    selected_proxy = selected[0]["proxy"]
                    self.assertIsInstance(selected_proxy, httpx.Proxy)
                    self.assertIs(selected_proxy.ssl_context, context)
                    self.assertEqual(str(selected_proxy.url), proxy)
                else:
                    self.assertEqual(selected[0]["proxy"], proxy)

    async def test_missing_image_ca_fails_closed_before_any_key_request(self):
        with (
            mock.patch.dict(
                os.environ,
                {
                    "SSL_CERT_FILE": "/synthetic/alternative-ca.pem",
                },
            ),
            mock.patch.object(
                portal_auth.ssl,
                "create_default_context",
                side_effect=FileNotFoundError("synthetic-private-CA-error"),
            ) as factory,
            mock.patch.object(portal_auth.httpx, "AsyncClient") as client,
        ):
            with self.assertRaises(portal_auth.AuthProblem) as raised:
                await self.verifier.verify(Headers(self.assertion()))
        factory.assert_called_once_with(
            cafile="/etc/ssl/certs/ca-certificates.crt"
        )
        client.assert_not_called()
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.code, "access_keys_unavailable")
        self.assertNotIn("synthetic-private-CA-error", str(raised.exception))
        self.assertEqual(self.key_requests, [])

    async def test_selected_no_proxy_can_bypass_only_the_configured_team(self):
        verifier = portal_auth.AccessVerifier(
            {
                **VALUES,
                "HTTPS_PROXY": "http://proxy.example.internal:8080",
                "NO_PROXY": "localhost,.cloudflareaccess.com",
            },
            transport=self.keys_transport,
        )
        self.assertIsNone(verifier.proxy)
        await verifier.verify(Headers(self.assertion()))
        self.assertEqual(self.key_requests[0].url.host, TEAM)


class AuthConfigurationTests(unittest.TestCase):
    """No arbitrary issuer URLs or accidental SSO enablement."""

    def test_invalid_settings_fail_closed(self):
        for override in (
            {"PORTAL_AUTH_MODE": "automatic"},
            {"PORTAL_LINK_MODE": "local"},
            {"PORTAL_ACCESS_TEAM_DOMAIN": "https://" + TEAM},
            {"PORTAL_ACCESS_TEAM_DOMAIN": TEAM + "/keys"},
            {"PORTAL_ACCESS_TEAM_DOMAIN": "127.0.0.1"},
            {"PORTAL_ACCESS_TEAM_DOMAIN": "synthetic.example.com"},
            {"PORTAL_ACCESS_AUD": ""},
            {"PORTAL_ACCESS_AUD": "x" * 64},
            {"HTTPS_PROXY": "http://proxy.example.internal:not-a-port"},
            {"HTTPS_PROXY": "http://proxy.example.internal:8080/path"},
            {"HTTPS_PROXY": "http://proxy.example.internal:8080?value=1"},
            {"HTTPS_PROXY": 1},
            {"NO_PROXY": "localhost\r\ninvalid"},
            {
                "HTTPS_PROXY": "http://proxy.example.internal:8080",
                "https_proxy": "http://other.example.internal:8080",
            },
        ):
            with self.subTest(fields=tuple(override)):
                with self.assertRaises(ValueError):
                    portal_gateway.create_app({**VALUES, **override})


class CredentialBackendTests(unittest.IsolatedAsyncioTestCase):
    """An old/unavailable API never falls back to a formerly valid secret."""

    async def test_unavailable_bad_shape_and_redirect_responses_fail_closed(
        self,
    ):
        for response in (
            httpx.Response(404),
            httpx.Response(
                302, headers={"Location": "https://else.example.com"}
            ),
            httpx.Response(200, json={"configured": False, "revision": "bad"}),
            httpx.Response(200, json={"configured": 1, "revision": "b" * 64}),
            httpx.Response(
                200,
                json={
                    "configured": True,
                    "revision": "b" * 64,
                    "token": "private",
                },
            ),
        ):
            calls = []

            def handler(request):
                calls.append(request)
                return response

            backend = portal_auth.CredentialBackend(
                CONTROL, httpx.MockTransport(handler)
            )
            with self.assertRaises(portal_auth.AuthProblem) as raised:
                await backend.call("status")
            self.assertEqual(
                str(raised.exception), "authentication_unavailable"
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                str(calls[0].url), "http://demo-api:8000/portal-auth/status"
            )

    async def test_whole_response_budget_closes_slow_upstream(self):
        class Slow(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                await asyncio.sleep(0.1)
                yield b"{}"

            async def aclose(self):
                self.closed = True

        stream = Slow()
        backend = portal_auth.CredentialBackend(
            CONTROL,
            httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        )
        timeout = asyncio.timeout
        with mock.patch.object(
            portal_auth.asyncio, "timeout", side_effect=lambda _: timeout(0.01)
        ):
            with self.assertRaises(portal_auth.AuthProblem):
                await backend.call("status")
        self.assertTrue(stream.closed)
