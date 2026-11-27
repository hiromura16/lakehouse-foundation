#!/usr/bin/env python3
"""提示された合成CDCの観測だけを検査する。接続・適用・修復はしない。

入力は希望契約、Phase 3A の登録観測、次の V1 証跡の JSON のみ:
version/environment/fixture_id/generation, UTC observed_at(epoch秒)、
registration_sha256/desired_spec_sha256、connectors/tables 配列。
証跡の指紋は cdc_registration.digest の正規 JSON:
- registration_sha256 は Phase 3A の observation_sha256。
- desired_spec_sha256 は希望仕様の指紋。connector は compile_spec の順、
  table は route ID の順。各仕様オブジェクトの完全な指紋で照合する。
- topic は使う一方の alias 名の指紋、group は合成 fixture_id と
  topic.prefix の配列の指紋。既存グループや alias の両方は仮定しない。
- target は kind を加えた契約上の destination オブジェクトの指紋。
- partition_count はその topic の全 partition 数。partitions は 0 始まりの
  全座標と、それぞれの broker/local の次位置・broker 下界/上界を報告する。
- 個々の未観測値は明示 null。connector/task/SQL/capture/target の状態、
  最終イベント経過秒やエラー分類を推測で埋めない。

as-of と 24h の範囲は合成報告の整合性検査で、署名・実測や運用の
鮮度SLAではない。入力者を認証せず実接続もしないため、結果はあくまで
提示資料の内訳である。数値・指紋も無条件に公開できる情報ではない。
既存 offset や Iceberg の位置は変えない。
"""

import json
import sys
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
from cdc_registration import (  # noqa: E402
    FIXTURE_ID,
    HEX_DIGEST,
    assessment,
    canonical_contract,
    compile_spec,
    digest,
    load_input,
    match,
    match_observed,
)

VERSION = 1
MAX_ID = 2**63 - 1
MAX_AGE = 86_400
KINDS = ("raw", "current", "silver")
RUN_STATES = ("running", "paused", "failed", "stopped", "absent")
SNAPSHOTS = ("not_started", "running", "complete", "disabled")
ERRORS = ("none", "source", "broker", "catalog", "storage", "decode")
MISMATCH_HELP = (
    "CHECK_SAME_SCOPE_GROUP_TOPIC_PARTITION_AND_GENERATION",
    "COMPARE_BROKER_AND_LOCAL_NEXT_OFFSETS_WITH_READ_ONLY_TOOLS",
    "KEEP_CONSUMER_PAUSED_FOR_APPROVED_RECONCILIATION",
    "DO_NOT_RESET_OFFSETS_OR_RECREATE_STATE",
)


def boolean(value):
    """欠落は未観測、文字列や整数の真偽値は許容しない。"""
    require(value is None or type(value) is bool, "INVALID_OBSERVED_BOOLEAN")
    return value


def quantity(value, *, maximum=MAX_ID):
    """集計値だけを許可し、型エラーで値を表示しない。"""
    require(
        value is None or bounded_int(value, 0, maximum),
        "INVALID_OBSERVED_QUANTITY",
    )
    return value


def enum(value, choices):
    """自由記述のエラー本文、状態、ログは取り込まない。"""
    require(value is None or value in choices, "INVALID_OBSERVED_ENUM")
    return value


def checksum(value):
    """識別子そのものではなく、比較用の仕様指紋だけを受け取る。"""
    require(value is None or match(HEX_DIGEST, value), "INVALID_OBSERVED_HASH")
    return value


def list_limit(value, low, high):
    """観測の繰返し・省略とは別に容積を制限する。"""
    require(
        isinstance(value, list) and low <= len(value) <= high,
        "INVALID_OBSERVED_LIST",
    )
    return value


def observe_sql(evidence, issue):
    """SQLの各証跡を検査し、確証のないcaptureを正常にしない。"""
    names = (
        "table_exists",
        "columns_match",
        "primary_key_match",
        "database_cdc_enabled",
        "table_cdc_enabled",
        "capture_visible",
    )
    fields(evidence, set(names) | {"capture_count"}, "INVALID_SQL_OBSERVATION")
    report = {name: boolean(evidence[name]) for name in names}
    capture = quantity(evidence["capture_count"], maximum=2**31 - 1)
    require(
        report["capture_visible"] is True or capture is None,
        "INCONSISTENT_CAPTURE_OBSERVATION",
    )
    for name in names:
        result = report[name]
        if result is not True:
            issue(
                "SQL_"
                + name.upper()
                + ("_UNKNOWN" if result is None else "_NOT_CONFIRMED")
            )
    if capture is None:
        issue("SQL_CAPTURE_COUNT_UNKNOWN")
    elif capture != 1:
        issue("SQL_CAPTURE_NOT_UNIQUE")
    return {**report, "capture_count": capture}


def observe_positions(evidence, count, issue):
    """next offset 同士を同じ partition で照合し、lagは上界から算出する。"""
    count = quantity(count, maximum=128)
    require(count is None or count > 0, "INVALID_PARTITION_COUNT")
    if evidence is None:
        issue("POSITIONS_UNKNOWN")
        return None
    require(count is not None, "PARTITION_COUNT_REQUIRED")
    positions = list_limit(evidence, count, count)
    result = []
    seen = set()
    for row in positions:
        fields(
            row,
            {
                "partition",
                "broker_next",
                "local_next",
                "broker_low",
                "broker_high",
            },
            "INVALID_POSITION_FIELDS",
        )
        partition = quantity(row["partition"], maximum=2**31 - 1)
        require(
            partition is not None and partition not in seen,
            "INVALID_OR_DUPLICATE_PARTITION",
        )
        require(partition < count, "PARTITION_OUTSIDE_OBSERVED_RANGE")
        seen.add(partition)
        broker = quantity(row["broker_next"])
        local = quantity(row["local_next"])
        low = quantity(row["broker_low"])
        high = quantity(row["broker_high"])
        if broker is not None and local is not None and broker != local:
            state = "mismatch"
            issue("POSITION_MISMATCH", partition=partition)
        elif None in (broker, local):
            state = "unknown"
            issue("POSITION_UNKNOWN", partition=partition)
        else:
            state = "aligned"
        if low is None:
            issue("BROKER_LOW_UNKNOWN", partition=partition)
        if high is None:
            issue("BROKER_HIGH_UNKNOWN", partition=partition)
        if None in (low, high) and state == "aligned":
            state = "unknown"
        if low is not None and high is not None and low > high:
            issue("BROKER_RETAINED_RANGE_INVERTED", partition=partition)
            state = "invalid"
        if low is not None and any(
            offset is not None and offset < low for offset in (broker, local)
        ):
            issue("POSITION_BELOW_RETAINED_LOW", partition=partition)
            state = "invalid"
        if high is not None and any(
            offset is not None and offset > high for offset in (broker, local)
        ):
            issue("POSITION_ABOVE_HIGH", partition=partition)
            state = "invalid"
        lag = (
            high - broker
            if None not in (low, high, broker) and low <= broker <= high
            else None
        )
        result.append(
            {
                "partition": partition,
                "broker_next": broker,
                "local_next": local,
                "broker_low": low,
                "broker_high": high,
                "broker_lag": lag,
                "state": state,
            }
        )
    return sorted(result, key=lambda row: row["partition"])


def observe_targets(targets, desired, issue):
    """出力先の同一性、観測済みsnapshotと件数を別々に扱う。"""
    fields(targets, set(KINDS), "INVALID_TARGETS")
    result = {}
    for kind in KINDS:
        row = targets[kind]
        fields(
            row,
            {"identity_sha256", "snapshot_id", "row_count"},
            "INVALID_TARGET_OBSERVATION",
        )
        identity = checksum(row["identity_sha256"])
        metadata = {"kind": kind, **desired[kind]}
        if identity is None:
            issue("TARGET_IDENTITY_UNKNOWN", layer=kind)
        elif identity != digest(metadata):
            issue("TARGET_IDENTITY_MISMATCH", layer=kind)
        snap = quantity(row["snapshot_id"])
        rows = quantity(row["row_count"])
        if None in (snap, rows):
            issue("TARGET_STATE_UNKNOWN", layer=kind)
        require(snap is None or snap > 0, "INVALID_SNAPSHOT_ID")
        result[kind] = {
            "snapshot_id": snap if identity == digest(metadata) else None,
            "row_count": rows if identity == digest(metadata) else None,
        }
    return result


def observe_table(row, route, group_sha, issue):
    """明示された1つのactive aliasだけを位置の観測対象にする。"""
    fields(
        row,
        {
            "route_sha256",
            "topic_sha256",
            "group_sha256",
            "sql",
            "partitions",
            "partition_count",
            "targets",
            "last_event_age_seconds",
            "last_error",
        },
        "INVALID_TABLE_OBSERVATION",
    )
    require(
        checksum(row["route_sha256"]) == digest(route), "TABLE_ROUTE_MISMATCH"
    )
    observed_topic = checksum(row["topic_sha256"])
    alias = next(
        (
            name
            for name, value in route["topics"].items()
            if observed_topic == digest(value)
        ),
        None,
    )
    group = checksum(row["group_sha256"])
    if alias is None:
        issue("TOPIC_UNKNOWN" if observed_topic is None else "TOPIC_MISMATCH")
    if group != group_sha:
        issue("GROUP_UNKNOWN" if group is None else "GROUP_MISMATCH")
    age = quantity(row["last_event_age_seconds"])
    last_error = enum(row["last_error"], ERRORS)
    if age is None:
        issue("LAST_EVENT_AGE_UNKNOWN")
    if last_error is None:
        issue("LAST_ERROR_UNKNOWN")
    elif last_error != "none":
        issue("LAST_ERROR_REPORTED")
    # Report raw numeric positions only for a topic AND group positively bound.
    bound = alias is not None and group == group_sha
    positions = observe_positions(
        row["partitions"],
        row["partition_count"],
        issue if bound else lambda *_args, **_fields: None,
    )
    return {
        "active_topic_alias": alias,
        "sql": observe_sql(row["sql"], issue),
        "partitions": positions if bound else None,
        "targets": observe_targets(
            row["targets"], route["destinations"], issue
        ),
        "last_event_age_seconds": age,
        "last_error": last_error,
    }


def observe_connector(row, wanted, issue):
    """Connector/Taskとsnapshotを別々に評価する。再起動は決めない。"""
    fields(
        row,
        {"spec_sha256", "state", "tasks", "snapshot"},
        "INVALID_CONNECTOR_OBSERVATION",
    )
    connector = enum(row["state"], RUN_STATES)
    snapshot = enum(row["snapshot"], SNAPSHOTS)
    if checksum(row["spec_sha256"]) != digest(wanted):
        issue("CONNECTOR_DESIRED_SPEC_NOT_CONFIRMED")
    if connector != "running":
        issue("CONNECTOR_NOT_RUNNING" if connector else "CONNECTOR_UNKNOWN")
    tasks = row["tasks"]
    if tasks is None:
        issue("TASKS_UNKNOWN")
    else:
        tasks = [enum(task, RUN_STATES) for task in list_limit(tasks, 0, 32)]
        if tasks != ["running"]:
            issue("SINGLE_RUNNING_TASK_NOT_CONFIRMED")
    expected = (
        "complete" if wanted["snapshot_policy"] == "initial" else "disabled"
    )
    if snapshot != expected:
        issue("SNAPSHOT_UNKNOWN" if snapshot is None else "SNAPSHOT_NOT_READY")
    return {"state": connector, "tasks": tasks, "snapshot": snapshot}


def structure(connectors):
    """比較用件数だけを表へ出し、includeリストを出力しない。"""
    return {
        "connectors": len(connectors),
        "table_filters": sum(len(group["routes"]) for group in connectors),
        "column_filters": sum(
            len(route["columns"])
            for group in connectors
            for route in group["routes"]
        ),
    }


def report(desired, registration, runtime, fixture_id, generation, now):
    """期待対象と観測世代を先に結び付け、未観測の合格を防ぐ。"""
    require(
        match(FIXTURE_ID, fixture_id)
        and match(HEX_DIGEST, generation)
        and bounded_int(now, 0, MAX_ID),
        "INVALID_EXPECTED_SCOPE",
    )
    wanted = canonical_contract(desired)
    planned = assessment(wanted, registration, fixture_id)
    _, before, _ = match_observed(registration, fixture_id)
    connectors = compile_spec(wanted)["connectors"]
    fields(
        runtime,
        {
            "version",
            "environment",
            "fixture_id",
            "generation",
            "observed_at",
            "registration_sha256",
            "desired_spec_sha256",
            "connectors",
            "tables",
        },
        "INVALID_RUNTIME_OBSERVATION",
    )
    require(
        type(runtime["version"]) is int
        and runtime["version"] == VERSION
        and runtime["environment"] == "synthetic"
        and runtime["fixture_id"] == fixture_id
        and runtime["generation"] == generation,
        "RUNTIME_SCOPE_OR_GENERATION_MISMATCH",
    )
    require(
        runtime["registration_sha256"] == planned["observation_sha256"]
        and runtime["desired_spec_sha256"] == planned["desired_spec_sha256"],
        "RUNTIME_VIEW_MISMATCH",
    )
    recorded = quantity(runtime["observed_at"])
    require(
        recorded is not None and 0 <= now - recorded <= MAX_AGE,
        "RUNTIME_OBSERVATION_STALE_OR_FUTURE",
    )

    # Explicit group identities are derived from this fixture only. They are
    # not asserted to be any existing runtime's subscription/group.
    routes = [
        (group, route) for group in connectors for route in group["routes"]
    ]
    routes.sort(key=lambda pair: pair[1]["id"])
    tables = list_limit(runtime["tables"], len(routes), len(routes))
    evidence = list_limit(
        runtime["connectors"], len(connectors), len(connectors)
    )
    diagnostics = []

    def reporter(kind, index):
        """安全な位置と固定reasonだけを保持する。"""

        def issue(code, **positions):
            diagnostics.append({"code": code, kind: index, **positions})

        return issue

    connector_report = [
        observe_connector(row, connector, reporter("connector", number))
        for number, (row, connector) in enumerate(zip(evidence, connectors), 1)
    ]
    table_report = [
        observe_table(
            row,
            route,
            digest([fixture_id, group["topic.prefix"]]),
            reporter("table", number),
        )
        for number, (row, (group, route)) in enumerate(zip(tables, routes), 1)
    ]
    help_codes = (
        list(MISMATCH_HELP)
        if any(item["code"] == "POSITION_MISMATCH" for item in diagnostics)
        else []
    )
    status = (
        "blocked"
        if diagnostics or planned["blockers"]
        else (
            "observed_aligned"
            if planned["status"] == "aligned"
            else "proposal_only"
        )
    )
    current_spec = compile_spec(before) if before else None
    return {
        "status": status,
        "observation_age_seconds": now - recorded,
        "generation_sha256": digest(generation),
        "registration_status": planned["status"],
        "registration_blockers": planned["blockers"],
        "registration_issues": planned["issues"],
        "current_spec_sha256": digest(current_spec) if current_spec else None,
        "desired_spec_sha256": planned["desired_spec_sha256"],
        "current_structure": structure(current_spec["connectors"])
        if current_spec
        else None,
        "desired_structure": structure(connectors),
        "connectors": connector_report,
        "tables": table_report,
        "diagnostics": diagnostics,
        "position_mismatch_help": help_codes,
        "proposal": {
            "steps": planned["steps"] if status != "blocked" else [],
            "steps_withheld": status == "blocked",
            "writes_available": False,
            "restart_is_proposed": False,
            "snapshot_is_requested": False,
            "offset_reset_available": False,
        },
    }


def main(argv=None):
    """3ファイルの診断だけを、固定JSONと終了値で返す。"""
    parser = ContractParser(description=__doc__)
    parser.add_argument("--offline-fixture", action="store_true")
    parser.add_argument("--fixture-id")
    parser.add_argument("--generation")
    parser.add_argument("--as-of-epoch", type=int, required=True)
    for name in ("desired", "registration", "runtime"):
        parser.add_argument("--" + name, type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        require(
            args.offline_fixture
            and match(FIXTURE_ID, args.fixture_id)
            and match(HEX_DIGEST, args.generation)
            and bounded_int(args.as_of_epoch, 0, MAX_ID),
            "EXPLICIT_SYNTHETIC_SCOPE_REQUIRED",
        )
        result = report(
            load_input(args.desired),
            load_input(args.registration),
            load_input(args.runtime),
            args.fixture_id,
            args.generation,
            args.as_of_epoch,
        )
        code = 0 if result["status"] == "observed_aligned" else 1
    except ContractError as error:
        result = {"status": "invalid", "code": str(error)}
        code = 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
