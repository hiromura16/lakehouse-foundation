"""外部入力の処理・復旧を排他実行し、短い進捗履歴を保存する。"""

import copy
import secrets
import threading
from pathlib import Path
from typing import Callable


class JobConflict(RuntimeError):
    """排他実行する受信後処理の重複受付を拒否する。"""


class OperationFailure(RuntimeError):
    """Carry an application-owned safe failure code and message."""

    def __init__(self, code: str, message: str):
        """Retain a safe public error instead of a raw driver exception."""
        self.code = code
        self.message = message
        super().__init__(code)


class JobManager:
    """受信後の処理を直列化し、APIプロセス内で最大20件を保持する。"""

    def __init__(self, output: Path):
        """実SQLバッチ専用の受渡し場所と排他ロックを初期化する。"""
        self.output = output
        self.operation = threading.Lock()
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}

    def submit_pipeline(self, action: str, runner: Callable) -> dict:
        """固定の後続処理を一件受け付け、照会IDを直ちに返す。"""
        if not self.operation.acquire(blocking=False):
            raise JobConflict("Another operation is running.")
        identifier = None
        try:
            with self.lock:
                while len(self.jobs) >= 20:
                    del self.jobs[next(iter(self.jobs))]
                identifier = secrets.token_hex(6)
                while identifier in self.jobs:
                    identifier = secrets.token_hex(6)
                self.jobs[identifier] = {
                    "id": identifier,
                    "kind": "pipeline",
                    "pipeline_action": action,
                    "status": "queued",
                    "completed": 0,
                    "batches": 1,
                    "pending_restart": False,
                    "results": [],
                    "error": None,
                }
            threading.Thread(
                target=self._run_action,
                args=(identifier, action, runner),
                daemon=True,
            ).start()
        except Exception:
            if identifier is not None:
                with self.lock:
                    self.jobs.pop(identifier, None)
            self.operation.release()
            raise
        return {"id": identifier, "status": "queued", "kind": "pipeline"}

    def _run_action(self, identifier: str, action: str, runner: Callable):
        """完了状態を記録し、未分類例外に含まれる接続情報は隠す。"""
        try:
            with self.lock:
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
            if isinstance(error, OperationFailure):
                public = {"code": error.code, "message": error.message}
            with self.lock:
                self.jobs[identifier].update(status="failed", error=public)
        finally:
            self.operation.release()

    def history(self, limit: int = 20) -> list[dict]:
        """秘密設定や内部状態を含まない新着順の複製を返す。"""
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("History limit must be in 1..20.")
        with self.lock:
            return copy.deepcopy(list(reversed(self.jobs.values()))[:limit])

    def get(self, identifier: str) -> dict:
        """受信後処理一件の状態を複製して返す。"""
        with self.lock:
            return copy.deepcopy(self.jobs[identifier])
