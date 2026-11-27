"""外部入力の処理・状態・参照とprivate設定を認証付きで提供する。

202は後続処理の受付であり、完了はジョブIDで確認する。このAPIは
合成データを生成・投入せず、設定も保存するだけで配置へ反映しない。
"""

import importlib.util
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Annotated, Literal

import httpx
from control_settings import SettingsStore
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from job_manager import JobConflict, JobManager, OperationFailure
from poc_api import portal_router
from pydantic import BaseModel, ConfigDict, Field
from sqlsource_api import sqlsource_router
from sqlsource_observer import observation_router

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
    """設定名と単一行値の変更要求。秘密欄が空なら既存値を保持する。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    values: dict[str, str] = Field(max_length=128)


class SettingsView(BaseModel):
    """非秘密設定と秘密値の設定済みフラグ、未反映状態だけを返す型。"""

    values: dict[str, str]
    secret_keys: list[str]
    configured_secrets: list[str]
    pending_restart: bool
    pending_mount_change: bool


class SettingsSaved(SettingsView):
    """privateファイルへの保存と、稼働環境への適用を区別する応答型。"""

    saved: bool
    applied: bool


class AcceptedJob(BaseModel):
    """受付済みのjob IDを返す型。queuedは処理完了を意味しない。"""

    id: str
    status: Literal["queued"]
    kind: Literal["pipeline"] = "pipeline"
    pending_restart: bool = False


class JobView(BaseModel):
    """有限jobの進捗・最終状態と、許可した結果情報を返す型。履歴は非永続。"""

    id: str
    kind: Literal["pipeline"]
    pipeline_action: str | None = None
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    completed: int
    batches: int
    pending_restart: bool = False
    results: list[dict]
    error: dict[str, str] | None


def reject(status: int, code: str, message: str):
    """利用者へ返してよい固定code/messageだけをHTTP例外として送出する。"""
    raise HTTPException(status, detail={"code": code, "message": message})


def export_ai_context(datasets: list[str], limit: int) -> dict:
    """固定Goldのsnapshot付きcontextを内部Trinoから読み、辞書で返す。

    入力は許可済みdatasetと各表の行数上限。source資格情報を公開せず、
    モデル実行や外部AI送信も行わない。複数表の同時snapshotは保証しない。
    """
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


def create_app(
    settings_file: Path | None = None,
    token: str | None = None,
    landing: Path | None = None,
    ai_exporter=export_ai_context,
) -> FastAPI:
    """private設定・認証・単一JobManagerを持つFastAPIアプリを組み立てる。

    settings_file等の引数は配置値や試験用I/Oを注入する入口。起動時設定と
    現在の保存値を分け、routeは型/認証→状態確認→固定操作の順に読む。
    pipeline/CDC受付の排他は共有するが、複数APIプロセスを跨がない。
    Docker socket・sudo・自動Deployの能力は持たせない。
    """
    # 起動時の適用値を保持する。Save後の値をその場で実行設定へ混ぜない。
    store = SettingsStore(
        settings_file
        or Path(os.environ.get("CONTROL_SETTINGS_FILE", "/private/site.env"))
    )
    active_token = (
        token if token is not None else os.environ.get("CONTROL_API_TOKEN", "")
    )
    jobs = JobManager(
        landing or Path(os.environ.get("SQL_BATCH_EXCHANGE_DIR", "/landing")),
    )
    application = FastAPI(
        title="Lakehouse 操作API",
        description=(
            "外部入力の処理・監視を提供し、設定の保存と反映を区別します。"
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
        """固定版Swaggerを内部配信し、ブラウザーの認証保持を無効にする。"""
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
        """起動時の共有Bearer tokenと照合し、不在・不一致を401で拒否する。

        保存値を変えても稼働中tokenは切り替わらない。資格情報を要求へ返さず、
        比較できない文字列も認証成功として扱わない。
        """
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
        """未処理例外の本文・接続情報をHTTP応答へ出さず、固定500へ変換する。"""
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
        """入力したpassword等をPydanticの詳細へ反射せず、固定422を返す。"""
        return JSONResponse(
            status_code=422,
            content={
                "code": "INVALID_REQUEST",
                "message": "入力項目・範囲を確認してください。",
            },
        )

    @application.exception_handler(HTTPException)
    async def controlled_error(_, error):
        """アプリが用意した安全なcode/messageを、GUIとSwagger共通のJSONへ揃える。"""
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
        """driver例外・SQL・資格情報を出さず、想定外の失敗を固定500へ変換する。"""
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
        """tokenと起動時設定の準備だけを、秘密なしの200/503で返す。

        worker・Trino・会社DBの疎通や、業務結果の正しさはこの応答に含まない。
        """
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
        """保存済みの非秘密項目と設定済みフラグを読み、秘密値は返さない。"""
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
        """他jobの実行中を避けて許可項目を保存し、反映待ちを返す。

        JobManagerと同じ排他を使い、処理途中の設定変更を避ける。保存先は
        privateファイルだけで、mount・Docker・起動時tokenは変更しない。
        """
        # CDC/pipeline受付と同じlockを使い、実行中の保存を拒否する。
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
        """既知の内部healthだけを照合し、到達・busy・反映待ちを返す。

        任意URLや保存済みproxyを使った診断にはしない。未選択profileの未到達と
        障害は別途判断し、API受付のlockとworkerのbusyも区別する。
        """
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
            "operation_running": jobs.operation.locked(),
            "pending_restart": store.public()["pending_restart"],
        }

    @application.get(
        "/jobs/{identifier}",
        response_model=JobView,
        dependencies=protected,
        tags=["後続処理"],
    )
    def get_job(identifier: str):
        """job IDで進捗・結果を返す。404を未実行の証明にはしない。"""
        try:
            return jobs.get(identifier)
        except KeyError:
            reject(404, "JOB_NOT_FOUND", "指定した処理が見つかりません。")

    def run_pipeline(action: str) -> dict:
        """固定actionをworkerへ依頼し、完了応答の許可した件数だけを返す。

        この関数はjob thread内で待つ。応答timeoutでもworkerは継続し得る。
        結果不明を専用codeにし、自動で再送・取消・成功扱いへ変換しない。
        """
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
            # 下流の新しいfieldや例外本文を、自動でAPI応答へ追加しない。
            result = {}
            for key in (
                "imported",
                "skipped",
                "sql_statements",
                "rejected_batches",
            ):
                if type(payload.get(key)) is int:
                    result[key] = payload[key]
            samples = payload.get("batch_rejections")
            if isinstance(samples, list):
                result["batch_rejections"] = [
                    item
                    for item in samples[:20]
                    if isinstance(item, dict)
                    and set(item) == {"id", "reason"}
                    and isinstance(item["id"], str)
                    and re.fullmatch(r"[a-f0-9]{16}", item["id"])
                    and isinstance(item["reason"], str)
                    and item["reason"]
                    in {
                        "manifest_invalid",
                        "artifact_unavailable",
                        "artifact_mismatch",
                        "artifact_format",
                        "batch_limit",
                    }
                ]
            if payload.get("ok") is True:
                result["ok"] = True
            if isinstance(payload.get("checks"), list):
                result["checks_completed"] = len(payload["checks"])
            return result
        except httpx.TimeoutException:
            # 待機切れはworkerの停止ではない。新しい実行を自動で重ねない。
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
        """workerの準備状態を確かめ、固定pipeline actionを202で受付する。

        API内の操作lockとworkerのbusy/blockedを別々に確認する。health確認と
        実行開始は別時点なので、最終的な同時実行拒否はworker側のlockが担う。
        """
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
        # healthは予備確認。busyのない旧workerでも/runのlockが最終排他する。
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
        """許可したGoldのsnapshot固定contextを同期取得して返す。

        limitは各表1〜500行で、全件合計の保証ではない。モデルSQLや推論を実行せず、
        未準備・取得失敗は生例外を返さず409で案内する。
        """
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

    application.include_router(
        portal_router(store, jobs, authorize, ai_exporter)
    )
    application.include_router(sqlsource_router(store, jobs, authorize))
    application.include_router(observation_router(store, jobs, authorize))
    return application


app = create_app()
