#!/usr/bin/env python3
"""隔離した合成SQLite台帳だけを変更し、実CDCの適用はしない。

prepare は希望契約と登録/実行の合成報告を比較し、期限付きの JSON を
標準出力へ返す。apply/reconcile はその JSON と同じ入力に対し、操作者が
確認した SHA-256 を --confirm-sha256 に明示する。同じ指紋は認証や署名に
ならず、合成の観測はファイルの内容であって現場の状態を示さない。

台帳は同名fixture IDの専用0700ディレクトリに限り、最初は空であること、
同じLinuxユーザーが所有することを要求する。一つの計画だけを受け入れ、
計画・世代・scope・観測・希望仕様と完了手順の指紋を記録する。手順ごとに
受領書を確定し、失敗した段階だけを戻す。一貫した未完了台帳の続きを実行
するには期限内に同じ承認を使って明示 reconcile が必要。期限切れ、未知の
ジャーナル、壊れた状態からの自動回復や新計画での上書きはしない。

受領書は合成手順の順番を記録するだけで、SQL、Kafka、Iceberg、
実コネクタの操作や確認を実行しない。時刻はローカル時計であり、環境の
真正性、障害の外部監査や非協調バイパスを防ぐ認可境界ではない。
"""

import json
import os
import sqlite3
import stat
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True

from cdc_contract import (  # noqa: E402
    ContractError,
    ContractParser,
    bounded_int,
    fields,
    require,
)
from cdc_observation import report  # noqa: E402
from cdc_registration import (  # noqa: E402
    FIXTURE_ID,
    HEX_DIGEST,
    canonical_contract,
    digest,
    load_input,
    match,
)

PLAN_VERSION = 1
PLAN_MAX_TTL = 900
MAX_TIME = 2**63 - 1
MAX_STEPS = 260
LEDGER = "cdc-fixture.sqlite3"
SQLITE_APPLICATION_ID = 0x43444346
CONTROL = """CREATE TABLE control (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    plan_sha TEXT NOT NULL,
    scope_sha TEXT NOT NULL,
    generation_sha TEXT NOT NULL,
    basis_sha TEXT NOT NULL,
    desired_contract_sha TEXT NOT NULL,
    desired_sha TEXT NOT NULL,
    applied_sha TEXT
)"""
RECEIPTS = """CREATE TABLE receipts (
    ordinal INTEGER PRIMARY KEY CHECK (ordinal >= 1),
    step_sha TEXT NOT NULL
)"""


class FixtureInterrupted(RuntimeError):
    """単体試験で手順の途中停止だけを注入する。公開CLIでは選べない。"""


def now_epoch():
    """期限判定をローカル時計から読み、CLI入力では上書きしない。"""
    return int(time.time())


def prepare(desired, registration, runtime, fixture, generation, issued, ttl):
    """既存の診断が許す合成操作だけを、期限付きの承認候補へ変換する。"""
    require(
        bounded_int(issued, 0, MAX_TIME - PLAN_MAX_TTL)
        and bounded_int(ttl, 1, PLAN_MAX_TTL),
        "INVALID_PLAN_EXPIRY",
    )
    desired = canonical_contract(desired)
    result = report(
        desired, registration, runtime, fixture, generation, issued
    )
    steps = result["proposal"]["steps"]
    require(
        result["status"] == "proposal_only" and 1 <= len(steps) <= MAX_STEPS,
        "PLAN_NOT_ELIGIBLE",
    )
    body = {
        "version": PLAN_VERSION,
        "environment": "synthetic",
        "scope_sha256": digest(fixture),
        "generation_sha256": digest(generation),
        "basis_sha256": digest(
            {
                "registration": registration,
                "runtime": runtime,
            }
        ),
        "desired_contract_sha256": digest(desired),
        "desired_sha256": result["desired_spec_sha256"],
        "issued_at": issued,
        "expires_at": issued + ttl,
        "steps": steps,
    }
    return {**body, "plan_sha256": digest(body)}


def validate_approval(
    approved, confirmation, inputs, fixture, generation, now
):
    """同じ観測・scope・世代で再計算し、一致前には台帳を開かない。"""
    fields(
        approved,
        {
            "version",
            "environment",
            "scope_sha256",
            "generation_sha256",
            "basis_sha256",
            "desired_contract_sha256",
            "desired_sha256",
            "issued_at",
            "expires_at",
            "steps",
            "plan_sha256",
        },
        "INVALID_APPROVAL_PLAN",
    )
    require(
        match(HEX_DIGEST, confirmation)
        and confirmation == approved["plan_sha256"],
        "EXACT_CONFIRMATION_REQUIRED",
    )
    require(
        type(approved["version"]) is int
        and approved["version"] == PLAN_VERSION
        and approved["environment"] == "synthetic"
        and all(
            match(HEX_DIGEST, approved[key])
            for key in (
                "scope_sha256",
                "generation_sha256",
                "basis_sha256",
                "desired_contract_sha256",
                "desired_sha256",
            )
        ),
        "INVALID_APPROVAL_FIELDS",
    )
    proposed = approved["steps"]
    require(
        isinstance(proposed, list) and 1 <= len(proposed) <= MAX_STEPS,
        "INVALID_APPROVAL_STEPS",
    )
    for step in proposed:
        require(
            isinstance(step, dict)
            and set(step) in ({"op"}, {"op", "table"})
            and match(r"[A-Z_]{1,64}", step["op"])
            and ("table" not in step or bounded_int(step["table"], 1, 128)),
            "INVALID_APPROVAL_STEPS",
        )
    issued, expires = approved["issued_at"], approved["expires_at"]
    require(
        bounded_int(issued, 0, MAX_TIME - PLAN_MAX_TTL)
        and bounded_int(expires, 0, MAX_TIME)
        and bounded_int(now, 0, MAX_TIME)
        and 1 <= expires - issued <= PLAN_MAX_TTL,
        "INVALID_PLAN_EXPIRY",
    )
    require(issued <= now < expires, "PLAN_EXPIRED_OR_FUTURE")
    expected = prepare(*inputs, fixture, generation, issued, expires - issued)
    # Python considers True == 1; bind exact types through canonical hashing.
    require(
        digest(approved) == digest(expected)
        and confirmation == expected["plan_sha256"],
        "APPROVED_PLAN_DRIFT",
    )
    current = report(*inputs, fixture, generation, now)
    require(
        current["status"] == "proposal_only"
        and current["proposal"]["steps"] == expected["steps"],
        "RUNTIME_NO_LONGER_ELIGIBLE",
    )
    return expected


def file_identity(path):
    """所有者以外の読み書き、別ファイルへのリンクを受理しない。"""
    info = path.stat(follow_symlinks=False)
    require(
        stat.S_ISREG(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_uid == os.getuid()
        and info.st_nlink == 1,
        "UNSAFE_LEDGER_FILE",
    )


@contextmanager
def locked_fixture(directory, fixture):
    """fixture ID と同名の専用0700ディレクトリでだけ排他を取る。"""
    import fcntl

    absolute = Path(os.path.abspath(directory))
    require(absolute.name == fixture, "LEDGER_DIRECTORY_SCOPE_MISMATCH")
    try:
        resolved = absolute.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ContractError("UNSAFE_LEDGER_DIRECTORY") from None
    require(resolved == absolute, "UNSAFE_LEDGER_DIRECTORY")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    dfd = os.open(absolute, flags)
    try:
        details = os.fstat(dfd)
        require(
            stat.S_IMODE(details.st_mode) == 0o700
            and details.st_uid == os.getuid(),
            "UNSAFE_LEDGER_DIRECTORY",
        )
        try:
            fcntl.flock(dfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ContractError("FIXTURE_LEDGER_BUSY") from None
        # The proc handle keeps resource selection on this verified directory.
        location = Path("/proc/self/fd") / str(dfd)
        require(location.is_dir(), "SAFE_LEDGER_HANDLE_UNAVAILABLE")
        entries = {item.name for item in location.iterdir()}
        require(LEDGER + "-journal" not in entries, "HOT_JOURNAL_UNSUPPORTED")
        require(entries <= {LEDGER}, "FIXTURE_DIRECTORY_NOT_DEDICATED")
        yield location, dfd, LEDGER in entries
    finally:
        os.close(dfd)


def open_database(path, *, readonly=False):
    """SQLiteは既存の所有ファイルだけを開き、モードを変えない。"""
    file_identity(path)
    mode = "ro" if readonly else "rw"
    return sqlite3.connect(
        path.as_uri() + "?mode=" + mode, uri=True, timeout=0
    )


def attest_existing(path, values, steps):
    """別DBや未知の回復を実行する前に、既存台帳を読取りで検証する。"""
    file_identity(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        header = stream.read(72)
    require(
        len(header) == 72
        and header[:16] == b"SQLite format 3\x00"
        and header[18:20] == b"\x01\x01"
        and int.from_bytes(header[68:72], "big") == SQLITE_APPLICATION_ID,
        "LEDGER_HEADER_MISMATCH",
    )
    with closing(open_database(path, readonly=True)) as reader:
        reader.execute("PRAGMA trusted_schema = OFF")
        return inspect_checkpoint(reader, values, steps)


def init_schema(db, values):
    """この試行だけが所有する空の合成台帳を一つのtransactionで作る。"""
    db.execute("BEGIN IMMEDIATE")
    with db:
        db.execute(f"PRAGMA application_id = {SQLITE_APPLICATION_ID}")
        db.execute(CONTROL)
        db.execute(RECEIPTS)
        db.execute(
            "INSERT INTO control VALUES (1, ?, ?, ?, ?, ?, ?, NULL)", values
        )


def inspect_checkpoint(db, values, steps):
    """既存DBを採用しない。台帳・手順・完了位置の一致を再検証する。"""
    require(
        db.execute("PRAGMA application_id").fetchone()
        == (SQLITE_APPLICATION_ID,)
        and db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        and db.execute(
            "SELECT type, name, sql FROM sqlite_schema ORDER BY name"
        ).fetchall()
        == [("table", "control", CONTROL), ("table", "receipts", RECEIPTS)],
        "LEDGER_SCHEMA_MISMATCH",
    )
    rows = db.execute("SELECT * FROM control").fetchall()
    require(
        len(rows) == 1 and rows[0][:-1] == (1, *values),
        "LEDGER_PLAN_BINDING_MISMATCH",
    )
    count = db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
    require(count <= len(steps), "LEDGER_RECEIPT_MISMATCH")
    history = db.execute(
        "SELECT ordinal, step_sha FROM receipts ORDER BY ordinal"
    ).fetchall()
    require(
        history == list(enumerate(steps[:count], 1)), "LEDGER_RECEIPT_MISMATCH"
    )
    require(
        rows[0][-1] == (values[-1] if count == len(steps) else None),
        "LEDGER_FINAL_STATE_MISMATCH",
    )
    return count, history


def execute(
    approved,
    confirmation,
    inputs,
    directory,
    fixture,
    generation,
    *,
    operation,
    clock=now_epoch,
    before_commit=None,
):
    """監査用受領書のみを記録する。本番やサービスへ副作用を持たない。"""
    require(operation in ("apply", "reconcile"), "INVALID_OPERATION")
    plan = validate_approval(
        approved, confirmation, inputs, fixture, generation, clock()
    )
    values = tuple(
        plan[key]
        for key in (
            "plan_sha256",
            "scope_sha256",
            "generation_sha256",
            "basis_sha256",
            "desired_contract_sha256",
            "desired_sha256",
        )
    )
    steps = [digest(step) for step in plan["steps"]]
    with locked_fixture(directory, fixture) as (location, descriptor, exists):
        require(exists or operation == "apply", "FIXTURE_LEDGER_NOT_STARTED")
        require(
            plan["issued_at"] <= clock() < plan["expires_at"],
            "PLAN_EXPIRED_OR_FUTURE",
        )
        path = location / LEDGER
        if exists:
            count, history = attest_existing(path, values, steps)
            if count == len(steps):
                return outcome("already_complete", plan, history)
            require(operation == "reconcile", "EXPLICIT_RECONCILE_REQUIRED")
        else:
            handle = os.open(
                LEDGER,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=descriptor,
            )
            os.close(handle)
        with closing(open_database(path)) as db:
            db.execute("PRAGMA trusted_schema = OFF")
            db.execute("PRAGMA synchronous = FULL")
            if not exists:
                init_schema(db, values)
            count, history = inspect_checkpoint(db, values, steps)
            if count == len(steps):
                return outcome("already_complete", plan, history)
            require(
                not exists or operation == "reconcile",
                "EXPLICIT_RECONCILE_REQUIRED",
            )
            for number in range(count + 1, len(steps) + 1):
                require(
                    plan["issued_at"] <= clock() < plan["expires_at"],
                    "PLAN_EXPIRED_OR_FUTURE",
                )
                db.execute("BEGIN IMMEDIATE")
                try:
                    with db:
                        db.execute(
                            "INSERT INTO receipts VALUES (?, ?)",
                            (number, steps[number - 1]),
                        )
                        if number == len(steps):
                            db.execute(
                                "UPDATE control SET applied_sha=? "
                                "WHERE singleton=1",
                                (values[-1],),
                            )
                        if before_commit is not None:
                            before_commit(number)
                        require(
                            plan["issued_at"] <= clock() < plan["expires_at"],
                            "PLAN_EXPIRED_OR_FUTURE",
                        )
                except FixtureInterrupted:
                    _, history = inspect_checkpoint(db, values, steps)
                    return outcome("partial", plan, history)
            _, history = inspect_checkpoint(db, values, steps)
            return outcome("completed", plan, history)


def outcome(status, plan, history):
    """結果は合成 scope 指紋、件数、順序付きの操作指紋だけ。"""
    return {
        "status": status,
        "environment": "synthetic",
        "plan_sha256": plan["plan_sha256"],
        "completed_steps": len(history),
        "total_steps": len(plan["steps"]),
        "receipts": [
            {"ordinal": number, "step_sha256": fingerprint}
            for number, fingerprint in history
        ],
    }


def main(argv=None):
    """接続先や生の例外を表示せず、明示的な合成操作のみ行う。"""
    parser = ContractParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("prepare", "apply", "reconcile"):
        command = commands.add_parser(operation)
        command.add_argument("--offline-fixture", action="store_true")
        command.add_argument("--fixture-id")
        command.add_argument("--generation")
        for name in ("desired", "registration", "runtime"):
            command.add_argument("--" + name, type=Path, required=True)
        if operation == "prepare":
            command.add_argument("--ttl", type=int, default=300)
        else:
            command.add_argument("--plan", type=Path, required=True)
            command.add_argument("--ledger-dir", type=Path, required=True)
            command.add_argument("--confirm-sha256", required=True)
    try:
        args = parser.parse_args(argv)
        require(
            args.offline_fixture
            and match(FIXTURE_ID, args.fixture_id)
            and match(HEX_DIGEST, args.generation),
            "EXPLICIT_SYNTHETIC_SCOPE_REQUIRED",
        )
        inputs = tuple(
            load_input(path)
            for path in (args.desired, args.registration, args.runtime)
        )
        if args.operation == "prepare":
            result = prepare(
                *inputs,
                args.fixture_id,
                args.generation,
                now_epoch(),
                args.ttl,
            )
            code = 0
        else:
            result = execute(
                load_input(args.plan),
                args.confirm_sha256,
                inputs,
                args.ledger_dir,
                args.fixture_id,
                args.generation,
                operation=args.operation,
            )
            code = 1 if result["status"] == "partial" else 0
    except ContractError as error:
        result, code = {"status": "blocked", "code": str(error)}, 2
    except (OSError, sqlite3.Error, ImportError):
        result, code = (
            {"status": "blocked", "code": "SYNTHETIC_LEDGER_UNAVAILABLE"},
            2,
        )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
