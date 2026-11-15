"""Narrow independent-runner reservations; no Docker or core lifecycle here.

The optional runner token is installed by the operator outside the checkout.
A reservation survives API restarts and never expires into an automatic write
unlock. This proves only Demo API JobManager + worker idle, not other engines.
"""

import contextlib
import os
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lifecycle_api import worker_is_idle
from lifecycle_store import (
    Journal,
    LifecycleError,
    open_directory,
    private_bytes,
)
from pydantic import BaseModel, ConfigDict, Field


class Reservation(BaseModel):
    """Bind maintenance to the enrolled runner reservation and explicit ACK."""

    model_config = ConfigDict(strict=True, extra="forbid")
    reservation_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    allow_interruption: bool = False


class MaintenanceJournal(Journal):
    """Reuse only private transaction storage; reservations do not expire."""

    def expire(self, data):
        """Validate a persistent reservation; never expire into an automatic
        unlock.
        """
        if (
            set(data) != {"version", "runner", "plans", "operations", "audit"}
            or data["runner"] is not None
            or data["audit"] != []
            or data["operations"]
            or set(data["plans"]) - {"whole_reservation"}
        ):
            raise LifecycleError("STATE_UNAVAILABLE")
        value = data["plans"].get("whole_reservation")
        if value is not None and (
            type(value) is not dict
            or set(value) != {"reservation_id"}
            or type(value["reservation_id"]) is not str
            or len(value["reservation_id"]) != 64
            or any(
                x not in "0123456789abcdef" for x in value["reservation_id"]
            )
        ):
            raise LifecycleError("STATE_UNAVAILABLE")


def install_whole_maintenance(application, store, jobs, *, worker_idle=None):
    """Attach narrow runner-only reservations to the existing job lock."""
    journal = MaintenanceJournal(store.path.parent / ".whole-maintenance")
    worker_idle = worker_idle or worker_is_idle
    held = {"lock": False}
    security = HTTPBearer(auto_error=False)
    router = APIRouter(prefix="/whole-maintenance", tags=["全体管理担当専用"])
    application.state.whole_maintenance = journal

    async def authorize(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(security)
        ],
    ):
        directory = None
        valid = False
        try:
            directory = open_directory(journal.directory)
            token = (
                private_bytes(directory, "runner.token", maximum=128)
                .decode("ascii")
                .strip()
            )
            valid = len(token) == 64 and all(
                x in "0123456789abcdef" for x in token
            )
            valid = (
                valid
                and credentials is not None
                and secrets.compare_digest(token, credentials.credentials)
            )
        except (OSError, ValueError, UnicodeError, TypeError):
            valid = False
        finally:
            if directory is not None:
                os.close(directory)
        if not valid:
            raise HTTPException(401, "WHOLE_RUNNER_AUTH_REQUIRED")

    def reserved(data):
        # Existing Journal schema has a bounded plans dictionary. This local
        # instance has no lifecycle plans/operations, only this fixed record.
        return data["plans"].get("whole_reservation")

    @contextlib.contextmanager
    def transaction(*, create=True):
        try:
            with journal.transaction(create=create) as data:
                yield data
        except HTTPException:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(
                409, "WHOLE_MAINTENANCE_STATE_UNAVAILABLE"
            ) from None

    @application.middleware("http")
    async def whole_gate(request, call_next):
        if request.method in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        } and not request.url.path.startswith("/whole-maintenance/"):
            directory = None
            try:
                try:
                    directory = open_directory(journal.directory)
                except FileNotFoundError:
                    return await call_next(request)
                # A partially enabled directory is not feature-off. Only the
                # authenticated reserve may initialize a missing journal.
                private_bytes(directory, "journal.json")
                with journal.transaction(create=False) as data:
                    if reserved(data):
                        return JSONResponse(
                            {"detail": "WHOLE_POC_MAINTENANCE"}, 409
                        )
            except (OSError, ValueError, TypeError, KeyError):
                return JSONResponse(
                    {"detail": "WHOLE_MAINTENANCE_STATE_UNAVAILABLE"}, 409
                )
            finally:
                if directory is not None:
                    os.close(directory)
        return await call_next(request)

    @router.get("/status", dependencies=[Depends(authorize)])
    async def status():
        directory = None
        try:
            directory = open_directory(journal.directory)
            try:
                private_bytes(directory, "journal.json")
            except FileNotFoundError:
                return {
                    "reserved": False,
                    "initialized": False,
                    "scope": "demo_jobs_and_worker_only",
                }
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(
                409, "WHOLE_MAINTENANCE_STATE_UNAVAILABLE"
            ) from None
        finally:
            if directory is not None:
                os.close(directory)
        with transaction(create=False) as data:
            return {
                "reserved": bool(reserved(data)),
                "initialized": True,
                "scope": "demo_jobs_and_worker_only",
            }

    @router.post("/initialize", dependencies=[Depends(authorize)])
    async def initialize(body: Reservation):
        """Explicitly persist an empty ready ledger; do not claim job idle."""
        with transaction():
            # Existing/corrupt state is validated, never silently replaced.
            return {"initialized": True, "scope": "state_only_no_drain"}

    @router.post("/reserve", dependencies=[Depends(authorize)])
    async def reserve(body: Reservation):
        with transaction() as data:
            previous = reserved(data)
            if previous and not secrets.compare_digest(
                previous["reservation_id"], body.reservation_id
            ):
                raise HTTPException(409, "WHOLE_RESERVATION_CONFLICT")
            if not held["lock"]:
                acquired = jobs.operation.acquire(blocking=False)
                if not acquired and not body.allow_interruption:
                    raise HTTPException(409, "WORKLOAD_BUSY")
                held["lock"] = acquired
            try:
                idle = held["lock"] and worker_idle()
            except Exception:
                idle = False
            if not idle and not body.allow_interruption:
                if previous is None:
                    jobs.operation.release()
                    held["lock"] = False
                raise HTTPException(409, "WORKLOAD_BUSY")
            data["plans"]["whole_reservation"] = {
                "reservation_id": body.reservation_id
            }
            return {
                "reserved": True,
                "scope": "demo_jobs_and_worker_only",
                "drained": bool(idle),
                "interruption_possible": not idle,
            }

    @router.post("/release", dependencies=[Depends(authorize)])
    async def release(body: Reservation):
        with transaction() as data:
            previous = reserved(data)
            if not previous or not secrets.compare_digest(
                previous["reservation_id"], body.reservation_id
            ):
                raise HTTPException(409, "WHOLE_RESERVATION_CONFLICT")
            del data["plans"]["whole_reservation"]
            if held["lock"]:
                jobs.operation.release()
                held["lock"] = False
            return {"released": True}

    application.include_router(router)
