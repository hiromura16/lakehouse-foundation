#!/usr/bin/env python3
"""Explicit PoC manager API client and separately installed root runner.

No default invocation reads credentials or performs network/Docker operations.
There is no service installation or automatic privilege acquisition here.
"""

import argparse
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).absolute().parents[1]
sys.path.insert(0, str(ROOT))

from infra.management.api import token_file  # noqa: E402
from infra.management.journal import (  # noqa: E402
    exclusive,
    private_directory,
    private_read,
    strict_json,
)
from infra.management.runner import (  # noqa: E402
    Client,
    Docker,
    Maintenance,
    Runner,
    RunnerError,
    enroll,
    internal_host_headers,
    manifest_value,
    private_json,
)


def api_call(origin, token, method, path, payload=None, *, public_origin=None):
    """Call a fixed route without proxy inheritance or redirects."""
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.scheme == "http"
        and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
    ):
        raise RunnerError("MANAGER_ORIGIN_INVALID")
    with httpx.Client(
        trust_env=False, follow_redirects=False, timeout=5
    ) as client:
        with client.stream(
            method,
            origin + path,
            json=payload,
            headers={
                **internal_host_headers(origin, public_origin),
                "Authorization": "Bearer " + token,
                "Accept-Encoding": "identity",
            },
        ) as response:
            if response.status_code not in {200, 202}:
                raise RunnerError("MANAGER_REQUEST_REJECTED")
            raw = bytearray()
            for block in response.iter_bytes():
                raw.extend(block)
                if len(raw) > 65536:
                    raise RunnerError("MANAGER_RESPONSE_LIMIT")
            return strict_json(raw)


def save_new(path, value):
    """Create a new owner-private file exclusively; existing files are
    preserved.
    """
    path = Path(path)
    directory = private_directory(path.parent)
    try:
        exclusive(directory, path.name, value)
    finally:
        os.close(directory)


def main(argv=None):
    """Dispatch an explicit mode; the default is an inert plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--origin",
        default="http://127.0.0.1:8010",
        help="固定manager origin。HTTPS又はloopback HTTP。既定: %(default)s",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        help="所有者0600のtokenファイル。値を引数やstdoutへ書かない",
    )
    parser.add_argument(
        "--public-origin",
        help="公開HTTPS origin。loopback接続の固定Hostだけ一致させる",
    )
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("status", help="固定2PoCの観測状態を読む")
    commands.add_parser("history", help="秘密を含まない管理操作履歴を読む")
    preview = commands.add_parser(
        "plan", help="短期計画を作成・保存。コンテナ操作はしない"
    )
    preview.add_argument(
        "poc_id", choices=("foundation", "vision"), help="固定の論理PoC ID"
    )
    preview.add_argument(
        "action",
        choices=("start", "stop", "restart"),
        help="既存resident containerへの全体操作",
    )
    preview.add_argument(
        "--output",
        type=Path,
        required=True,
        help="新規0600計画ファイル。既存ファイルを上書きしない",
    )
    apply = commands.add_parser(
        "apply", help="保存済み短期計画を一回だけ受付。直接Dockerを呼ばない"
    )
    apply.add_argument("plan_file", type=Path, help="所有者0600の保存済み計画")
    apply.add_argument(
        "--interruption-ack",
        action="store_true",
        help="未完了計算が中断される可能性を明示確認。killや削除の許可ではない",
    )
    operation = commands.add_parser(
        "operation", help="受付済み操作の結果を読む"
    )
    operation.add_argument("identifier", help="24桁hexの操作ID")
    recovery = commands.add_parser(
        "by-key", help="応答喪失時の読取照会。POSTを自動再送しない"
    )
    recovery.add_argument("key", help="元の8-80文字idempotency key")
    init = commands.add_parser(
        "init",
        help="新規private tokenファイルだけ作る。サービス/権限を変更しない",
    )
    init.add_argument(
        "directory",
        type=Path,
        help="安全な親配下の新規又は所有者0700ディレクトリ",
    )
    enrollment = commands.add_parser(
        "enroll",
        help="root operator専用。固定2projectの読取検査と承認manifest作成",
    )
    enrollment.add_argument(
        "--spec",
        type=Path,
        required=True,
        help="root所有0600のoperator選択。browser入力は不可",
    )
    enrollment.add_argument(
        "--output",
        type=Path,
        required=True,
        help="root専用0700親に置く新規0600manifest",
    )
    run = commands.add_parser(
        "run-once",
        help="別途承認・配備したroot runner専用。一受付を検証し実行",
    )
    run.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="root所有0600の固定container承認manifest",
    )
    run.add_argument(
        "--intent-dir",
        type=Path,
        required=True,
        help="既存root所有0700の独立intent/journal保存先",
    )
    serve = commands.add_parser(
        "serve",
        help="承認済みroot runnerのpoll。失敗/不確定なら終了し再試行しない",
    )
    serve.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="root所有0600の承認manifest",
    )
    serve.add_argument(
        "--intent-dir", type=Path, required=True, help="元のroot専用0700保存先"
    )
    serve.add_argument(
        "--poll-seconds",
        type=int,
        default=2,
        choices=range(2, 31),
        help="idle時poll間隔2-30秒。既定2秒",
    )
    reconcile = commands.add_parser(
        "reconcile", help="root operatorの明示照合。mutationを再実行しない"
    )
    reconcile.add_argument(
        "operation_id", help="未確定となった24桁hexの操作ID"
    )
    reconcile.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="同じroot承認manifest。更新や再採用はしない",
    )
    reconcile.add_argument(
        "--intent-dir",
        type=Path,
        required=True,
        help="元のroot専用intent保存先。消去しない",
    )
    args = parser.parse_args(argv)
    try:
        if args.command is None:
            result = {
                "state": "plan_only",
                "docker_calls": 0,
                "network_calls": 0,
                "requires_operator_enrollment": True,
            }
        elif args.command == "init":
            directory = private_directory(args.directory, create=True)
            try:
                # Values are written only to selected owner-private files,
                # never stdout, command arguments, Markdown or shell env.
                for name in ("api.token", "runner.token"):
                    raw = secrets.token_hex(32)
                    fd = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory,
                    )
                    with os.fdopen(fd, "w") as target:
                        os.fchmod(target.fileno(), 0o600)
                        target.write(raw + "\n")
                        target.flush()
                        os.fsync(target.fileno())
                    os.fsync(directory)
                result = {
                    "state": "private_tokens_created",
                    "values_printed": False,
                }
            finally:
                os.close(directory)
        elif args.command in {"enroll", "run-once", "serve", "reconcile"}:
            if os.getuid() != 0 or os.geteuid() != 0:
                raise RunnerError("ROOT_OPERATOR_REQUIRED")
            if args.command == "enroll":
                value = enroll(private_json(args.spec), Docker())
                save_new(args.output, value)
                result = {
                    "state": "enrollment_saved",
                    "poc_ids": ["foundation", "vision"],
                    "docker_mutated": False,
                }
            else:
                if args.token_file is None:
                    raise RunnerError("RUNNER_TOKEN_FILE_REQUIRED")
                value = manifest_value(private_json(args.manifest))
                runner = Runner(
                    value,
                    Client(
                        args.origin,
                        token_file(args.token_file),
                        public_origin=args.public_origin,
                    ),
                    Docker(),
                    args.intent_dir,
                    Maintenance(value),
                )
                result = (
                    runner.reconcile(args.operation_id)
                    if args.command == "reconcile"
                    else runner.run_once()
                )
                if args.command == "serve":
                    while result["state"] in {"idle", "succeeded"}:
                        time.sleep(args.poll_seconds)
                        result = runner.run_once()
                    raise RunnerError("OPERATION_UNKNOWN")
        else:
            if args.token_file is None:
                raise RunnerError("TOKEN_FILE_REQUIRED")
            token = token_file(args.token_file)

            def call(method, path, payload=None):
                return api_call(
                    args.origin,
                    token,
                    method,
                    path,
                    payload,
                    public_origin=args.public_origin,
                )

            if args.command == "plan":
                value = call(
                    "POST",
                    "/api/pocs/" + args.poc_id + "/plans",
                    {"action": args.action},
                )
                save_new(args.output, value)
                result = {
                    "state": "plan_saved",
                    "poc_id": args.poc_id,
                    "action": args.action,
                    "requires_interruption_ack": value.get(
                        "requires_interruption_ack", False
                    ),
                }
            elif args.command == "apply":
                path = args.plan_file
                directory = private_directory(path.parent)
                try:
                    value = strict_json(private_read(directory, path.name))
                finally:
                    os.close(directory)
                payload = {
                    "plan_id": value["plan_id"],
                    "confirmation": value["confirmation"],
                    "idempotency_key": "cli-" + value["plan_id"],
                    "interruption_ack": args.interruption_ack,
                }
                result = call("POST", "/api/operations", payload)
            elif args.command == "operation":
                if not re.fullmatch(r"[a-f0-9]{24}", args.identifier):
                    raise RunnerError("OPERATION_ID_INVALID")
                result = call(
                    "GET",
                    "/api/operations/" + args.identifier,
                )
            elif args.command == "by-key":
                if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", args.key):
                    raise RunnerError("IDEMPOTENCY_KEY_INVALID")
                result = call(
                    "GET",
                    "/api/operations/by-key/" + args.key,
                )
            else:
                result = call(
                    "GET",
                    "/api/pocs"
                    if args.command == "status"
                    else "/api/operations",
                )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        code = (
            str(error)
            if type(error) is RunnerError
            and re.fullmatch(r"[A-Z_]{1,64}", str(error))
            else "MANAGER_OPERATION_FAILED"
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "state": "stopped",
                    "reason": code,
                    "automatic_retry": False,
                    "automatic_rollback": False,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
