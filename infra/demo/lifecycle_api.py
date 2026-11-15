"""Authenticated durable lifecycle requests; this module cannot run Docker."""

import json
import os
import secrets
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lifecycle_catalog import CARD_GROUP, GROUPS, profile_set
from lifecycle_store import (
    Journal,
    LifecycleError,
    open_directory,
    private_bytes,
    public_settings_digest,
)
from poc_catalog import SERVICES
from pydantic import BaseModel, ConfigDict, Field, model_validator

SECURITY = HTTPBearer(auto_error=False)
GroupId = Literal[
    "monitoring",
    "automation",
    "bi",
    "compute",
    "notebooks",
    "openobserve-lab",
    "sail-lab",
]
State = Literal["running", "stopped", "unknown"]


class StrictModel(BaseModel):
    """拒否対象の追加フィールドを暗黙に捨てず、入力型を厳密に検査する。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class PlanRequest(StrictModel):
    """固定サービスIDと操作種別だけを受け付け、コマンドは受け取らない。"""

    service_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,48}$")
    action: Literal["start", "restart", "stop"]


class OperationRequest(StrictModel):
    """短期計画への明示同意と、二重実行を防ぐ受付キーを渡す。"""

    plan_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    confirmation: str = Field(pattern=r"^[a-f0-9]{32}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")


class RunnerState(StrictModel):
    """ホスト担当が承認範囲・準備状態・観測結果だけを報告する。"""

    project: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,40}$")
    runner_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    config_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    groups: list[GroupId] = Field(max_length=7)
    blocked_groups: dict[
        str,
        Literal[
            "INITIAL_SETUP_REQUIRED",
            "MEMORY_BUDGET",
            "APPROVAL_CHANGED",
        ],
    ] = Field(default_factory=dict)
    observations: dict[str, State] = Field(default_factory=dict)
    observed_at: float = Field(default=0, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def fixed_identifiers(self):
        if (
            len(set(self.groups)) != len(self.groups)
            or not self.blocked_groups.keys() <= set(self.groups)
            or not self.observations.keys() <= CARD_GROUP.keys()
        ):
            raise ValueError("Invalid operator scope.")
        return self


class ClaimRequest(StrictModel):
    """現在の担当世代に限って、未実行要求を一件だけ引き受ける。"""

    runner_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class ResultRequest(StrictModel):
    """秘密値を含まない固定結果を、引受nonceとともに返す。"""

    operation_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    lease_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    status: Literal["succeeded", "failed", "unknown"]
    error_code: str | None = Field(default=None, max_length=64)
    observed_state: State


class ReconcileRequest(StrictModel):
    """手動調査済みの未確定操作を、再実行せずに確認済みへ進める。"""

    operation_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    confirmation: str = Field(pattern=r"^[a-f0-9]{24}$")


class PermitRequest(StrictModel):
    """各変更直前に、有効な担当世代と期限付き実行権限を再検査する。"""

    operation_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    lease_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def reject(code, status=409):
    """No user input, command output or private exception is reflected."""
    raise HTTPException(
        status,
        detail={
            "code": code,
            "message": "管理状態と必要な準備を確認してください。",
        },
    )


def allowed_actions(reasons):
    """Low memory must never remove the operator's safe stop capability."""
    if not reasons:
        return ["start", "restart", "stop"]
    return ["stop"] if set(reasons) == {"MEMORY_BUDGET"} else []


def worker_is_idle():
    """Only the existing internal worker health route is contacted."""
    try:
        with httpx.Client(
            timeout=1, trust_env=False, follow_redirects=False
        ) as client:
            with client.stream("GET", "http://worker:8000/health") as result:
                if result.status_code != 200:
                    return False
                body = bytearray()
                for chunk in result.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 8192:
                        return False
                value = json.loads(body)
                return value.get("busy") is False
    except Exception:
        return False


def install_lifecycle(
    application, store, jobs, authorize, *, worker_idle=None
):
    """Install owner routes and separate non-forwardable operator routes."""
    journal = Journal(store.path.parent / ".lifecycle")
    worker_idle = worker_idle or worker_is_idle
    router = APIRouter(prefix="/lifecycle", tags=["サービス管理"])
    owner = [Depends(authorize)]
    held = {"job_lock": False}
    application.state.lifecycle_journal = journal

    def release_finished(data):
        if not journal.active(data) and held["job_lock"]:
            jobs.operation.release()
            held["job_lock"] = False

    def checked(function):
        try:
            return function()
        except LifecycleError as error:
            reject(str(error))
        except (OSError, ValueError, KeyError, TypeError):
            reject("STATE_UNAVAILABLE")

    def runner_authorize(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(SECURITY),
        ],
    ):
        descriptor = None
        try:
            descriptor = open_directory(journal.directory)
            token = private_bytes(descriptor, "runner.token", maximum=256)
            token = token.decode("ascii").strip()
            valid = (
                len(token) == 64
                and credentials is not None
                and secrets.compare_digest(token, credentials.credentials)
            )
        except (OSError, ValueError, UnicodeError, TypeError):
            valid = False
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if not valid:
            reject("OPERATOR_AUTH_REQUIRED", 401)

    operator = [Depends(runner_authorize)]

    @application.middleware("http")
    async def maintenance_gate(request, call_next):
        # Persisted reservations survive API restarts. Existing connection
        # settings are never rewritten into the applied snapshot.
        if request.method in {
            "POST",
            "PATCH",
            "PUT",
            "DELETE",
        } and not request.url.path.startswith("/lifecycle/"):
            try:
                with journal.transaction(create=False) as data:
                    if journal.active(data):
                        return JSONResponse(
                            status_code=409,
                            content={
                                "code": "LIFECYCLE_BUSY",
                                "message": "管理操作を確認してください。",
                            },
                        )
            except (OSError, ValueError, TypeError, KeyError):
                return JSONResponse(
                    status_code=409,
                    content={
                        "code": "STATE_UNAVAILABLE",
                        "message": "管理状態の確認が必要です。",
                    },
                )
        return await call_next(request)

    @router.get(
        "/capabilities",
        dependencies=owner,
        summary="起動担当の接続状態と操作可能なサービスを確認する",
        description=(
            "選択済みprofileと実際の観測状態を区別して返します。"
            "担当未接続・準備不足・保護対象では実行できません。"
            "このAPI自身はDockerを操作しません。"
        ),
    )
    def capabilities():
        def read():
            public = store.public()
            with journal.transaction(create=False) as data:
                release_finished(data)
                connected = journal.connected(data)
                runner = data["runner"] or {}
                observed_at = runner.get("observed_at", 0)
                fresh_observation = (
                    connected
                    and observed_at > 0
                    and 0 <= journal.clock() - observed_at <= 40
                )
                active = journal.active(data)
                rows = []
                for card in SERVICES:
                    group = CARD_GROUP.get(card.id)
                    reasons = []
                    if not group:
                        reasons.append(
                            "SERVICE_PROTECTED"
                            if not card.profile
                            or card.id in {"swagger", "mcp"}
                            else "SERVICE_UNSUPPORTED"
                        )
                    if not connected:
                        reasons.append("RUNNER_DISCONNECTED")
                    elif group and group not in runner["groups"]:
                        reasons.append("INITIAL_SETUP_REQUIRED")
                    elif group and runner["blocked_groups"].get(group):
                        reasons.append(runner["blocked_groups"][group])
                    if connected and runner[
                        "config_digest"
                    ] != public_settings_digest(public):
                        reasons.append("RUNNER_SCOPE_CHANGED")
                    if active:
                        reasons.append(
                            "OPERATION_UNKNOWN"
                            if active["status"] == "unknown"
                            else "OPERATION_BUSY"
                        )
                    rows.append(
                        {
                            "service_id": card.id,
                            "group_id": group,
                            "title": card.title,
                            "allowed_actions": allowed_actions(reasons),
                            "blocked_reasons": reasons,
                            "observed_state": runner.get(
                                "observations", {}
                            ).get(card.id, "unknown")
                            if fresh_observation
                            else "unknown",
                            "last_seen": observed_at or None,
                            "profile_selected": not card.profile
                            or bool(
                                set(card.profiles) & profile_set(store.applied)
                            ),
                        }
                    )
                # Sail is internal RPC, so it has controls but no invented UI.
                group = "sail-lab"
                reasons = []
                if not connected:
                    reasons.append("RUNNER_DISCONNECTED")
                elif group not in runner["groups"]:
                    reasons.append("INITIAL_SETUP_REQUIRED")
                elif runner["blocked_groups"].get(group):
                    reasons.append(runner["blocked_groups"][group])
                if connected and runner[
                    "config_digest"
                ] != public_settings_digest(public):
                    reasons.append("RUNNER_SCOPE_CHANGED")
                if active:
                    reasons.append(
                        "OPERATION_UNKNOWN"
                        if active["status"] == "unknown"
                        else "OPERATION_BUSY"
                    )
                rows.append(
                    {
                        "service_id": "sail-server",
                        "group_id": group,
                        "title": "Sail Lab Server",
                        "allowed_actions": allowed_actions(reasons),
                        "blocked_reasons": reasons,
                        "observed_state": runner.get("observations", {}).get(
                            "sail-server", "unknown"
                        )
                        if fresh_observation
                        else "unknown",
                        "last_seen": observed_at or None,
                        "profile_selected": group
                        in profile_set(store.applied),
                    }
                )
                return {
                    "version": 1,
                    "manager": "operator-compose",
                    "connected": connected,
                    "last_seen": runner.get("last_seen"),
                    "active_operation": {
                        "id": active["id"],
                        "status": active["status"],
                    }
                    if active
                    else None,
                    "applied_profiles": sorted(profile_set(store.applied)),
                    "desired_profiles": sorted(profile_set(public["values"])),
                    "services": rows,
                }

        return checked(read)

    @router.post(
        "/plans",
        dependencies=owner,
        summary="起動・再起動・停止の影響と短期確認計画を作成する",
        description=(
            "固定サービスグループ・依存関係・稼働jobを検査します。"
            "実行はせず、影響対象と確認値と期限を返します。"
            "任意コマンド・project・Composeファイルは受け付けません。"
        ),
    )
    def plans(options: PlanRequest):
        def create():
            with journal.transaction() as data:
                if not journal.connected(data):
                    raise LifecycleError("RUNNER_DISCONNECTED")
                if jobs.operation.locked() or not worker_idle():
                    raise LifecycleError("OPERATION_BUSY")
                return journal.plan(
                    data, options.service_id, options.action, store.public()
                )

        return checked(create)

    @router.post(
        "/operations",
        dependencies=owner,
        status_code=202,
        summary="確認済み計画を重複防止キー付きで受け付ける",
        description=(
            "接続中の担当へ渡す要求を永続保存します。202は受付を表し、"
            "サービス起動成功ではありません。同じキーの再送は元の要求を"
            "返します。未確定結果は自動再実行しません。"
        ),
    )
    def operations(options: OperationRequest):
        def create():
            acquired = False
            try:
                with journal.transaction() as data:
                    release_finished(data)
                    existing = next(
                        (
                            o
                            for o in data["operations"].values()
                            if o["idempotency_key"] == options.idempotency_key
                        ),
                        None,
                    )
                    if existing:
                        return journal.submit(
                            data,
                            options.plan_id,
                            options.confirmation,
                            options.idempotency_key,
                            store.public(),
                        )
                    if not jobs.operation.acquire(blocking=False):
                        raise LifecycleError("OPERATION_BUSY")
                    acquired = True
                    if not worker_idle():
                        raise LifecycleError("OPERATION_BUSY")
                    result = journal.submit(
                        data,
                        options.plan_id,
                        options.confirmation,
                        options.idempotency_key,
                        store.public(),
                    )
                # Do not hold an in-memory reservation if atomic persistence
                # failed. The persisted queue remains the cross-process gate.
                held["job_lock"] = True
                return result
            except Exception:
                if acquired:
                    jobs.operation.release()
                raise

        return checked(create)

    @router.get(
        "/operations/by-key/{key}",
        dependencies=owner,
        summary="応答を失った受付を重複防止キーから確認する",
        description=(
            "新しい要求を作らずに元の受付と結果を調べます。"
            "実行nonce・コマンド・秘密値は返しません。"
        ),
    )
    def by_key(key: str):
        def read():
            with journal.transaction(create=False) as data:
                release_finished(data)
                match = next(
                    (
                        o
                        for o in data["operations"].values()
                        if o["idempotency_key"] == key
                    ),
                    None,
                )
                if not match:
                    reject("OPERATION_NOT_FOUND", 404)
                return journal.operation_view(match)

        return checked(read)

    @router.get(
        "/operations",
        dependencies=owner,
        summary="サービス操作の直近履歴を新しい順に読む",
        description=(
            "1〜20件を返します。履歴は有界で、全件の永久監査台帳では"
            "ありません。失敗・未確定を成功へ読み替えません。"
        ),
    )
    def history(limit: int = 10):
        def read():
            if not 1 <= limit <= 20:
                reject("INVALID_REQUEST", 422)
            with journal.transaction(create=False) as data:
                release_finished(data)
                rows = sorted(
                    data["operations"].values(),
                    key=lambda item: item["created_at"],
                    reverse=True,
                )
                return {
                    "operations": [
                        journal.operation_view(item) for item in rows[:limit]
                    ]
                }

        return checked(read)

    @router.get(
        "/operations/{identifier}",
        dependencies=owner,
        summary="指定したサービス操作の受付・実行・結果を確認する",
        description=(
            "queued・claimed・running・succeeded・failed・unknownを区別"
            "します。unknownは担当の手動確認が必要で、自動再実行しません。"
        ),
    )
    def operation(identifier: str):
        def read():
            with journal.transaction(create=False) as data:
                release_finished(data)
                item = data["operations"].get(identifier)
                if not item:
                    reject("OPERATION_NOT_FOUND", 404)
                return journal.operation_view(item)

        return checked(read)

    @router.post(
        "/runner/register", dependencies=operator, include_in_schema=False
    )
    @router.post(
        "/runner/heartbeat", dependencies=operator, include_in_schema=False
    )
    def register(options: RunnerState):
        def update():
            if options.project != store.applied.get("COMPOSE_PROJECT_NAME"):
                raise LifecycleError("RUNNER_SCOPE_CHANGED")
            if options.observed_at > journal.clock():
                raise LifecycleError("STATE_UNAVAILABLE")
            with journal.transaction() as data:
                journal.runner_state(data, options.model_dump())
            return {"registered": True}

        return checked(update)

    @router.post(
        "/runner/claim", dependencies=operator, include_in_schema=False
    )
    def claim(options: ClaimRequest):
        def update():
            with journal.transaction() as data:
                if not worker_idle():
                    raise LifecycleError("OPERATION_BUSY")
                return {"operation": journal.claim(data, options.runner_id)}

        return checked(update)

    @router.post(
        "/runner/permit", dependencies=operator, include_in_schema=False
    )
    def permit(options: PermitRequest):
        def update():
            with journal.transaction() as data:
                item = data["operations"].get(options.operation_id)
                if (
                    not item
                    or item["status"] not in {"claimed", "running"}
                    or item["lease_id"] != options.lease_id
                    or not journal.connected(data)
                    or item["runner_id"] != data["runner"]["runner_id"]
                ):
                    raise LifecycleError("OPERATION_UNKNOWN")
                if not worker_idle():
                    raise LifecycleError("OPERATION_BUSY")
                item.update(
                    status="running",
                    updated_at=journal.clock(),
                    lease_expires_at=journal.clock() + 300,
                )
                return {"valid_until": item["lease_expires_at"]}

        return checked(update)

    @router.post(
        "/runner/result", dependencies=operator, include_in_schema=False
    )
    def result(options: ResultRequest):
        def update():
            with journal.transaction() as data:
                item = data["operations"].get(options.operation_id)
                if (
                    not item
                    or item["status"] not in {"claimed", "running"}
                    or item["lease_id"] != options.lease_id
                    or not journal.connected(data)
                    or item["runner_id"] != data["runner"]["runner_id"]
                ):
                    raise LifecycleError("OPERATION_UNKNOWN")
                if options.status == "succeeded":
                    expected = (
                        "stopped" if item["action"] == "stop" else "running"
                    )
                    if (
                        options.observed_state != expected
                        or options.error_code
                    ):
                        raise LifecycleError("STATE_UNAVAILABLE")
                    profiles = profile_set(store.read())
                    group = GROUPS[item["group_id"]]
                    if item["action"] == "start":
                        profiles.update(group.profiles)
                    elif item["action"] == "stop":
                        profiles.discard(group.id)
                    try:
                        store.save(
                            {"COMPOSE_PROFILES": ",".join(sorted(profiles))}
                        )
                    except Exception:
                        options.status = "unknown"
                        options.error_code = "PROFILE_SAVE_FAILED"
                        options.observed_state = "unknown"
                completed = journal.finish(data, options.model_dump())
                runner = data["runner"]
                if runner:
                    # A result updates this group's value only. Never renew
                    # the global scan timestamp for unrelated stale cards.
                    for card in GROUPS[item["group_id"]].cards:
                        runner["observations"][card] = options.observed_state
                release_finished(data)
                return completed

        return checked(update)

    @router.post(
        "/runner/reconcile", dependencies=operator, include_in_schema=False
    )
    def reconcile(options: ReconcileRequest):
        def update():
            if options.confirmation != options.operation_id:
                raise LifecycleError("CONFIRMATION_REQUIRED")
            with journal.transaction() as data:
                item = data["operations"].get(options.operation_id)
                if not item or item["status"] != "unknown":
                    raise LifecycleError("OPERATION_UNKNOWN")
                item.update(
                    status="failed",
                    updated_at=journal.clock(),
                    error={
                        "code": "OPERATOR_RECONCILED",
                        "message": "管理者が未確定処理を確認しました。",
                    },
                )
                journal.event(data, "operator_reconciled", item["id"])
                release_finished(data)
                return journal.operation_view(item)

        return checked(update)

    application.include_router(router)
