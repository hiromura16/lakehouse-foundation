"""Authenticated GUI/control API with source-only generation and Swagger."""

import importlib.util
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Annotated, Literal

import httpx
from control_settings import SettingsError, SettingsStore
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from job_manager import JobConflict, JobManager, OperationFailure
from pydantic import BaseModel, ConfigDict, Field, model_validator
from service import generate_batch

ROOT = Path(__file__).resolve().parents[2]
SECURITY = HTTPBearer(auto_error=False)
KNOWN_HEALTH_URLS = {
    "worker": "http://worker:8000/health",
    "prometheus": "http://prometheus:9090/-/ready",
    "grafana": "http://grafana:3000/api/health",
    "n8n": "http://n8n:5678/healthz",
    "airflow": "http://airflow:8080/api/v2/monitor/health",
    "cdc-connect": "http://cdc-connect:8083/",
    "cdc-ui": "http://cdc-ui:8080/actuator/health",
}


class SettingsPatch(BaseModel):
    """Accept named settings only; empty secret values retain stored values."""

    model_config = ConfigDict(extra="forbid", strict=True)
    values: dict[str, str] = Field(max_length=128)


class SourceJob(BaseModel):
    """Bound source size, SQL consent, and finite streaming duration."""

    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["batch", "stream"] = "batch"
    batches: int = Field(default=1, ge=1, le=100)
    interval_seconds: float = Field(default=0, ge=0, le=60)
    seed: int = Field(default=42, ge=0, le=2147483647)
    orders: int = Field(default=20, ge=1, le=1000)
    days: int = Field(default=7, ge=1, le=366)
    scenario: Literal["clean", "edge"] = "edge"
    destination: Literal["landing", "sqlserver", "both"] = "landing"
    images: bool = False
    allow_sql_write: bool = False

    @model_validator(mode="after")
    def validate_job(self):
        """Require separate user intent for SQL writes and repeated batches."""
        if self.mode == "batch" and self.batches != 1:
            raise ValueError("Batch mode runs once.")
        if (self.batches - 1) * self.interval_seconds > 600:
            raise ValueError("Scheduled waiting exceeds ten minutes.")
        if self.destination != "landing" and not self.allow_sql_write:
            raise ValueError("SQL destination requires explicit consent.")
        if self.destination == "sqlserver" and self.images:
            raise ValueError("Images require a landing destination.")
        return self


class CdcConfirmation(BaseModel):
    """Require explicit user intent without accepting SQL or TLS overrides."""

    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: bool

    @model_validator(mode="after")
    def require_confirmation(self):
        """Reject false and preserve strict JSON-boolean validation."""
        if not self.confirm:
            raise ValueError("Explicit confirmation is required.")
        return self


class SettingsView(BaseModel):
    """Describe saved values without ever returning credentials."""

    values: dict[str, str]
    secret_keys: list[str]
    configured_secrets: list[str]
    pending_restart: bool
    pending_mount_change: bool


class SettingsSaved(SettingsView):
    """Distinguish a file save from a deployment or mount change."""

    saved: bool
    applied: bool


class AcceptedJob(BaseModel):
    """Identify an accepted operation that clients can poll."""

    id: str
    status: Literal["queued"]
    kind: Literal["source", "pipeline", "cdc-source"] = "source"
    pending_restart: bool = False


class JobView(BaseModel):
    """Expose bounded progress and source-only result metadata."""

    id: str
    kind: Literal["source", "pipeline", "cdc-source"]
    pipeline_action: str | None = None
    cdc_action: Literal["insert", "update", "delete"] | None = None
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    completed: int
    batches: int
    pending_restart: bool = False
    results: list[dict]
    error: dict[str, str] | None


def reject(status: int, code: str, message: str):
    """Raise only application-owned error messages and stable codes."""
    raise HTTPException(status, detail={"code": code, "message": message})


def export_ai_context(datasets: list[str], limit: int) -> dict:
    """Load the fixed read-only adapter without exposing source credentials."""
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    specification = importlib.util.spec_from_file_location(
        "api_ai_context", ROOT / "scripts/ai_context.py"
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.export_context(
        {"BIND_IP": "trino", "TRINO_PORT": "8080"}, datasets, limit
    )


def apply_cdc_demo(values: dict[str, str], action: str) -> dict:
    """Invoke fixed DML with ownership checks and normal TLS validation."""
    specification = importlib.util.spec_from_file_location(
        "api_owned_cdc_demo", ROOT / "scripts/cdc-demo.py"
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    # The GUI cannot opt out of CA or hostname verification.
    return module.apply_scenario(values, action, trust_local_test=False)


def create_app(
    settings_file: Path | None = None,
    token: str | None = None,
    landing: Path | None = None,
    generator=generate_batch,
    ai_exporter=export_ai_context,
    cdc_runner=apply_cdc_demo,
) -> FastAPI:
    """Create one process-local controller with no Docker/root capability."""
    store = SettingsStore(
        settings_file
        or Path(os.environ.get("CONTROL_SETTINGS_FILE", "/private/site.env"))
    )
    active_token = (
        token if token is not None else os.environ.get("CONTROL_API_TOKEN", "")
    )
    jobs = JobManager(
        landing or Path(os.environ.get("DEMO_LANDING_DIR", "/landing")),
        generator,
        store.applied,
    )
    application = FastAPI(
        title="Lakehouse 操作API",
        description=(
            "設定の保存と反映を区別します。素材投入は後続処理を起動しません。"
        ),
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
    )
    application.mount(
        "/static",
        StaticFiles(
            directory=Path(__file__).parent / "static", check_dir=False
        ),
        name="swagger-assets",
    )

    @application.get("/docs", include_in_schema=False)
    def swagger():
        """Serve version-pinned assets without browser-side CDN access."""
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title="Lakehouse 操作API",
            swagger_js_url="/static/swagger-ui-bundle.js",
            swagger_css_url="/static/swagger-ui.css",
            swagger_favicon_url="/static/favicon-32x32.png",
            swagger_ui_parameters={
                "validatorUrl": None,
                "persistAuthorization": False,
            },
        )

    application.state.store = store
    application.state.jobs = jobs

    def authorize(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(SECURITY)
        ],
    ) -> None:
        """Keep tokens server-side and refuse unconfigured authentication."""
        valid = bool(active_token and credentials)
        if valid:
            try:
                valid = secrets.compare_digest(
                    credentials.credentials, active_token
                )
            except TypeError:
                valid = False
        if not valid:
            reject(401, "AUTH_REQUIRED", "操作APIの認証が必要です。")

    protected = [Depends(authorize)]

    @application.middleware("http")
    async def private_error_boundary(request, call_next):
        """Keep unexpected exception details out of server tracebacks."""
        try:
            return await call_next(request)
        except Exception:
            return JSONResponse(
                status_code=500,
                content={
                    "code": "INTERNAL_ERROR",
                    "message": (
                        "操作を完了できませんでした。状態を確認してください。"
                    ),
                },
            )

    @application.exception_handler(RequestValidationError)
    async def invalid_request(_, __):
        """Pydantic errors must not echo a submitted password or body."""
        return JSONResponse(
            status_code=422,
            content={
                "code": "INVALID_REQUEST",
                "message": "入力項目・範囲を確認してください。",
            },
        )

    @application.exception_handler(HTTPException)
    async def controlled_error(_, error):
        """Keep error responses uniform for Streamlit and Swagger clients."""
        detail = (
            error.detail
            if isinstance(error.detail, dict)
            else {
                "code": "REQUEST_FAILED",
                "message": "リクエストを確認してください。",
            }
        )
        return JSONResponse(status_code=error.status_code, content=detail)

    @application.exception_handler(Exception)
    async def unexpected_error(_, __):
        """Do not expose driver messages, endpoints, SQL, or credentials."""
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": (
                    "操作を完了できませんでした。"
                    "設定と状態を確認してください。"
                ),
            },
        )

    @application.get("/health", tags=["状態"])
    def health():
        """Readiness contains no connection details or secret flags."""
        ready = bool(active_token and store.applied)
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ok" if ready else "not_configured"},
        )

    @application.get(
        "/settings",
        response_model=SettingsView,
        dependencies=protected,
        tags=["設定"],
    )
    def get_settings():
        """Read non-secret settings and configured-secret flags."""
        try:
            return store.public()
        except (OSError, ValueError):
            reject(
                503,
                "SETTINGS_UNAVAILABLE",
                "非公開設定の場所・権限・形式を確認してください。",
            )

    @application.patch(
        "/settings",
        response_model=SettingsSaved,
        dependencies=protected,
        tags=["設定"],
    )
    def patch_settings(patch: SettingsPatch):
        """Save without changing mounts, Docker services, or active tokens."""
        if not jobs.operation.acquire(blocking=False):
            reject(
                409,
                "OPERATION_RUNNING",
                "実行中の処理が終わってから保存してください。",
            )
        try:
            return store.save(patch.values)
        except (OSError, ValueError):
            reject(
                422,
                "INVALID_SETTINGS",
                "設定項目・形式・秘密ファイルの権限を確認してください。",
            )
        finally:
            jobs.operation.release()

    @application.get("/status", dependencies=protected, tags=["状態"])
    def status():
        """Probe known internal services only; callers cannot select a URL."""
        services = {}
        with httpx.Client(
            timeout=1, trust_env=False, follow_redirects=False
        ) as client:
            for name, url in KNOWN_HEALTH_URLS.items():
                try:
                    response = client.get(url)
                    services[name] = {"healthy": response.status_code == 200}
                    if name == "worker":
                        try:
                            payload = response.json()
                        except ValueError:
                            payload = None
                        if (
                            isinstance(payload, dict)
                            and type(payload.get("busy")) is bool
                        ):
                            services[name]["busy"] = payload["busy"]
                except httpx.HTTPError:
                    services[name] = {"healthy": False}
        return {
            "services": services,
            "source_job_running": jobs.operation.locked(),
            "pending_restart": store.public()["pending_restart"],
        }

    @application.post(
        "/jobs",
        status_code=202,
        response_model=AcceptedJob,
        dependencies=protected,
        tags=["素材投入"],
    )
    def create_job(options: SourceJob):
        """Generate sources without ingestion, medallion, or workflow calls."""
        if not store.applied:
            reject(
                503, "SETTINGS_UNAVAILABLE", "非公開設定を確認してください。"
            )
        try:
            values = options.model_dump()
            values["pending_restart"] = store.public()["pending_restart"]
            source_values = (
                store.source_settings()
                if options.destination != "landing"
                else None
            )
            return jobs.submit(values, settings=source_values)
        except SettingsError:
            reject(
                422,
                "DEMO_CA_UNAVAILABLE",
                "検証DBのCAを専用private設定ディレクトリ内で確認してください。",
            )
        except JobConflict:
            reject(
                409,
                "OPERATION_RUNNING",
                "別の処理が実行中です。完了を待ってください。",
            )

    @application.post(
        "/cdc/demo/{action}",
        status_code=202,
        response_model=AcceptedJob,
        dependencies=protected,
        tags=["CDC専用素材"],
    )
    def cdc_demo(
        action: Literal["insert", "update", "delete"],
        confirmation: CdcConfirmation,
    ):
        """Change fixed records only in a previously prepared owned demo DB."""
        del confirmation  # Request validation already requires explicit true.
        try:
            values = store.source_settings()
            for key in ("CDC_SQLSERVER_DATABASE", "CDC_DEMO_OWNERSHIP_TOKEN"):
                values[key] = store.applied.get(key, "")
            database = values["CDC_SQLSERVER_DATABASE"]
            if (
                not re.fullmatch(r"lh_cdc_demo_[0-9a-f]{16}", database)
                or database.casefold()
                == values.get("SOURCE_SQLSERVER_DATABASE", "").casefold()
                or not re.fullmatch(
                    r"[0-9a-f]{64}", values["CDC_DEMO_OWNERSHIP_TOKEN"]
                )
                or not all(
                    values.get("DEMO_SQLSERVER_" + key)
                    for key in ("HOST", "USER", "PASSWORD")
                )
            ):
                reject(
                    422,
                    "CDC_DEMO_NOT_PREPARED",
                    "専用検証DBの準備・所有権鍵・DEMO認証を確認してください。",
                )
            pending = store.public()["pending_restart"]

            def run_cdc(selected: str) -> dict:
                """Return only fixed public counters, never driver details."""
                result = cdc_runner(values, selected)
                if (
                    not isinstance(result, dict)
                    or result.get("action") != selected
                    or result.get("source_only") is not True
                    or type(result.get("changed_rows")) is not int
                    or result["changed_rows"] != 6
                ):
                    raise OperationFailure(
                        "CDC_DEMO_FAILED",
                        "専用検証DBの結果を確認してください。",
                    )
                return {
                    "action": selected,
                    "changed_rows": 6,
                    "source_only": True,
                }

            return jobs.submit_cdc(action, run_cdc, pending_restart=pending)
        except SettingsError:
            reject(
                422,
                "DEMO_CA_UNAVAILABLE",
                "検証DBのCAを専用private設定ディレクトリ内で確認してください。",
            )
        except JobConflict:
            reject(409, "OPERATION_RUNNING", "別の処理が実行中です。")

    @application.get(
        "/jobs/{identifier}",
        response_model=JobView,
        dependencies=protected,
        tags=["素材投入"],
    )
    def get_job(identifier: str):
        """Poll bounded source metadata without connection details."""
        try:
            return jobs.get(identifier)
        except KeyError:
            reject(404, "JOB_NOT_FOUND", "指定した投入処理が見つかりません。")

    @application.post(
        "/jobs/{identifier}/cancel",
        response_model=JobView,
        dependencies=protected,
        tags=["素材投入"],
    )
    def cancel_job(identifier: str):
        """Cancel between batches without undoing already published sources."""
        try:
            return jobs.cancel(identifier)
        except KeyError:
            reject(404, "JOB_NOT_FOUND", "指定した投入処理が見つかりません。")
        except JobConflict:
            if jobs.get(identifier)["kind"] == "cdc-source":
                reject(
                    409,
                    "CDC_DEMO_RUNNING",
                    "開始済みの専用DB変更は完了を待ってください。",
                )
            reject(
                409,
                "PIPELINE_RUNNING",
                "開始済みの後続処理は完了を待ってください。",
            )

    def run_pipeline(action: str) -> dict:
        """Forward one fixed action and retain only bounded result counters."""
        try:
            with httpx.Client(
                timeout=240, trust_env=False, follow_redirects=False
            ) as client:
                response = client.post(
                    "http://worker:8000/run",
                    json={"action": action},
                    headers={
                        "Authorization": "Bearer "
                        + store.applied.get("WORKER_TOKEN", "")
                    },
                )
            if response.status_code != 200:
                raise OperationFailure(
                    "WORKER_REJECTED",
                    "workerの状態と実行中の処理を確認してください。",
                )
            payload = response.json()
            if not isinstance(payload, dict) or "error" in payload:
                raise OperationFailure(
                    "WORKER_FAILED", "workerの処理結果を確認してください。"
                )
            result = {}
            for key in ("imported", "skipped", "sql_statements"):
                if type(payload.get(key)) is int:
                    result[key] = payload[key]
            if payload.get("ok") is True:
                result["ok"] = True
            if isinstance(payload.get("checks"), list):
                result["checks_completed"] = len(payload["checks"])
            return result
        except httpx.TimeoutException:
            # A lost response does not mean that the worker stopped its job.
            raise OperationFailure(
                "WORKER_RESULT_UNKNOWN",
                "処理が継続中の可能性があります。workerの実行中表示と結果を"
                "確認してから再実行してください。",
            ) from None
        except (httpx.HTTPError, ValueError):
            raise OperationFailure(
                "WORKER_UNAVAILABLE", "workerとの接続状態を確認してください。"
            ) from None

    @application.post(
        "/pipeline/{action}",
        status_code=202,
        response_model=AcceptedJob,
        dependencies=protected,
        tags=["後続処理"],
    )
    def pipeline(action: Literal["ingest", "medallion", "verify"]):
        """Check worker state before queuing one fixed explicit action."""
        if jobs.operation.locked():
            reject(409, "OPERATION_RUNNING", "別の処理が実行中です。")
        try:
            with httpx.Client(
                timeout=3, trust_env=False, follow_redirects=False
            ) as client:
                response = client.get(KNOWN_HEALTH_URLS["worker"])
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid worker health response.")
        except (httpx.HTTPError, ValueError):
            reject(
                503,
                "WORKER_UNAVAILABLE",
                "workerの状態を確認できません。接続と状態を確認してください。",
            )
        # Older workers omit busy; their existing /run lock remains effective.
        if payload.get("busy") is True or payload.get("blocked") is True:
            reject(
                409,
                "WORKER_NOT_IDLE",
                "workerが実行中または確認待ちです。状態と結果を確認して"
                "から再実行してください。",
            )
        if response.status_code != 200:
            reject(
                503,
                "WORKER_UNAVAILABLE",
                "workerの起動状態を確認してください。",
            )
        try:
            return jobs.submit_pipeline(action, run_pipeline)
        except JobConflict:
            reject(409, "OPERATION_RUNNING", "別の処理が実行中です。")

    @application.get(
        "/ai/context", dependencies=protected, tags=["AI向け参照"]
    )
    def ai_context(
        dataset: Literal["all", "production", "sales"] = "all",
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        """Export snapshot-pinned Gold data without executing model SQL."""
        datasets = ["production", "sales"] if dataset == "all" else [dataset]
        try:
            return json.loads(
                json.dumps(ai_exporter(datasets, limit), default=str)
            )
        except Exception:
            reject(
                409,
                "GOLD_UNAVAILABLE",
                "メダリオンを実行後に再試行し、Goldの状態を確認してください。",
            )

    return application


app = create_app()
