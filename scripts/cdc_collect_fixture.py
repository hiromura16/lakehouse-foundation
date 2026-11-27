#!/usr/bin/env python3
"""既に採取された合成CDC証跡を、安全な読取り報告へ束ねる。

ツール自身はSQL、Connect、Kafka、Icebergへ接続しない。入力は固定
5種のローカルfixture JSONと、希望契約・登録観測だけ。Python側の
collectは3B用オブジェクトを返すが、CLIは識別子や観測本文を出さず、
3Bの診断を返す。実採取者の認証・実接続・到達性を主張しない。

共通 envelope は version/domain/environment/fixture_id/generation/
observed_at/registration_sha256/desired_contract_sha256/rows。
Connect 行は DB/prefix 正規ペアの指紋、それ以外は
完全 route の指紋で対応付ける。全5種の oldest 時刻を観測時刻とし、
最大30秒を超える観測の混在を拒否する。未取得の値は null のまま。
Connect の独立 spec 指紋を希望仕様のコピーで補わない。
"""

import json
import signal
import sys
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

if __name__ == "__main__":
    sys.dont_write_bytecode = True

from cdc_contract import (  # noqa: E402
    MAX_COLUMNS,
    MAX_PRIMARY_KEY_COLUMNS,
    MAX_TABLES,
    ContractError,
    ContractParser,
    bounded_int,
    fields,
    identifier,
    require,
    validate_column,
)
from cdc_observation import (  # noqa: E402
    ERRORS,
    KINDS,
    MAX_AGE,
    MAX_ID,
    VERSION,
    boolean,
    checksum,
    enum,
    list_limit,
    observe_positions,
    quantity,
    report,
)
from cdc_registration import (  # noqa: E402
    FIXTURE_ID,
    HEX_DIGEST,
    assessment,
    compile_spec,
    digest,
    load_input,
    match,
)

DOMAINS = ("sql", "connect", "broker", "consumer", "iceberg")
WINDOW_SECONDS = 30
CONFIGURATION_FIELDS = {
    "database.names",
    "topic.prefix",
    "table.include.list",
    "column.include.list",
    "snapshot_policy",
}
TOPIC = r"[a-z0-9_.-]{1,249}"


def envelope(value, domain, fixture, generation, now, view):
    """同じ合成対象・観測世代で、未来や古いファイルを拒否する。"""
    fields(
        value,
        {
            "version",
            "domain",
            "environment",
            "fixture_id",
            "generation",
            "observed_at",
            "registration_sha256",
            "desired_contract_sha256",
            "rows",
        },
        "COLLECTOR_ENVELOPE",
    )
    require(
        type(value["version"]) is int
        and value["version"] == VERSION
        and value["domain"] == domain
        and value["environment"] == "synthetic"
        and value["fixture_id"] == fixture
        and value["generation"] == generation,
        "COLLECTOR_SCOPE_MISMATCH",
    )
    require(
        checksum(value["registration_sha256"]) == view["observation_sha256"]
        and checksum(value["desired_contract_sha256"])
        == view["desired_contract_sha256"],
        "COLLECTOR_DECLARED_VIEW_MISMATCH",
    )
    timestamp = quantity(value["observed_at"])
    require(
        timestamp is not None and 0 <= now - timestamp <= MAX_AGE,
        "COLLECTOR_OBSERVATION_OUTSIDE_WINDOW",
    )
    return timestamp, list_limit(value["rows"], 0, MAX_TABLES)


def indexed(rows, expected):
    """欠落・他対象・重複を、既知の別表の情報で埋めない。"""
    by_identity = {}
    for item in rows:
        require(isinstance(item, dict), "COLLECTOR_ROW_SHAPE")
        ident = checksum(item.get("binding_sha256"))
        require(
            ident in expected and ident not in by_identity,
            "COLLECTOR_ROW_BINDING",
        )
        by_identity[ident] = item
    require(set(by_identity) == set(expected), "COLLECTOR_MISSING_ROWS")
    return by_identity


def same_columns(observed, wanted):
    """入力列の実型を順序に依存せず比較する。未取得はnull。"""
    if observed is None:
        return None
    seen = {}
    for column in list_limit(observed, 0, MAX_COLUMNS):
        validate_column(column)
        name = column["name"]
        require(name not in seen, "COLLECTOR_DUPLICATE_COLUMN")
        seen[name] = column
    return seen == {column["name"]: column for column in wanted}


def same_primary_key(observed, wanted):
    """順序付きPK。列名だけ一致しても並び順が違えば不一致。"""
    if observed is None:
        return None
    key = list_limit(observed, 0, MAX_PRIMARY_KEY_COLUMNS)
    require(
        all(identifier(name) for name in key) and len(set(key)) == len(key),
        "COLLECTOR_PRIMARY_KEY_SHAPE",
    )
    return key == wanted


def sql_receipt(row, route):
    """認識したsourceに限ってSQLの事実を対応付ける。"""
    observations = {
        "table_exists",
        "database_cdc_enabled",
        "table_cdc_enabled",
        "capture_visible",
    }
    fields(
        row,
        observations
        | {
            "binding_sha256",
            "source_sha256",
            "columns",
            "primary_key",
            "capture_count",
        },
        "COLLECTOR_SQL_FIELDS",
    )
    evidence = {key: boolean(row[key]) for key in observations}
    evidence["columns_match"] = same_columns(row["columns"], route["columns"])
    evidence["primary_key_match"] = same_primary_key(
        row["primary_key"], route["primary_key"]
    )
    evidence["capture_count"] = quantity(
        row["capture_count"], maximum=2**31 - 1
    )
    if checksum(row["source_sha256"]) != digest(route["source"]):
        return dict.fromkeys(evidence)
    return evidence


def bridge_connector(row, group):
    """秘密フィールドを受け付けず、別証跡のconnector仕様指紋を渡す。"""
    fields(
        row,
        {
            "binding_sha256",
            "configuration",
            "observed_spec_sha256",
            "state",
            "tasks",
            "snapshot",
        },
        "COLLECTOR_CONNECT_FIELDS",
    )
    actual = row["configuration"]
    observed = checksum(row["observed_spec_sha256"])
    if actual is not None:
        fields(actual, CONFIGURATION_FIELDS, "COLLECTOR_UNSAFE_CONNECT_CONFIG")
        require(
            all(
                isinstance(value, str) and len(value) <= 240_000
                for value in actual.values()
            ),
            "COLLECTOR_CONNECT_CONFIG_SHAPE",
        )
        matches = actual == {key: group[key] for key in CONFIGURATION_FIELDS}
    else:
        matches = False
    # Matching 5 fields does not prove the independent consumer route map.
    return {
        "spec_sha256": observed if matches else None,
        "state": row["state"],
        "tasks": row["tasks"],
        "snapshot": row["snapshot"],
    }


def local_positions(receipt):
    """local checkpoint だけを受け入れ、順番や欠損を勝手に補わない。"""
    fields(
        receipt,
        {
            "binding_sha256",
            "topic_sha256",
            "group_sha256",
            "checkpoints",
            "last_event_age_seconds",
            "last_error",
        },
        "COLLECTOR_CONSUMER_FIELDS",
    )
    if receipt["checkpoints"] is None:
        return {}
    result = {}
    for record in list_limit(receipt["checkpoints"], 0, 128):
        fields(
            record, {"partition", "local_next"}, "COLLECTOR_CHECKPOINT_SHAPE"
        )
        partition = quantity(record["partition"], maximum=2**31 - 1)
        require(
            partition is not None and partition not in result,
            "COLLECTOR_DUPLICATE_CHECKPOINT_PARTITION",
        )
        result[partition] = quantity(record["local_next"])
    return result


def positions(broker, consumer, group, route, fixture):
    """同じtopic/groupのKafka位置とSQLite位置だけを突き合わせる。"""
    fields(
        broker,
        {
            "binding_sha256",
            "active_topic",
            "group_sha256",
            "partition_count",
            "partitions",
        },
        "COLLECTOR_BROKER_FIELDS",
    )
    name = broker["active_topic"]
    require(name is None or match(TOPIC, name), "COLLECTOR_TOPIC_SHAPE")
    topic = digest(name) if name is not None else None
    group_identity = checksum(broker["group_sha256"])
    local = local_positions(consumer)
    local_topic = checksum(consumer["topic_sha256"])
    local_group = checksum(consumer["group_sha256"])
    bound = (
        name in route["topics"].values()
        and local_topic == topic
        and group_identity
        == local_group
        == digest([fixture, group["topic.prefix"]])
    )
    count = quantity(broker["partition_count"], maximum=128)
    if broker["partitions"] is None:
        return topic, group_identity, count, None, bound
    joined = []
    for row in list_limit(broker["partitions"], 1, 128):
        fields(
            row,
            {"partition", "broker_next", "broker_low", "broker_high"},
            "COLLECTOR_BROKER_POSITION_SHAPE",
        )
        partition = quantity(row["partition"], maximum=2**31 - 1)
        joined.append(
            {
                **row,
                "local_next": local.get(partition) if bound else None,
            }
        )
    if bound:
        require(
            set(local) <= {row["partition"] for row in joined},
            "COLLECTOR_CHECKPOINT_OUTSIDE_BROKER_PARTITIONS",
        )
    # Validation also attests the supplied count and each position's types.
    observe_positions(joined, count, lambda *_args, **_fields: None)
    return topic, group_identity, count, joined, bound


def target_receipt(row):
    """Icebergメタデータ証跡にエンドポイントやfile情報を足さない。"""
    fields(row, {"binding_sha256", "targets"}, "COLLECTOR_ICEBERG_FIELDS")
    targets = row["targets"]
    fields(targets, set(KINDS), "COLLECTOR_ICEBERG_TARGET_SHAPE")
    return targets


def collect(desired, registration, receipts, fixture, generation, now):
    """既存3Bが読めるV1に組み立て、整合性の検査を再利用する。"""
    require(
        match(FIXTURE_ID, fixture)
        and match(HEX_DIGEST, generation)
        and bounded_int(now, 0, MAX_ID),
        "COLLECTOR_EXPECTED_SCOPE",
    )
    fields(receipts, set(DOMAINS), "COLLECTOR_DOMAINS")
    plan = assessment(desired, registration, fixture)
    groups = compile_spec(desired)["connectors"]
    group_keys = [
        digest([g["database.names"], g["topic.prefix"]]) for g in groups
    ]
    routes = [(group, route) for group in groups for route in group["routes"]]
    routes.sort(key=lambda pair: pair[1]["id"])
    route_keys = [digest(route) for _, route in routes]
    timestamps, domains = [], {}
    for domain in DOMAINS:
        timestamp, rows = envelope(
            receipts[domain], domain, fixture, generation, now, plan
        )
        timestamps.append(timestamp)
        domains[domain] = indexed(
            rows, group_keys if domain == "connect" else route_keys
        )
    require(
        max(timestamps) - min(timestamps) <= WINDOW_SECONDS,
        "COLLECTOR_MIXED_OBSERVATION_INTERVALS",
    )
    runtime = {
        "version": VERSION,
        "environment": "synthetic",
        "fixture_id": fixture,
        "generation": generation,
        "observed_at": min(timestamps),
        "registration_sha256": plan["observation_sha256"],
        "desired_spec_sha256": plan["desired_spec_sha256"],
        "connectors": [
            bridge_connector(domains["connect"][key], group)
            for key, group in zip(group_keys, groups, strict=True)
        ],
        "tables": [],
    }
    for key, (group, route) in zip(route_keys, routes, strict=True):
        consumer = domains["consumer"][key]
        topic, group_identity, count, joined, bound = positions(
            domains["broker"][key], consumer, group, route, fixture
        )
        age = quantity(consumer["last_event_age_seconds"])
        error = enum(consumer["last_error"], ERRORS)
        runtime["tables"].append(
            {
                "route_sha256": key,
                "sql": sql_receipt(domains["sql"][key], route),
                "topic_sha256": topic,
                "group_sha256": group_identity,
                "partition_count": count,
                "partitions": joined,
                "targets": target_receipt(domains["iceberg"][key]),
                "last_event_age_seconds": age if bound else None,
                "last_error": error if bound else None,
            }
        )
    # Invalid shape never escapes as a "normalized" observation object.
    summary = report(desired, registration, runtime, fixture, generation, now)
    return deepcopy(runtime), summary


@contextmanager
def deadline(seconds):
    """CLI のローカル入力と処理はひとつの固定期限を共有する。"""

    def timeout(_signum, _frame):
        raise ContractError("COLLECTOR_LOCAL_TIMEOUT")

    previous = signal.signal(signal.SIGALRM, timeout)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def main(argv=None):
    """実接続フラグやアダプタ実行機能を持たない報告コマンド。"""
    parser = ContractParser(description=__doc__)
    parser.add_argument("--offline-fixture", action="store_true")
    parser.add_argument("--fixture-id")
    parser.add_argument("--generation")
    parser.add_argument("--as-of-epoch", type=int, required=True)
    for name in ("desired", "registration", *DOMAINS):
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
        with deadline(5):
            _, result = collect(
                load_input(args.desired),
                load_input(args.registration),
                {key: load_input(getattr(args, key)) for key in DOMAINS},
                args.fixture_id,
                args.generation,
                args.as_of_epoch,
            )
        code = 0 if result["status"] == "observed_aligned" else 1
    except ContractError as error:
        result, code = {"status": "invalid", "code": str(error)}, 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
