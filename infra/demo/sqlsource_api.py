"""Authenticated, fixed SQL source controls; no arbitrary SQL or identifiers.

The source generator and subsequent ingestion remain separate operations.
All DB exceptions stop at a fixed error boundary. Source reader credentials
are never promoted to a writer; the DBA applies owned DDL separately.
"""

import importlib
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from job_manager import JobConflict, OperationFailure
from pydantic import BaseModel, ConfigDict, Field, model_validator
from service import fixed_sql_module
from sqlsource_client import public_result, send_batch


class SqlConsent(BaseModel):
    """Require explicit consent and a bounded synthetic run identifier."""

    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: bool
    run_id: str = Field(pattern=r"^[0-9a-f]{12}$")

    @model_validator(mode="after")
    def explicit_consent(self):
        """Require a strict boolean true, including for shared DB writes."""
        if self.confirm is not True:
            raise ValueError("Explicit confirmation required.")
        return self


class SqlTableStatus(BaseModel):
    """Filter status to known logical names, counts and safe timestamps."""

    exists: bool
    rows: int | None = Field(default=None, ge=0)
    last_updated_at: str | None = Field(
        default=None,
        pattern=r"^[0-9TZ: .+\-]{10,40}$",
    )
    cdc_enabled: bool = False
    capture_instance: (
        Literal[
            "lh_demo_erp_products_v1",
            "lh_demo_erp_work_orders_v1",
            "lh_demo_erp_sales_orders_v1",
            "lh_demo_erp_shipments_v1",
            "lh_demo_mes_events_v1",
            "lh_demo_mes_downtime_v1",
        ]
        | None
    ) = None


class SqlDiagnostic(BaseModel):
    """Never return host/database/user, ownership hashes or driver text."""

    connected: bool
    ownership: bool
    permissions_safe: bool
    schema_version: int = Field(ge=0, le=1)
    tables: dict[
        Literal[
            "erp_products",
            "erp_work_orders",
            "erp_sales_orders",
            "erp_shipments",
            "mes_events",
            "mes_downtime",
        ],
        SqlTableStatus,
    ]


class BatchConsent(BaseModel):
    """Confirm downstream writes, without connection or SQL parameters."""

    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: bool

    @model_validator(mode="after")
    def explicit_consent(self):
        """Require a strict boolean true for processing writes."""
        if self.confirm is not True:
            raise ValueError("Explicit processing consent is required.")
        return self


def batch_module():
    """Load the reader with the shared process-local TLS safety adapter."""
    fixed_sql_module()  # Adds only this repository's scripts to import path.
    return importlib.import_module("sqlserver_batch")


def sqlsource_router(store, jobs, authorize):
    """Bind fixed operations to the applied private configuration snapshot."""
    router = APIRouter(
        prefix="/sql-source",
        tags=["固定SQL source"],
        dependencies=[Depends(authorize)],
    )

    @router.post("/batches/{mode}", status_code=202)
    def batch(mode: Literal["snapshot", "incremental"], body: BatchConsent):
        """Queue reader extraction, worker load and separate checkpoint auth.

        Snapshot remains usable without control credentials. Incremental
        requires a committed six-table baseline and separate narrow writer.
        """
        del body  # Validation already required explicit processing consent.
        values = store.reader_settings(include_control=True)

        def run_batch(_):
            """Return success only after a matching worker acknowledgement."""
            try:

                def callback(batch_id, stamp):
                    """Send file identity; keep credentials source-side."""
                    return send_batch(
                        store.applied.get("WORKER_TOKEN", ""), batch_id, stamp
                    )

                module = batch_module()
                runner = (
                    module.run_snapshot
                    if mode == "snapshot"
                    else module.run_incremental
                )
                result = public_result(runner(values, jobs.output, callback))
                if mode == "incremental" and not result["checkpointed"]:
                    raise ValueError(
                        "Incremental checkpoint was not confirmed."
                    )
                return result
            except OperationFailure:
                raise
            except Exception:
                raise OperationFailure(
                    "SQL_BATCH_FAILED",
                    "SOURCEの読取権限・所有権・型とworkerの状態を確認します。",
                ) from None

        try:
            return jobs.submit_pipeline("sql-" + mode, run_batch)
        except JobConflict:
            raise HTTPException(
                409,
                detail={
                    "code": "OPERATION_RUNNING",
                    "message": "操作の完了を待ちます。",
                },
            ) from None

    @router.get("/status", response_model=SqlDiagnostic)
    def source_status():
        """Read owned schema status, without exposing credentials or rows."""
        if not jobs.operation.acquire(blocking=False):
            raise HTTPException(
                409,
                detail={
                    "code": "OPERATION_RUNNING",
                    "message": "操作の完了を待ってください。",
                },
            )
        try:
            return fixed_sql_module().diagnose(store.source_settings())
        except Exception:
            raise HTTPException(
                422,
                detail={
                    "code": "SQL_SOURCE_NOT_PREPARED",
                    "message": (
                        "固定schema・所有権・限定権限・TLSを確認してください。"
                    ),
                },
            ) from None
        finally:
            jobs.operation.release()

    @router.post("/demo/{action}", status_code=202)
    def mutate(
        action: Literal["insert", "update", "delete"], body: SqlConsent
    ):
        """Queue one owned synthetic product DML, without applying DDL."""
        values = store.source_settings()

        def runner(selected):
            """Keep the result contract source-only and exclude driver text."""
            try:
                result = fixed_sql_module().mutate(
                    values,
                    selected,
                    run_id=body.run_id,
                    consent=True,
                )
                count = result["changed_rows"]
                if type(count) is not int or count not in {0, 1}:
                    raise ValueError("Invalid source mutation result.")
                return {
                    "action": selected,
                    "changed_rows": count,
                    "run_id": body.run_id,
                    "source_only": True,
                }
            except Exception:
                raise OperationFailure(
                    "FIXED_SQL_DML_FAILED",
                    "固定SQL sourceの所有権・権限・行の状態を"
                    "確認してください。",
                ) from None

        try:
            return jobs.submit_cdc(action, runner)
        except JobConflict:
            raise HTTPException(
                409,
                detail={
                    "code": "OPERATION_RUNNING",
                    "message": "実行中の操作が終わるまで待ってください。",
                },
            ) from None

    return router
