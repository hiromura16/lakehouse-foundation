#!/usr/bin/env python3
"""合成 CDC の状態と登録計画だけを比較する。適用機能は持たない。"""

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

# The CLI must not create Python cache files while comparing inputs.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

from cdc_contract import (  # noqa: E402
    MAX_BYTES,
    ContractError,
    ContractParser,
    compatibility,
    fields,
    require,
    validate,
)

VERSION = 1
FIXTURE_ID = r"fixture_[0-9a-f]{32}"
HEX_DIGEST = r"[0-9a-f]{64}"


def digest(document):
    """UTF-8 化した正規 JSON を一方向の照合値にする。"""
    payload = json.dumps(
        document, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def match(pattern, value):
    """型と ASCII 文字集合を決め、外部投入文字列を表示しない。"""
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def load_input(path):
    """呼出側が選んだ通常ファイルだけを開き、FIFO 等を拒否する。"""
    require(
        hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_NONBLOCK"),
        "SAFE_FILE_OPEN_UNAVAILABLE",
    )
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        with os.fdopen(os.open(path, flags), "rb") as handle:
            require(
                stat.S_ISREG(os.fstat(handle.fileno()).st_mode),
                "UNSAFE_INPUT_FILE",
            )
            data = handle.read(MAX_BYTES + 1)
    except OSError:
        raise ContractError("INPUT_FILE_UNREADABLE") from None
    require(len(data) <= MAX_BYTES, "INPUT_FILE_TOO_LARGE")

    def pairs(entries):
        """重複した状態・契約フィールドを上書きで隠さない。"""
        output = {}
        for key, value in entries:
            require(key not in output, "DUPLICATE_JSON_KEY")
            output[key] = value
        return output

    def unsupported(_):
        """JSON 標準外の数値や非整数を拒否する。"""
        raise ContractError("INVALID_JSON")

    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=unsupported,
            parse_float=unsupported,
        )
    except ContractError:
        raise
    except (ValueError, RecursionError):
        raise ContractError("INVALID_JSON") from None


def canonical_contract(document):
    """表・列は名前で整列させ、複合キー列だけは宣言順を保持する。"""
    source = validate(document)
    tables = sorted(source["tables"], key=lambda table: table["id"])
    return {
        "version": VERSION,
        "tables": [
            {
                **table,
                "columns": sorted(
                    table["columns"], key=lambda col: col["name"]
                ),
            }
            for table in tables
        ],
    }


def compile_spec(document):
    """許可列・トピック・出力を宣言から決定的に作る。登録はしない。

    戻り値は識別子入りの内部データ。CLI の出力には載せない。
    これは接続先、認証、稼働中 CDC capture、topic 作成の完全設定ではない。
    """
    doc = canonical_contract(document)
    groups, prefix_owner = {}, {}
    for table in doc["tables"]:
        if not table["enabled"]:
            continue
        source = table["source"]
        db, prefix = source["database"], source["topic_prefix"]
        require(
            prefix_owner.setdefault(prefix, db) == db,
            "TOPIC_PREFIX_REUSED_ACROSS_DATABASES",
        )
        group = groups.setdefault(
            (db, prefix),
            {
                "snapshot_policy": table["snapshot"],
                "table_filters": [],
                "column_filters": [],
                "routes": [],
            },
        )
        require(
            group["snapshot_policy"] == table["snapshot"],
            "MIXED_SNAPSHOT_POLICY",
        )
        source_table = source["schema"] + "." + source["table"]
        group["table_filters"].append(re.escape(source_table))
        group["column_filters"].extend(
            re.escape(source_table + "." + col["name"])
            for col in table["columns"]
        )
        group["routes"].append(
            {
                "id": table["id"],
                "source": db + "." + source_table,
                "primary_key": table["primary_key"],
                "topics": {
                    "canonical": prefix + "." + db + "." + source_table,
                    "legacy_alias": prefix + "." + source_table,
                },
                "columns": table["columns"],
                "destinations": table["destinations"],
                "mode": table["mode"],
                "delete_policy": table["delete_policy"],
            }
        )
    result = []
    for db, prefix in sorted(groups):
        group = groups[db, prefix]
        result.append(
            {
                "database.names": db,
                "topic.prefix": prefix,
                "snapshot_policy": group["snapshot_policy"],
                "table.include.list": ",".join(sorted(group["table_filters"])),
                "column.include.list": ",".join(
                    sorted(group["column_filters"])
                ),
                "routes": group["routes"],
            }
        )
    return {"version": VERSION, "connectors": result}


def match_observed(snapshot, fixture_id):
    """同じ合成対象に関する宣言と観測指紋だけを受け入れる。"""
    fields(
        snapshot,
        {
            "version",
            "environment",
            "fixture_id",
            "state",
            "registered_contract",
            "observed_spec_sha256",
        },
        "INVALID_OBSERVATION_FIELDS",
    )
    require(
        type(snapshot["version"]) is int and snapshot["version"] == VERSION,
        "INVALID_OBSERVATION_VERSION",
    )
    require(
        snapshot["environment"] == "synthetic"
        and match(FIXTURE_ID, fixture_id)
        and snapshot["fixture_id"] == fixture_id,
        "SYNTHETIC_SCOPE_MISMATCH",
    )
    state = snapshot["state"]
    require(
        state in ("present", "absent", "unknown"), "INVALID_OBSERVATION_STATE"
    )
    contract = snapshot["registered_contract"]
    observed = snapshot["observed_spec_sha256"]
    if state == "present":
        require(
            contract is not None and match(HEX_DIGEST, observed),
            "OBSERVATION_INCOMPLETE",
        )
        contract = canonical_contract(contract)
    else:
        require(
            contract is None and observed is None, "INCONSISTENT_OBSERVATION"
        )
    return state, contract, observed


def plan_steps(previous, desired):
    """作成・追加・許容される改訂だけを明示し、削除操作は生成しない。"""
    previous = (
        {item["id"]: item for item in previous["tables"]} if previous else {}
    )
    existing_groups = {
        (item["source"]["database"], item["source"]["topic_prefix"])
        for item in previous.values()
        if item["enabled"]
    }
    result = []
    for index, table in enumerate(desired["tables"], 1):
        if not table["enabled"]:
            continue
        old = previous.get(table["id"])
        if old is None:
            src = table["source"]
            if (src["database"], src["topic_prefix"]) in existing_groups:
                result.append(
                    {"op": "REVIEW_BACKFILL_AND_OFFSETS", "table": index}
                )
            result.append({"op": "REGISTER_TABLE", "table": index})
        elif table != old:
            result.append({"op": "UPDATE_TABLE", "table": index})
    if not result:
        return []
    return [
        {"op": "RECHECK_SCOPE_AND_OBSERVATION"},
        {"op": "PREFLIGHT_SOURCE_AND_CDC_READ_ONLY"},
        *result,
        {"op": "VERIFY_TOPICS_AND_DESTINATIONS"},
        {"op": "VERIFY_RETAINED_OFFSETS_AND_RECORD_RECEIPT"},
    ]


def assessment(desired, snapshot, fixture_id):
    """現在状態、差分と承認候補を同じ読み取り結果に結び付ける。"""
    desired = canonical_contract(desired)
    spec = compile_spec(desired)
    state, previous, observed = match_observed(snapshot, fixture_id)
    desired_hash = digest(desired)
    spec_hash = digest(spec)
    scope = digest({"environment": "synthetic", "fixture_id": fixture_id})
    previous_hash = digest(previous) if previous else None
    observation_hash = digest(
        {
            "scope": scope,
            "state": state,
            "registered_contract": previous_hash,
            "spec_sha256": observed,
        }
    )
    steps, blockers, issues = [], [], []
    if not spec["connectors"]:
        blockers.append("NO_ENABLED_TABLES")
    if state == "unknown":
        blockers.append("OBSERVATION_UNKNOWN")
    elif previous is not None:
        if digest(compile_spec(previous)) != observed:
            blockers.append("OBSERVED_SPEC_DRIFT")
        issues = compatibility(previous, desired)
        if issues:
            blockers.append("INCOMPATIBLE_CONTRACT_CHANGE")
    if not blockers:
        steps = plan_steps(previous, desired)
        if not steps and previous_hash != desired_hash:
            # Disabled declaration edits alone must not yield apply material.
            blockers.append("NO_ENABLED_CHANGE_TO_APPLY")
    token = (
        digest(
            {
                "operation_order_version": VERSION,
                "scope": scope,
                "observation": observation_hash,
                "desired_contract": desired_hash,
                "desired_spec": spec_hash,
                "steps": steps,
            }
        )
        if steps
        else None
    )
    return {
        "version": VERSION,
        "status": "blocked"
        if blockers
        else ("planned" if steps else "aligned"),
        "observation": state,
        "desired_table_count": sum(
            table["enabled"] for table in desired["tables"]
        ),
        "desired_connector_count": len(spec["connectors"]),
        "desired_contract_sha256": desired_hash,
        "desired_spec_sha256": spec_hash,
        "registered_contract_sha256": previous_hash,
        "observation_sha256": observation_hash,
        "blockers": blockers,
        "issues": issues,
        "steps": steps,
        "approval_candidate_sha256": token,
    }


def main(argv=None):
    """plan/status を識別子なし JSON と固定終了コードで返す。"""
    parser = ContractParser(description=__doc__)
    modes = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "status"):
        mode = modes.add_parser(name)
        mode.add_argument("--offline-fixture", action="store_true")
        mode.add_argument("--fixture-id")
        mode.add_argument("--desired", type=Path, required=True)
        mode.add_argument("--observed", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        require(
            args.offline_fixture and match(FIXTURE_ID, args.fixture_id),
            "EXPLICIT_SYNTHETIC_SCOPE_REQUIRED",
        )
        result = assessment(
            load_input(args.desired),
            load_input(args.observed),
            args.fixture_id,
        )
        if args.command == "status":
            result.pop("steps")
            result.pop("approval_candidate_sha256")
        status = 1 if result["status"] == "blocked" else 0
    except ContractError as error:
        result = {"status": "invalid", "code": str(error)}
        status = 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
