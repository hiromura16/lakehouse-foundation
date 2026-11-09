"""Run finite source-only jobs without invoking downstream processing."""

import copy
import secrets
import threading
import time
from pathlib import Path
from typing import Callable


class JobConflict(RuntimeError):
    """Reject overlapping source generation and explicit pipeline requests."""


class OperationFailure(RuntimeError):
    """Carry an application-owned safe failure code and message."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(code)


class JobManager:
    """Serialize bounded batches and keep a small in-memory job history."""

    def __init__(self, output: Path, generator: Callable, settings: dict):
        self.output = output
        self.generator = generator
        self.settings = {
            key: value
            for key, value in settings.items()
            if key.startswith("DEMO_SQLSERVER_")
            or key == "SOURCE_SQLSERVER_DATABASE"
        }
        self.operation = threading.Lock()
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.cancel_events: dict[str, threading.Event] = {}

    def submit(self, options: dict, settings: dict | None = None) -> dict:
        """Reserve the source writer before starting one finite thread."""
        if not self.operation.acquire(blocking=False):
            raise JobConflict("Another operation is running.")
        with self.lock:
            # Bound history without dropping active cancellation handles.
            for identifier in list(self.jobs):
                if len(self.jobs) < 20:
                    break
                if self.jobs[identifier]["status"] in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    del self.jobs[identifier]
                    self.cancel_events.pop(identifier, None)
            identifier = secrets.token_hex(6)
            self.jobs[identifier] = {
                "id": identifier,
                "kind": "source",
                "status": "queued",
                "completed": 0,
                "batches": options["batches"],
                "pending_restart": options.get("pending_restart", False),
                "results": [],
                "error": None,
            }
            event = threading.Event()
            self.cancel_events[identifier] = event
        threading.Thread(
            target=self._run,
            args=(identifier, options, event, dict(settings or self.settings)),
            daemon=True,
        ).start()
        return {
            "id": identifier,
            "status": "queued",
            "pending_restart": options.get("pending_restart", False),
        }

    def submit_pipeline(self, action: str, runner: Callable) -> dict:
        """Queue an explicit worker action with the common operation lock."""
        return self._submit_action(action, runner, kind="pipeline")

    def submit_cdc(
        self,
        action: str,
        runner: Callable,
        *,
        pending_restart: bool = False,
    ) -> dict:
        """Queue one source-only transaction on an owned CDC demo DB."""
        return self._submit_action(
            action, runner, kind="cdc-source", pending_restart=pending_restart
        )

    def _submit_action(
        self,
        action: str,
        runner: Callable,
        *,
        kind: str,
        pending_restart: bool = False,
    ) -> dict:
        """Serialize one-shot source DML and explicit worker actions."""
        if not self.operation.acquire(blocking=False):
            raise JobConflict("Another operation is running.")
        with self.lock:
            for previous in list(self.jobs):
                if len(self.jobs) < 20:
                    break
                if self.jobs[previous]["status"] in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    del self.jobs[previous]
                    self.cancel_events.pop(previous, None)
            identifier = secrets.token_hex(6)
            action_key = (
                "cdc_action" if kind == "cdc-source" else "pipeline_action"
            )
            self.jobs[identifier] = {
                "id": identifier,
                "kind": kind,
                action_key: action,
                "status": "queued",
                "completed": 0,
                "batches": 1,
                "pending_restart": pending_restart,
                "results": [],
                "error": None,
            }
            event = threading.Event()
            self.cancel_events[identifier] = event
        threading.Thread(
            target=self._run_action,
            args=(identifier, action, runner, event),
            daemon=True,
        ).start()
        return {
            "id": identifier,
            "status": "queued",
            "kind": kind,
            "pending_restart": pending_restart,
        }

    def _run_action(self, identifier, action, runner, event):
        """Avoid mid-transaction cancellation and redact driver failures."""
        try:
            with self.lock:
                if event.is_set():
                    self.jobs[identifier]["status"] = "cancelled"
                    return
                self.jobs[identifier]["status"] = "running"
            result = runner(action)
            with self.lock:
                self.jobs[identifier].update(
                    status="succeeded", completed=1, results=[result]
                )
        except Exception as error:
            public = {
                "code": "PIPELINE_FAILED",
                "message": "workerの状態と処理結果を確認してください。",
            }
            if self.jobs[identifier]["kind"] == "cdc-source":
                public = {
                    "code": "CDC_DEMO_FAILED",
                    "message": (
                        "専用検証DBの所有権・行の状態・認証・TLSを"
                        "確認してください。"
                    ),
                }
            if isinstance(error, OperationFailure):
                public = {"code": error.code, "message": error.message}
            with self.lock:
                self.jobs[identifier].update(status="failed", error=public)
        finally:
            self.operation.release()

    def _run(
        self,
        identifier: str,
        options: dict,
        event: threading.Event,
        settings: dict,
    ):
        """Check cancellation between atomic source batches, never mid-SQL."""
        deadline = time.monotonic() + 600
        try:
            with self.lock:
                self.jobs[identifier]["status"] = "running"
            for batch in range(1, options["batches"] + 1):
                if event.is_set():
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Source job deadline exceeded.")
                result = self.generator(
                    self.output,
                    run_id=identifier,
                    batch=batch,
                    seed=options["seed"],
                    orders=options["orders"],
                    days=options["days"],
                    scenario=options["scenario"],
                    destination=options["destination"],
                    images=options["images"],
                    allow_sql_write=options["allow_sql_write"],
                    settings=settings,
                )
                with self.lock:
                    self.jobs[identifier]["results"].append(result)
                    self.jobs[identifier]["completed"] += 1
                if batch < options["batches"] and event.wait(
                    options["interval_seconds"]
                ):
                    break
            with self.lock:
                self.jobs[identifier]["status"] = (
                    "cancelled" if event.is_set() else "succeeded"
                )
        except Exception:
            # Driver exceptions may contain private names and connection data.
            with self.lock:
                self.jobs[identifier]["status"] = "failed"
                self.jobs[identifier]["error"] = {
                    "code": "SOURCE_GENERATION_FAILED",
                    "message": "投入設定・権限・TLSを確認してください。",
                }
        finally:
            self.operation.release()

    def get(self, identifier: str) -> dict:
        """Return metadata without mutable internal cancellation state."""
        with self.lock:
            return copy.deepcopy(self.jobs[identifier])

    def cancel(self, identifier: str) -> dict:
        """Stop the next batch; preserve already published source records."""
        with self.lock:
            job = self.jobs[identifier]
            if job["kind"] != "source" and job["status"] == "running":
                raise JobConflict(
                    "A running one-shot action cannot be cancelled here."
                )
            if job["status"] in {"queued", "running"}:
                self.cancel_events[identifier].set()
            return self.get(identifier)
