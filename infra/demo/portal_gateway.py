"""React用の同一origin BFF。PoC所有者向けで、SSO・role分離は未実装。

root tokenはサーバーだけで保持し、ブラウザーには不透明な短期sessionを渡す。
sessionは1 processのメモリだけに保存し、再起動時にすべて失効する。
既存APIのSQL・CDC・所有権・明示同意の検査を代理実装したり回避したりしない。
"""

import asyncio
import hashlib
import json
import os
import re
import secrets
import stat
import time
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from control_settings import VALIDATOR
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import ClientDisconnect

BACKEND_ORIGIN = "http://demo-api:8000"
REQUEST_LIMIT = 1024 * 1024
RESPONSE_LIMIT = 8 * 1024 * 1024
SESSION_SECONDS = 8 * 60 * 60
SESSION_LIMIT = 128
LOGIN_ATTEMPTS = 10
PROXY_METHODS = ("GET", "POST", "PATCH", "DELETE")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
STATIC_TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
        "object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class GatewayProblem(ValueError):
    """入力・例外本文を含めない固定codeだけをHTTP境界へ運ぶ。"""

    def __init__(self, status, code):
        self.status = status
        self.code = code
        super().__init__(code)


def failure(status, code):
    """秘密値・URL・元例外を応答へ転記しない。"""
    return JSONResponse({"detail": code}, status_code=status)


class Sessions:
    """有界なsessionとglobal login制限。単一event loopから同期操作する。"""

    def __init__(self, clock):
        self.clock = clock
        self.sessions = OrderedDict()
        self.attempts = deque(maxlen=LOGIN_ATTEMPTS)

    @staticmethod
    def key(value):
        """固定長cookie以外を検索せず、メモリにはdigestだけを置く。"""
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", value
        ):
            return None
        return hashlib.sha256(value.encode("ascii")).digest()

    def purge(self):
        """期限は延長せず、最大128件だけを走査する。"""
        now = self.clock()
        for key, expires in list(self.sessions.items()):
            if expires <= now:
                del self.sessions[key]

    def authenticated(self, value):
        self.purge()
        return self.key(value) in self.sessions

    def login_allowed(self):
        now = self.clock()
        while self.attempts and self.attempts[0] <= now - 60:
            self.attempts.popleft()
        if len(self.attempts) >= LOGIN_ATTEMPTS:
            return False
        self.attempts.append(now)
        return True

    def issue(self, previous=None):
        """再login時も新しい256bit値を生成する。容量不足では他人を追放しない。"""
        self.purge()
        self.sessions.pop(self.key(previous), None)
        if len(self.sessions) >= SESSION_LIMIT:
            raise GatewayProblem(503, "session_capacity_reached")
        value = secrets.token_urlsafe(32)
        self.sessions[self.key(value)] = self.clock() + SESSION_SECONDS
        return value

    def revoke(self, value):
        self.sessions.pop(self.key(value), None)


class OriginPolicy:
    """Forwarded系headerを信用せず、明示したorigin/Hostを検査する。"""

    def __init__(self, values):
        self.mode = values.get("PORTAL_LINK_MODE", "local")
        if self.mode not in {"local", "public"}:
            raise ValueError("PORTAL_LINK_MODE must be local or public.")
        self.origin = VALIDATOR.public_origin(
            values.get("PORTAL_PUBLIC_ORIGIN", "")
        )
        # HTTPSの既定portはbrowserのOrigin直列化と同じ形へ揃える。
        if self.origin and urlsplit(self.origin).port == 443:
            self.origin = "https://" + urlsplit(self.origin).hostname
        if self.mode == "public" and not self.origin:
            raise ValueError("Public portal requires its HTTPS origin.")
        self.secure = self.mode == "public"

    def check(self, scope):
        """GETのOriginは任意、変更操作ではOriginと独自headerの両方を要求。"""
        headers = Headers(scope=scope)
        hosts = headers.getlist("host")
        if len(hosts) != 1:
            raise GatewayProblem(403, "host_denied")
        host = hosts[0]
        if self.secure:
            expected = self.origin
            if host.lower() != urlsplit(expected).netloc:
                raise GatewayProblem(403, "host_denied")
        else:
            try:
                parts = urlsplit("http://" + host)
                port = parts.port
                expected_host = parts.hostname or ""
                authority = expected_host + (":" + str(port) if port else "")
                if (
                    expected_host not in {"localhost", "127.0.0.1"}
                    or host.lower() != authority
                    or port is not None
                    and not 1 <= port <= 65535
                ):
                    raise ValueError
            except ValueError:
                raise GatewayProblem(403, "host_denied") from None
            expected = "http://" + authority
        origins = headers.getlist("origin")
        if origins and origins != [expected]:
            raise GatewayProblem(403, "origin_denied")
        if scope["method"] in UNSAFE_METHODS and (
            origins != [expected] or headers.getlist("x-poc-request") != ["1"]
        ):
            raise GatewayProblem(403, "csrf_check_failed")


class PortalSecurity:
    """全応答をhardeningし、APIのCORS許可やSPAへの誤fallbackを防ぐ。"""

    def __init__(self, app, policy):
        self.app = app
        self.policy = policy

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secured(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS.items():
                    headers[key] = value
            await send(message)

        try:
            if len(scope.get("query_string", b"")) > 8192:
                raise GatewayProblem(400, "query_too_large")
            request = Request(scope)
            if any("token" in key.lower() for key in request.query_params):
                raise GatewayProblem(400, "query_token_forbidden")
            if not (
                scope["path"] in {"/health", "/_stcore/health"}
                and scope["method"] == "GET"
            ):
                self.policy.check(scope)
                if scope["method"] == "OPTIONS":
                    raise GatewayProblem(405, "cors_preflight_not_supported")
            await self.app(scope, receive, secured)
        except GatewayProblem as error:
            await failure(error.status, error.code)(scope, receive, secured)


async def request_body(request, *, limit=REQUEST_LIMIT):
    """Content-Lengthを信用せず、stream全体を5秒/1MiBで制限する。"""
    body = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise GatewayProblem(413, "request_too_large")
                body.extend(chunk)
    except TimeoutError:
        raise GatewayProblem(408, "request_timeout") from None
    except ClientDisconnect:
        raise GatewayProblem(400, "request_disconnected") from None
    return bytes(body)


def static_bytes(root, relative):
    """descriptor相対/no-followで公開distのregular fileだけを読む。"""
    if relative.is_absolute() or any(
        part.startswith(".") for part in relative.parts
    ):
        raise GatewayProblem(404, "asset_unavailable")
    descriptor = None
    try:
        # 各祖先もno-followで開き、検査後のpath-based openへ戻らない。
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for part in root.parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        for part in relative.parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        asset = os.open(
            relative.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
        try:
            metadata = os.fstat(asset)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > RESPONSE_LIMIT
            ):
                raise GatewayProblem(404, "asset_unavailable")
            chunks = bytearray()
            while chunk := os.read(
                asset, min(65536, RESPONSE_LIMIT + 1 - len(chunks))
            ):
                chunks.extend(chunk)
                if len(chunks) > RESPONSE_LIMIT:
                    raise GatewayProblem(404, "asset_unavailable")
            return bytes(chunks)
        finally:
            os.close(asset)
    except OSError:
        raise GatewayProblem(404, "asset_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def create_app(values=None, *, transport=None, clock=time.monotonic):
    """環境だけから構成し、private設定fileや外部URLを探索しないfactory。"""
    values = dict(os.environ if values is None else values)
    policy = OriginPolicy(values)
    token = values.get("CONTROL_API_TOKEN", "")
    project = values.get("PORTAL_PROJECT_NAME", "lakehouse")
    if not re.fullmatch(r"[A-Za-z0-9]{20,128}", token):
        raise ValueError("Configure the private portal API token.")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", project):
        raise ValueError("Configure a valid portal project name.")
    if values.get("DEMO_API_URL", BACKEND_ORIGIN) != BACKEND_ORIGIN:
        raise ValueError("Portal backend must remain its fixed internal API.")
    dist = Path(values.get("PORTAL_DIST_DIR", "/opt/lakehouse/frontend/dist"))
    if (
        not dist.is_absolute()
        or ".." in dist.parts
        or str(dist)
        in {
            "/",
            "/etc",
            "/home",
            "/tmp",
            "/srv",
            "/opt",
            "/var",
            "/usr",
            "/proc",
            "/sys",
            "/dev",
            "/run",
        }
    ):
        raise ValueError("Configure an absolute dedicated frontend dist.")
    if {".git", ".ssh", ".aws", ".docker", ".config", ".codex"} & set(
        dist.parts
    ):
        raise ValueError("Configure a dedicated frontend dist.")
    cookie = "poc_session_" + project
    sessions = Sessions(clock)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(PortalSecurity, policy=policy)
    app.state.sessions = sessions

    @app.get("/health")
    async def health():
        """livenessだけを示す。backendやSQLの成功を表さない。"""
        return {"ok": True}

    @app.get("/_stcore/health")
    async def readiness():
        """既存Compose probe互換。未buildのSPAをreadyとは報告しない。"""
        try:
            if not static_bytes(dist, Path("index.html")):
                raise GatewayProblem(503, "frontend_build_required")
        except GatewayProblem:
            return failure(503, "frontend_build_required")
        return {"ok": True}

    @app.get("/api/session")
    async def session_state(request: Request):
        return {
            "authenticated": sessions.authenticated(
                request.cookies.get(cookie)
            )
        }

    @app.post("/api/session")
    async def login(request: Request):
        if not sessions.login_allowed():
            return failure(429, "login_rate_limited")
        try:
            data = json.loads(await request_body(request, limit=4096))
            supplied = data.get("token") if isinstance(data, dict) else None
            if (
                not isinstance(data, dict)
                or set(data) != {"token"}
                or not isinstance(supplied, str)
                or len(supplied) > 256
            ):
                return failure(400, "invalid_login_body")
            if not secrets.compare_digest(supplied.encode(), token.encode()):
                return failure(401, "authentication_failed")
            identifier = sessions.issue(request.cookies.get(cookie))
        except (json.JSONDecodeError, UnicodeError):
            return failure(400, "invalid_login_body")
        except GatewayProblem as error:
            return failure(error.status, error.code)
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            cookie,
            identifier,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=policy.secure,
            samesite="strict",
            path="/api",
        )
        return response

    @app.delete("/api/session")
    async def logout(request: Request):
        sessions.revoke(request.cookies.get(cookie))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(
            cookie,
            path="/api",
            secure=policy.secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.api_route("/api/control/{path:path}", methods=list(PROXY_METHODS))
    async def control(path: str, request: Request):
        if not sessions.authenticated(request.cookies.get(cookie)):
            return failure(401, "authentication_required")
        if (
            not re.fullmatch(r"[A-Za-z0-9_./-]{1,512}", path)
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or b"%" in request.scope.get("raw_path", b"")
            or len(request.scope.get("query_string", b"")) > 8192
        ):
            return failure(400, "invalid_api_path")
        try:
            body = await request_body(request)
            headers = {
                "Authorization": "Bearer " + token,
                "Accept-Encoding": "identity",
            }
            if body:
                if (
                    request.headers.get("content-type", "").split(";")[0]
                    != "application/json"
                ):
                    return failure(415, "json_body_required")
                headers["Content-Type"] = "application/json"
            # clientのCookie/Authorization/Forwarded等をコピーしない。
            async with asyncio.timeout(30):
                async with httpx.AsyncClient(
                    transport=transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(5),
                ) as client:
                    async with client.stream(
                        request.method,
                        BACKEND_ORIGIN + "/" + path,
                        params=list(request.query_params.multi_items()),
                        content=body,
                        headers=headers,
                    ) as upstream:
                        # 圧縮bodyをdecodeせず、byte上限を保つ。
                        if (
                            upstream.headers.get(
                                "Content-Encoding", "identity"
                            ).lower()
                            != "identity"
                        ):
                            return failure(502, "backend_encoding_rejected")
                        content = bytearray()
                        async for chunk in upstream.aiter_bytes():
                            if len(content) + len(chunk) > RESPONSE_LIMIT:
                                return failure(
                                    502, "backend_response_too_large"
                                )
                            content.extend(chunk)
                        # hop header・Cookie・Location・外部CORSを渡さない。
                        content_type = upstream.headers.get(
                            "Content-Type", "application/octet-stream"
                        )
                        if len(content_type) > 128 or not re.fullmatch(
                            r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+"
                            r"(?:;[ A-Za-z0-9=._+\"-]+)?",
                            content_type,
                        ):
                            return failure(502, "invalid_backend_response")
                        return Response(
                            bytes(content),
                            status_code=upstream.status_code,
                            headers={"Content-Type": content_type},
                        )
        except GatewayProblem as error:
            return failure(error.status, error.code)
        except (TimeoutError, httpx.TimeoutException):
            return failure(504, "backend_timeout")
        except httpx.HTTPError:
            return failure(502, "backend_unavailable")

    @app.get("/{path:path}")
    async def frontend(path: str, request: Request):
        if path == "api" or path.startswith("api/"):
            return failure(404, "api_route_not_found")
        if (
            len(path) > 2048
            or b"%" in request.scope.get("raw_path", b"")
            or "\\" in path
            or any(
                not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", part)
                or part in {".", ".."}
                for part in path.split("/")
                if part
            )
            or "//" in path
        ):
            return failure(404, "asset_unavailable")
        selected = Path(path) if path else Path("index.html")
        if not selected.suffix:
            selected = Path("index.html")
        if selected.suffix not in STATIC_TYPES or (
            selected.suffix == ".html" and selected != Path("index.html")
        ):
            return failure(404, "asset_unavailable")
        try:
            payload = static_bytes(dist, selected)
        except GatewayProblem as error:
            # dist未生成は開発時の制御された状態。directory listingへ戻さない。
            if selected == Path("index.html"):
                return failure(503, "frontend_build_required")
            return failure(error.status, error.code)
        return Response(payload, media_type=STATIC_TYPES[selected.suffix])

    return app
