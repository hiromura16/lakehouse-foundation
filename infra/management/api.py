"""Independent unprivileged PoC manager; it never invokes Docker or a shell."""

import hashlib
import os
import re
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from infra.demo.portal_auth import AccessVerifier, AuthProblem

from .journal import (
    Journal,
    StateError,
    operation_view,
    private_directory,
    private_read,
    strict_json,
)
from .planning import POC_IDS, PlanError

COOKIE = "poc_manager_session"
SESSION_TTL = 3600
SECURITY = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


class StrictModel(BaseModel):
    """Reject unknown request fields and implicit type coercion."""

    model_config = ConfigDict(strict=True, extra="forbid")


class Action(StrictModel):
    """Accept only the three whole-PoC lifecycle actions."""

    action: str = Field(pattern=r"^(start|stop|restart)$")


class Submission(StrictModel):
    """Confirm a plan and optionally acknowledge possible interrupted work."""

    plan_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    confirmation: str = Field(pattern=r"^[a-f0-9]{32}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")
    interruption_ack: bool = False


class Claim(StrictModel):
    """Identify an enrolled runner and its frozen manifest."""

    runner_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class Result(StrictModel):
    """Accept only bounded safe runner result fields."""

    runner_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    operation_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    lease_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    status: str = Field(pattern=r"^(succeeded|failed|unknown)$")
    completed_steps: int = Field(ge=0, le=256)
    error_code: str | None = Field(default=None, max_length=64)


class Heartbeat(StrictModel):
    """Identify one existing operation lease without broadening its scope."""

    runner_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    operation_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    lease_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class Reconcile(StrictModel):
    """Identify the explicit operator reconciliation, never a retry."""

    runner_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    operation_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    lease_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@dataclass(frozen=True)
class Settings:
    """Hold selected manager paths and origin; credentials stay in protected
    files.
    """

    state_dir: Path
    token_file: Path
    runner_token_file: Path
    public_origin: str = "http://127.0.0.1:8010"
    auth_mode: str = "token"
    access_team_domain: str = ""
    access_aud: str = ""
    access_proxy_settings: dict | None = None


def token_file(path):
    """Read one protected 256-bit credential; callers never print its value."""
    path = Path(path)
    directory = private_directory(path.parent)
    try:
        raw = (
            private_read(directory, path.name, maximum=128)
            .decode("ascii")
            .strip()
        )
        if not re.fullmatch(r"[a-f0-9]{64}", raw):
            raise StateError("AUTH_CONFIGURATION_INVALID")
        return raw
    finally:
        os.close(directory)


def environment_settings():
    """Read only manager selections at explicit application startup."""
    return Settings(
        Path(os.environ.get("MANAGER_STATE_DIR", "/state")),
        Path(
            os.environ.get(
                "MANAGER_TOKEN_FILE", "/run/manager-secrets/api.token"
            )
        ),
        Path(
            os.environ.get(
                "MANAGER_RUNNER_TOKEN_FILE",
                "/run/manager-secrets/runner.token",
            )
        ),
        os.environ.get("MANAGER_PUBLIC_ORIGIN", "http://127.0.0.1:8010"),
        os.environ.get("MANAGER_AUTH_MODE", "token"),
        os.environ.get("MANAGER_ACCESS_TEAM_DOMAIN", ""),
        os.environ.get("MANAGER_ACCESS_AUD", ""),
        {
            key: os.environ.get(key, "")
            for key in (
                "HTTPS_PROXY",
                "https_proxy",
                "NO_PROXY",
                "no_proxy",
                "LAKEHOUSE_NO_PROXY",
            )
        },
    )


def create_app(
    settings=None, *, journal=None, tokens=None, verifier=None, clock=time.time
):
    """An explicit factory; importing the module reads no private state."""
    settings = settings or environment_settings()
    parts = urlsplit(settings.public_origin)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.path
        or parts.query
        or parts.fragment
        or parts.scheme == "http"
        and parts.hostname not in {"localhost", "127.0.0.1", "::1"}
        or settings.auth_mode not in {"token", "cloudflare-access"}
    ):
        raise ValueError("Configure the manager's fixed origin and auth mode.")
    owner, runner = tokens or (
        token_file(settings.token_file),
        token_file(settings.runner_token_file),
    )
    if not all(
        type(x) is str and re.fullmatch(r"[a-f0-9]{64}", x)
        for x in (owner, runner)
    ) or secrets.compare_digest(owner, runner):
        raise ValueError(
            "Configure distinct 256-bit manager and runner tokens."
        )
    if settings.auth_mode == "cloudflare-access":
        verifier = verifier or AccessVerifier(
            {
                **(settings.access_proxy_settings or {}),
                "PORTAL_ACCESS_TEAM_DOMAIN": settings.access_team_domain,
                "PORTAL_ACCESS_AUD": settings.access_aud,
            }
        )
    journal = journal or Journal(settings.state_dir)
    sessions, attempts = OrderedDict(), deque(maxlen=10)
    app = FastAPI(
        title="Lakehouse PoC Manager",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.manager_journal = journal

    def bearer(request):
        fields = request.headers.getlist("authorization")
        if len(fields) != 1 or not re.fullmatch(
            r"Bearer [a-f0-9]{64}", fields[0]
        ):
            return None
        return fields[0][7:]

    async def identity(request, *, internal=False, optional=False):
        presented = bearer(request)
        if presented and secrets.compare_digest(
            presented, runner if internal else owner
        ):
            return (
                "runner"
                if internal
                else hashlib.sha256(owner.encode()).hexdigest()
            )
        if internal:
            raise HTTPException(401, "RUNNER_AUTH_REQUIRED")
        if len(request.headers.getlist("authorization")) > 0:
            raise HTTPException(401, "AUTH_REQUIRED")
        cookie = request.cookies.get(COOKIE, "")
        digest = (
            hashlib.sha256(cookie.encode()).hexdigest()
            if re.fullmatch(r"[A-Za-z0-9_-]{43}", cookie)
            else ""
        )
        saved = sessions.get(digest)
        if saved and saved["expires"] > clock():
            if settings.auth_mode == "cloudflare-access":
                current = await verifier.verify(request.headers)
                if not secrets.compare_digest(current, saved["actor"]):
                    raise HTTPException(401, "AUTH_REQUIRED")
            return saved["actor"]
        if optional:
            return None
        raise HTTPException(401, "AUTH_REQUIRED")

    def csrf(request):
        origins = request.headers.getlist("origin")
        if len(origins) != 1 or origins[0] != settings.public_origin:
            raise HTTPException(403, "ORIGIN_REQUIRED")

    @app.middleware("http")
    async def boundary(request, call_next):
        try:
            if request.headers.getlist("host") != [parts.netloc]:
                return JSONResponse(
                    {"detail": "HOST_REJECTED"}, 400, headers=SECURITY
                )
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                if not bearer(request):
                    csrf(request)
                body = bytearray()
                maximum = (
                    256 * 1024
                    if request.url.path == "/internal/runner/report"
                    else 16384
                )
                async for block in request.stream():
                    body.extend(block)
                    if len(body) > maximum:
                        raise HTTPException(413, "REQUEST_LIMIT")
                if body:
                    strict_json(body)
                request._body = bytes(body)
            response = await call_next(request)
        except AuthProblem as error:
            response = JSONResponse({"detail": error.code}, error.status)
        except HTTPException as error:
            response = JSONResponse(
                {"detail": error.detail}, error.status_code
            )
        except (StateError, PlanError) as error:
            code = str(error)
            response = JSONResponse(
                {
                    "detail": code
                    if re.fullmatch(r"[A-Z_]{1,64}", code)
                    else "STATE_UNAVAILABLE"
                },
                409,
            )
        except Exception:
            response = JSONResponse({"detail": "STATE_UNAVAILABLE"}, 503)
        response.headers.update(SECURITY)
        return response

    @app.exception_handler(StateError)
    @app.exception_handler(PlanError)
    async def state_failure(request, error):
        code = str(error)
        return JSONResponse(
            {
                "detail": code
                if re.fullmatch(r"[A-Z_]{1,64}", code)
                else "STATE_UNAVAILABLE"
            },
            409,
        )

    @app.exception_handler(AuthProblem)
    async def auth_failure(request, error):
        return JSONResponse({"detail": error.code}, error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_failure(request, error):
        return JSONResponse({"detail": "REQUEST_INVALID"}, 422)

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"ok": True, "scope": "manager_process_only"}

    @app.get("/api/session")
    async def session_status(request: Request):
        actor = await identity(request, optional=True)
        return {
            "authenticated": actor is not None,
            "mode": settings.auth_mode,
            "role": "operator" if actor else None,
            "logout_url": "/cdn-cgi/access/logout"
            if settings.auth_mode == "cloudflare-access"
            else None,
        }

    @app.post("/api/session")
    async def session_login(request: Request):
        csrf(request)
        now = clock()
        while attempts and attempts[0] <= now - 60:
            attempts.popleft()
        if len(attempts) >= 10:
            raise HTTPException(429, "LOGIN_RATE_LIMIT")
        attempts.append(now)
        value = strict_json(await request.body())
        if settings.auth_mode == "cloudflare-access":
            if value != {"method": "cloudflare-access"}:
                raise HTTPException(422, "SSO_REQUIRED")
            actor = await verifier.verify(request.headers)
        else:
            if (
                type(value) is not dict
                or set(value) != {"token"}
                or type(value["token"]) is not str
                or re.fullmatch(r"[a-f0-9]{64}", value["token"]) is None
                or not secrets.compare_digest(value["token"], owner)
            ):
                raise HTTPException(401, "AUTH_REQUIRED")
            actor = hashlib.sha256(owner.encode()).hexdigest()
        for key in list(sessions):
            if sessions[key]["expires"] <= now:
                del sessions[key]
        if len(sessions) >= 128:
            raise HTTPException(503, "SESSION_CAPACITY")
        cookie = secrets.token_urlsafe(32)
        sessions[hashlib.sha256(cookie.encode()).hexdigest()] = {
            "actor": actor,
            "expires": now + SESSION_TTL,
        }
        response = JSONResponse(
            {
                "authenticated": True,
                "mode": settings.auth_mode,
                "role": "operator",
            }
        )
        response.set_cookie(
            COOKIE,
            cookie,
            max_age=SESSION_TTL,
            httponly=True,
            secure=parts.scheme == "https",
            samesite="strict",
            path="/",
        )
        return response

    @app.delete("/api/session")
    async def session_logout(request: Request):
        csrf(request)
        value = request.cookies.get(COOKIE, "")
        sessions.pop(hashlib.sha256(value.encode()).hexdigest(), None)
        response = JSONResponse(
            {
                "authenticated": False,
                "logout_url": "/cdn-cgi/access/logout"
                if settings.auth_mode == "cloudflare-access"
                else None,
            }
        )
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/api/pocs")
    async def pocs(request: Request):
        await identity(request)
        with journal.transaction() as data:
            connected = journal.connected(data)
            report = data["runner"] if connected else None
            active = journal.active(data)
            rows = []
            for poc_id in sorted(POC_IDS):
                inventory = report["inventories"][poc_id] if report else []
                states = {row["state"] for row in inventory}
                state = (
                    next(iter(states))
                    if len(states) == 1
                    else "partial"
                    if states
                    else "unknown"
                )
                reasons = [] if connected else ["RUNNER_OFFLINE"]
                if active:
                    reasons.append(
                        "OPERATION_UNKNOWN"
                        if active["status"] == "unknown"
                        else "OPERATION_BUSY"
                    )
                if report and not report["workloads_idle"][poc_id]:
                    reasons.append(
                        "INTERRUPTION_ACK_REQUIRED"
                        if report["approvals"][poc_id]["allow_interruption"]
                        else "WORKLOAD_BUSY"
                    )
                if report and not report["admission_ready"]:
                    reasons.append("MEMORY_BUDGET")
                rows.append(
                    {
                        "poc_id": poc_id,
                        "title": "基盤PoC"
                        if poc_id == "foundation"
                        else "将来構想PoC",
                        "state": state,
                        "services": [
                            {"service": r["service"], "state": r["state"]}
                            for r in inventory
                        ],
                        "blocked_reasons": reasons,
                        "observed_at": report["observed_at"]
                        if report
                        else None,
                        "operation": operation_view(active)
                        if active and active["poc_id"] == poc_id
                        else None,
                    }
                )
            return {
                "schema_version": 1,
                "pocs": rows,
                "runner_connected": connected,
            }

    @app.post("/api/pocs/{poc_id}/plans")
    async def plans(poc_id: str, body: Action, request: Request):
        actor = await identity(request)
        if poc_id not in POC_IDS:
            raise HTTPException(404, "POC_UNKNOWN")
        with journal.transaction() as data:
            return journal.create_plan(data, poc_id, body.action, actor)

    @app.post("/api/operations", status_code=202)
    async def submit(body: Submission, request: Request):
        actor = await identity(request)
        with journal.transaction() as data:
            return {
                "operation": journal.submit(
                    data,
                    body.plan_id,
                    body.confirmation,
                    body.idempotency_key,
                    actor,
                    body.interruption_ack,
                )
            }

    @app.get("/api/operations")
    async def history(request: Request, limit: int = 10):
        await identity(request)
        if not 1 <= limit <= 30:
            raise HTTPException(422, "LIMIT_INVALID")
        with journal.transaction() as data:
            rows = sorted(
                data["operations"].values(),
                key=lambda r: r["created_at"],
                reverse=True,
            )
            return {"operations": [operation_view(r) for r in rows[:limit]]}

    @app.get("/api/operations/by-key/{key}")
    async def by_key(key: str, request: Request):
        actor = await identity(request)
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
            raise HTTPException(422, "REQUEST_INVALID")
        key_hash = hashlib.sha256((actor + "\0" + key).encode()).hexdigest()
        with journal.transaction() as data:
            identifier = data["keys"].get(key_hash)
            if identifier is None:
                raise HTTPException(404, "OPERATION_NOT_CONFIRMED")
            return {
                "operation": operation_view(data["operations"][identifier])
            }

    @app.get("/api/operations/{identifier}")
    async def operation(identifier: str, request: Request):
        await identity(request)
        with journal.transaction() as data:
            row = data["operations"].get(identifier)
            if not row:
                raise HTTPException(404, "OPERATION_UNKNOWN")
            return {"operation": operation_view(row)}

    @app.post("/internal/runner/report", include_in_schema=False)
    async def report(request: Request):
        await identity(request, internal=True)
        value = strict_json(await request.body())
        with journal.transaction() as data:
            journal.report(data, value)
        return {"accepted": True}

    @app.post("/internal/runner/claim", include_in_schema=False)
    async def claim(body: Claim, request: Request):
        await identity(request, internal=True)
        with journal.transaction() as data:
            return {
                "operation": journal.claim(
                    data, body.runner_id, body.manifest_digest
                )
            }

    @app.post("/internal/runner/result", include_in_schema=False)
    async def result(body: Result, request: Request):
        await identity(request, internal=True)
        value = body.model_dump()
        runner_id = value.pop("runner_id")
        with journal.transaction() as data:
            return {"operation": journal.finish(data, runner_id, value)}

    @app.post("/internal/runner/heartbeat", include_in_schema=False)
    async def heartbeat(body: Heartbeat, request: Request):
        await identity(request, internal=True)
        with journal.transaction() as data:
            journal.heartbeat(
                data, body.runner_id, body.operation_id, body.lease_id
            )
        return {"accepted": True}

    @app.post("/internal/runner/reconcile", include_in_schema=False)
    async def reconcile(body: Reconcile, request: Request):
        await identity(request, internal=True)
        with journal.transaction() as data:
            return {
                "operation": journal.reconcile(
                    data, body.runner_id, body.operation_id, body.lease_id
                )
            }

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi(request: Request):
        await identity(request)
        return app.openapi()

    @app.get("/docs", include_in_schema=False)
    async def docs(request: Request):
        await identity(request)
        # Parent image serves pinned local Swagger assets; never use a CDN.
        from fastapi.openapi.docs import get_swagger_ui_html

        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=app.title,
            swagger_js_url="/swagger/swagger-ui-bundle.js",
            swagger_css_url="/swagger/swagger-ui.css",
            swagger_favicon_url="",
        )

    from .web import attach_web

    async def authenticated(request):
        return await identity(request)

    attach_web(app, authenticated)
    return app
