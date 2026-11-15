"""Verify browser identity without exposing server-side API credentials.

Only the configured Cloudflare team can provide signing keys. Request headers
cannot select an issuer, key URL or proxy. Public keys are cached briefly and
failed refreshes never extend their lifetime. Backend credential operations use
three fixed internal endpoints and never return private response bodies.
"""

import asyncio
import base64
import hashlib
import json
import re
import ssl
import time
from urllib.parse import urlsplit
from urllib.request import proxy_bypass_environment

import httpx
import jwt

BACKEND_ORIGIN = "http://demo-api:8000"
JSON_LIMIT = 64 * 1024
JWT_LIMIT = 16 * 1024
KEY_CACHE_SECONDS = 300
KEY_REFRESH_SECONDS = 30
# install_ca.py installs the optional company CA into this image-owned trust
# bundle. trust_env=False deliberately ignores SSL_CERT_FILE, so pass an
# explicit context instead of httpx's default certifi-only verification.
SYSTEM_CA_FILE = "/etc/ssl/certs/ca-certificates.crt"


class AuthProblem(ValueError):
    """Carry a fixed public error code, never an upstream exception body."""

    def __init__(self, status, code):
        self.status = status
        self.code = code
        super().__init__(code)


def unique_object(pairs):
    """Reject ambiguous duplicate JSON keys before interpreting credentials."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def strict_json(value):
    """Use the same unambiguous JSON rules for JWTs and auth responses."""

    def reject_constant(_):
        raise ValueError("Invalid JSON constant.")

    return json.loads(
        value,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


async def bounded_json(client, method, url, *, headers=None, payload=None):
    """Bound the whole operation, response bytes and accepted encodings."""
    async with asyncio.timeout(5):
        async with client.stream(
            method, url, headers=headers, json=payload
        ) as response:
            if response.status_code != 200:
                raise AuthProblem(
                    response.status_code
                    if response.status_code in {401, 422, 429}
                    else 503,
                    "authentication_unavailable",
                )
            if (
                response.headers.get("content-encoding", "identity").lower()
                != "identity"
            ):
                raise AuthProblem(503, "authentication_unavailable")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > JSON_LIMIT:
                    raise AuthProblem(503, "authentication_unavailable")
            return strict_json(bytes(data))


class CredentialBackend:
    """Keep mutable login proof in the existing private API state store."""

    def __init__(self, token, transport=None):
        self.token = token
        self.transport = transport

    async def call(self, action, payload=None):
        if action not in {"verify", "status", "change"}:
            raise AuthProblem(503, "authentication_unavailable")
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=3,
            ) as client:
                result = await bounded_json(
                    client,
                    "GET" if action == "status" else "POST",
                    BACKEND_ORIGIN + "/portal-auth/" + action,
                    headers={
                        "Authorization": "Bearer " + self.token,
                        "Accept-Encoding": "identity",
                    },
                    payload=payload,
                )
            flag = {
                "verify": "valid",
                "status": "configured",
                "change": "changed",
            }[action]
            if (
                not isinstance(result, dict)
                or set(result) != {flag, "revision"}
                or type(result[flag]) is not bool
                or not isinstance(result["revision"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", result["revision"])
                or action == "change"
                and result[flag] is not True
            ):
                raise AuthProblem(503, "authentication_unavailable")
            return result
        except AuthProblem:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
            raise AuthProblem(503, "authentication_unavailable") from None


class AccessVerifier:
    """Verify a human application JWT against a fixed team and audience.

    Source: Cloudflare Access application-token and validating-json docs. A
    service token (empty subject/common_name) is not a browser owner identity.
    Revocation at Access can only be reflected once the submitted signed JWT
    stops being accepted/expires; local validation is not an introspection API.
    """

    def __init__(self, values, *, transport=None, clock=time.monotonic):
        domain = values.get("PORTAL_ACCESS_TEAM_DOMAIN", "")
        audience = values.get("PORTAL_ACCESS_AUD", "")
        if not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
            r"\.cloudflareaccess\.com",
            domain,
        ) or not re.fullmatch(r"[a-f0-9]{64}", audience):
            raise ValueError("Configure the Access team and application AUD.")
        self.issuer = "https://" + domain
        self.audience = audience
        self.transport = transport
        self.clock = clock
        # Internet key retrieval is the only auth hop that may use the site's
        # explicitly selected proxy. Never inherit shell proxy settings or
        # send the internal credential/backend requests through that proxy.
        proxy = values.get("HTTPS_PROXY") or values.get("https_proxy", "")
        if (
            values.get("HTTPS_PROXY")
            and values.get("https_proxy")
            and values["HTTPS_PROXY"] != values["https_proxy"]
        ):
            raise ValueError("Keep selected HTTPS proxy variants consistent.")
        bypass = (
            values.get("LAKEHOUSE_NO_PROXY")
            or values.get("NO_PROXY")
            or values.get("no_proxy", "")
        )
        if not isinstance(bypass, str) or any(
            character in bypass for character in "\r\n\x00"
        ):
            raise ValueError("Configure a valid selected proxy bypass list.")
        if not isinstance(proxy, str):
            raise ValueError("Configure a valid selected HTTPS proxy.")
        if proxy:
            try:
                parts = urlsplit(proxy)
                if (
                    len(proxy) > 4096
                    or parts.scheme not in {"http", "https"}
                    or not parts.hostname
                    or parts.port is None
                    or parts.path not in {"", "/"}
                    or parts.query
                    or parts.fragment
                    or any(character.isspace() for character in proxy)
                ):
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError(
                    "Configure a valid selected HTTPS proxy."
                ) from None
        self.proxy = (
            proxy
            if proxy and not proxy_bypass_environment(domain, {"no": bypass})
            else None
        )
        self.keys = {}
        self.fetched_at = float("-inf")
        self.attempted_at = float("-inf")
        self.lock = asyncio.Lock()

    async def signing_key(self, kid):
        """Refresh at most once per30s, with no stale-on-error fallback."""
        async with self.lock:
            now = self.clock()
            if kid in self.keys and now - self.fetched_at < KEY_CACHE_SECONDS:
                return self.keys[kid]
            if now - self.attempted_at < KEY_REFRESH_SECONDS:
                raise AuthProblem(401, "access_identity_required")
            self.attempted_at = now
            try:
                context = ssl.create_default_context(cafile=SYSTEM_CA_FILE)
                proxy = self.proxy
                if proxy and urlsplit(proxy).scheme == "https":
                    # Target TLS and HTTPS-proxy TLS are distinct channels;
                    # both use the same fixed image trust bundle. No browser
                    # input or ambient CA/proxy setting selects trust roots.
                    proxy = httpx.Proxy(proxy, ssl_context=context)
                async with httpx.AsyncClient(
                    transport=self.transport,
                    proxy=proxy,
                    verify=context,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=3,
                ) as client:
                    result = await bounded_json(
                        client,
                        "GET",
                        self.issuer + "/cdn-cgi/access/certs",
                        headers={"Accept-Encoding": "identity"},
                    )
                candidates = result.get("keys")
                if not isinstance(candidates, list) or not (
                    1 <= len(candidates) <= 16
                ):
                    raise ValueError("Invalid signing key set.")
                keys = {}
                for item in candidates:
                    if (
                        not isinstance(item, dict)
                        or item.get("kty") != "RSA"
                        or item.get("alg") != "RS256"
                        or item.get("use") != "sig"
                        or not isinstance(item.get("kid"), str)
                        or not re.fullmatch(
                            r"[A-Za-z0-9_-]{1,128}", item["kid"]
                        )
                        or item["kid"] in keys
                        or any(key in item for key in ("d", "p", "q"))
                        or not all(
                            isinstance(item.get(key), str)
                            and re.fullmatch(
                                r"[A-Za-z0-9_-]{1,2048}", item[key]
                            )
                            for key in ("e", "n")
                        )
                    ):
                        raise ValueError("Invalid signing key.")
                    public = jwt.algorithms.RSAAlgorithm.from_jwk(item)
                    if not 2048 <= public.key_size <= 8192:
                        raise ValueError("Invalid RSA key size.")
                    keys[item["kid"]] = public
                self.keys = keys
                self.fetched_at = self.clock()
            except (
                AuthProblem,
                httpx.HTTPError,
                OSError,
                TimeoutError,
                ValueError,
                TypeError,
                AttributeError,
                jwt.PyJWTError,
            ):
                raise AuthProblem(503, "access_keys_unavailable") from None
            if kid not in self.keys:
                raise AuthProblem(401, "access_identity_required")
            return self.keys[kid]

    async def verify(self, headers):
        """Reject header spoofing, algorithm confusion and nonhuman tokens."""
        values = headers.getlist("cf-access-jwt-assertion")
        if len(values) != 1 or not 1 <= len(values[0]) <= JWT_LIMIT:
            raise AuthProblem(401, "access_identity_required")
        encoded = values[0]
        try:
            parts = encoded.split(".")
            if len(parts) != 3 or not all(
                re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts
            ):
                raise ValueError("Invalid compact JWT.")
            documents = [
                strict_json(
                    base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
                )
                for part in parts[:2]
            ]
            header, claims = documents
            if (
                not isinstance(header, dict)
                or not isinstance(claims, dict)
                or header.get("alg") != "RS256"
                or not isinstance(header.get("kid"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", header["kid"])
                or any(key in header for key in ("crit", "jku", "x5u"))
                or claims.get("type") != "app"
                or claims.get("common_name") is not None
                or not isinstance(claims.get("sub"), str)
                or not 1 <= len(claims["sub"]) <= 256
                or not isinstance(claims.get("email"), str)
                or not re.fullmatch(
                    r"[^\s@\x00-\x1f]{1,200}@"
                    r"[^\s@\x00-\x1f]{1,100}",
                    claims["email"],
                )
                or any(
                    type(claims.get(key)) is not int for key in ("exp", "iat")
                )
                or "nbf" in claims
                and type(claims["nbf"]) is not int
                or claims["iat"] >= claims["exp"]
            ):
                raise ValueError("Invalid application token.")
            public_key = await self.signing_key(header["kid"])
            verified = jwt.decode(
                encoded,
                public_key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={
                    "require": ["iss", "aud", "exp", "iat", "sub"],
                },
            )
            # Store no email or JWT in the browser/session. The issuer and
            # account subject bind an opaque local cookie to one principal.
            return hashlib.sha256(
                (self.issuer + "\0" + verified["sub"]).encode("utf-8")
            ).hexdigest()
        except AuthProblem:
            raise
        except (
            jwt.PyJWTError,
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
        ):
            raise AuthProblem(401, "access_identity_required") from None
