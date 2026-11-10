"""設定保存・素材生成・明示pipeline・固定参照を認証付きAPIで提供する。

読む順序は要求/応答型 → create_app → 各route → run_pipeline。
POSTの202はJobManagerへの受付で、完了はjob IDをGETして確認する。
素材生成はsource-onlyで、後続処理を自動開始しない。設定の保存も
privateファイルの更新までとし、container反映やDocker操作は行わない。
共有token・job履歴・排他はこのAPIプロセス内の構成として扱う。
"""

import importlib.util
import json
import os
import re
import secrets
import sys
from functools import lru_cache
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
from poc_api import portal_router
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


@lru_cache(maxsize=1)
def source_scenario_engine():
    """CLIと共通の固定異常catalog・入力検査を初回だけ読み込んで返す。"""
    specification = importlib.util.spec_from_file_location(
        "api_source_scenarios", ROOT / "scripts/source_scenarios.py"
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class ScenarioDefinition(BaseModel):
    """1つの選択可能な異常と、対象・同意条件・下流で期待する影響を表す。"""

    id: str
    label: str
    description: str
    category: str
    targets: list[str]
    supported_destinations: list[Literal["landing", "sqlserver", "both"]]
    expected_effect: str
    requires_images: bool = False
    requires_file_consent: bool = False


class ScenarioPreset(BaseModel):
    """利用者が編集できる初期選択として、base品質と異常ID集合を渡す。"""

    id: str
    label: str
    base_scenario: Literal["clean", "edge"]
    anomalies: list[str] = Field(max_length=32)


class ScenarioCatalog(BaseModel):
    """GUI/API共通の異常・preset・競合条件を、固定version付きで返す。"""

    version: Literal[1]
    scenarios: list[ScenarioDefinition]
    presets: list[ScenarioPreset]
    conflicts: list[list[str]]


class SettingsPatch(BaseModel):
    """設定名と単一行値の変更要求。秘密欄が空なら既存値を保持する。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    values: dict[str, str] = Field(max_length=128)


class SourceJob(BaseModel):
    """素材生成の有限batch/stream、件数、出力先、同意を表す要求型。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["batch", "stream"] = "batch"
    batches: int = Field(default=1, ge=1, le=100)
    interval_seconds: float = Field(default=0, ge=0, le=60)
    seed: int = Field(default=42, ge=0, le=2147483647)
    orders: int = Field(default=20, ge=1, le=1000)
    days: int = Field(default=7, ge=1, le=366)
    scenario: Literal["clean", "edge"] = "edge"
    anomalies: list[str] = Field(default_factory=list, max_length=32)
    anomaly_rate: int = Field(default=10, ge=1, le=100)
    allow_file_faults: bool = False
    destination: Literal["landing", "sqlserver", "both"] = "landing"
    images: bool = False
    allow_sql_write: bool = False

    @model_validator(mode="after")
    def validate_job(self):
        """型検査後に、回数・待機合計・SQL同意・異常の組合せを検査する。

        streamも有限回で、待機合計は実処理時間を含まない。異常選択はCLIと
        共通の検査へ委ね、この段階ではfile作成やDB接続を行わない。
        """
        if self.mode == "batch" and self.batches != 1:
            raise ValueError("Batch mode runs once.")
        if (self.batches - 1) * self.interval_seconds > 600:
            raise ValueError("Scheduled waiting exceeds ten minutes.")
        if self.destination != "landing" and not self.allow_sql_write:
            raise ValueError("SQL destination requires explicit consent.")
        if self.destination == "sqlserver" and self.images:
            raise ValueError("Images require a landing destination.")
        self.anomalies = list(
            source_scenario_engine().validate_selection(
                anomalies=self.anomalies,
                destination=self.destination,
                images=self.images,
                allow_file_faults=self.allow_file_faults,
                anomaly_rate=self.anomaly_rate,
            )
        )
        return self


class CdcConfirmation(BaseModel):
    """任意SQLやTLS回避設定を受け付けず、固定CDC試験DMLへの同意を表す。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: bool

    @model_validator(mode="after")
    def require_confirmation(self):
        """strictなJSON booleanのtrueだけを同意として受け付ける。"""
        if not self.confirm:
            raise ValueError("Explicit confirmation is required.")
        return self


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
    kind: Literal["source", "pipeline", "cdc-source"] = "source"
    pending_restart: bool = False


class JobView(BaseModel):
    """有限jobの進捗・最終状態と、許可した結果情報を返す型。履歴は非永続。"""

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


def apply_cdc_demo(values: dict[str, str], action: str) -> dict:
    """所有確認済み専用DBへの固定DMLを、共通helperへ委ねて返す。

    SQL側のcommitはhelperが担当する。GUIからCA/hostname検証を無効化する
    選択は渡さず、後続CDCのIceberg到着完了とも区別する。
    """
    specification = importlib.util.spec_from_file_location(
        "api_owned_cdc_demo", ROOT / "scripts/cdc-demo.py"
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    # GUI/APIの要求から、CAやhostname検証の回避を指定できないようにする。
    return module.apply_scenario(values, action, trust_local_test=False)


def create_app(
    settings_file: Path | None = None,
    token: str | None = None,
    landing: Path | None = None,
    generator=generate_batch,
    ai_exporter=export_ai_context,
    cdc_runner=apply_cdc_demo,
) -> FastAPI:
    """private設定・認証・単一JobManagerを持つFastAPIアプリを組み立てる。

    settings_file等の引数は配置値や試験用I/Oを注入する入口。起動時設定と
    現在の保存値を分け、routeは型/認証→状態確認→固定操作の順に読む。
    source/pipeline/CDC受付の排他は共有するが、複数APIプロセスを跨がない。
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
        "/scenarios",
        response_model=ScenarioCatalog,
        dependencies=protected,
        tags=["素材投入"],
    )
    def get_scenarios():
        """固定の異常選択・追加同意・競合集合を返し、生成処理は開始しない。"""
        engine = source_scenario_engine()
        return {
            "version": 1,
            "scenarios": engine.catalog(),
            "presets": engine.presets(),
            "conflicts": [sorted(group) for group in engine.CONFLICTS],
        }

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

        JobManagerと同じ排他を使い、投入途中の設定変更を避ける。保存先は
        privateファイルだけで、mount・Docker・起動時tokenは変更しない。
        """
        # 素材/CDC/pipeline受付と同じlockを使い、実行中の保存を拒否する。
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
        """素材生成を非同期受付し、完了照会用のjob IDを202で返す。

        SQL用には適用済みの専用設定を選ぶ。受付後のfile公開/DB commitは
        JobManager側で進み、ingest・medallion・workflowは自動実行しない。
        """
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
            # ここで返すのは受付IDだけ。完了状態はGETで照会し、再POSTしない。
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
        """所有権付き専用DBの固定I/U/Dだけを非同期受付する。

        DB識別子・所有権token・専用DEMO認証を確認し、会社SOURCE DBを除外する。
        202はDML commitでもCDC到着でもない。実行結果はjob IDで確認する。
        """
        del confirmation  # 要求型の検査で、既に明示的なtrueを必須にしている。
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
                """実行結果を固定6行・source_onlyへ照合し、接続情報を含まない結果だけ返す。"""
                # helper結果の内部情報を返さず、固定の結果契約だけを採用する。
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
        """job IDで進捗・結果を返す。404を未実行の証明にはしない。"""
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
        """未開始処理またはsourceのbatch境界で取消を要求し、現在状態を返す。

        完了済み素材は残る。開始済みCDC transactionやpipelineは、このAPIで
        途中取消・rollbackせず409で待機を案内する。
        """
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
            for key in ("imported", "skipped", "sql_statements"):
                if type(payload.get(key)) is int:
                    result[key] = payload[key]
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
    return application


app = create_app()
