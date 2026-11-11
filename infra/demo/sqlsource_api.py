"""Authenticated, fixed SQL source controls; no arbitrary SQL or identifiers.

The source generator and subsequent ingestion remain separate operations.
All DB exceptions stop at a fixed error boundary. Source reader credentials
are never promoted to a writer; the DBA applies owned DDL separately.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from job_manager import JobConflict, OperationFailure
from pydantic import BaseModel, ConfigDict, Field, model_validator
from service import fixed_sql_module


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


def sqlsource_router(store, jobs, authorize):
    """Bind fixed operations to the applied private configuration snapshot."""
    router = APIRouter(
        prefix="/sql-source",
        tags=["固定SQL source"],
        dependencies=[Depends(authorize)],
    )

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
