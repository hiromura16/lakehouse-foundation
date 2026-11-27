#!/usr/bin/env python3
"""CDC 宣言を接続・実行なしで検査し、互換性差分を判定する。"""

import argparse
import json
import re
from pathlib import Path

IDENT = r"[a-z][a-z0-9_]{0,62}"
PREFIX = r"[a-z][a-z0-9_-]{0,62}"
MAX_BYTES = 256 * 1024
MAX_TABLES = 128
MAX_COLUMNS = 256
MAX_PRIMARY_KEY_COLUMNS = 32
MAX_TOPIC_LENGTH = 249
SYSTEM_DATABASES = {"master", "model", "msdb", "tempdb"}
WIDTHS = {"nvarchar": 4000, "char": 8000, "varchar": 8000}
SIMPLE = {"bigint", "int", "bit", "date"}
EXTRAS = {
    **dict.fromkeys(WIDTHS, {"width"}),
    "decimal": {"precision", "scale"},
    "datetime2": {"scale"},
}
TABLE_FIELDS = {
    "id",
    "enabled",
    "source",
    "primary_key",
    "mode",
    "destinations",
    "columns",
    "delete_policy",
    "snapshot",
}


class ContractError(ValueError):
    """固定診断コードを保持し、入力文字列やパスを出力しない。"""


class ContractParser(argparse.ArgumentParser):
    """CLI の誤った引数を公開診断へ引用しない。"""

    def error(self, _message):
        """標準の echo を避け、使用法エラーを固定コードにする。"""
        raise ContractError("INVALID_USAGE")


def require(condition, code):
    """条件不成立なら固定コードで検査を中止する。"""
    if not condition:
        raise ContractError(code)


def read_document(path):
    """上限のある UTF-8 JSON を読み、曖昧な重複キーは拒否する。"""

    def pairs(entries):
        result = {}
        for key, value in entries:
            require(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def bad_constant(_):
        raise ContractError("INVALID_JSON")

    require(path.is_file(), "CONTRACT_FILE_UNREADABLE")
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
    except OSError:
        raise ContractError("CONTRACT_FILE_UNREADABLE") from None
    require(len(raw) <= MAX_BYTES, "CONTRACT_FILE_TOO_LARGE")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=bad_constant,
        )
    except ContractError:
        raise
    except (ValueError, RecursionError):
        raise ContractError("INVALID_JSON") from None


def fields(value, keys, code):
    """宣言ごとの必須・許可キーを完全一致で検査する。"""
    require(isinstance(value, dict) and set(value) == keys, code)


def identifier(value, pattern=IDENT):
    """識別子の ASCII・長さ・文字集合を固定する。"""
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def bounded_int(value, low, high):
    """bool を整数として受理せず、整数の閉区間を検査する。"""
    return type(value) is int and low <= value <= high


def validate_column(column):
    """型ごとの属性と、SQL Server の寸法制約を検査する。"""
    require(isinstance(column, dict), "INVALID_COLUMN")
    kind = column.get("type")
    require(
        isinstance(kind, str) and kind in SIMPLE | EXTRAS.keys(),
        "INVALID_COLUMN_TYPE",
    )
    fields(
        column,
        {"name", "type", "nullable"} | EXTRAS.get(kind, set()),
        "INVALID_COLUMN_FIELDS",
    )
    require(
        identifier(column["name"]) and type(column["nullable"]) is bool,
        "INVALID_COLUMN",
    )
    if kind in WIDTHS:
        width = column["width"]
        require(
            bounded_int(width, 1, WIDTHS[kind])
            or (kind != "char" and width == "max"),
            "INVALID_COLUMN_WIDTH",
        )
    if kind == "decimal":
        precision, scale = column["precision"], column["scale"]
        require(
            bounded_int(precision, 1, 38) and bounded_int(scale, 0, precision),
            "INVALID_DECIMAL_SHAPE",
        )
    if kind == "datetime2":
        require(bounded_int(column["scale"], 0, 7), "INVALID_DATETIME_SCALE")


def validate_table(table):
    """単一テーブルの入力、キー順、書き込み方針を検査する。"""
    fields(table, TABLE_FIELDS, "INVALID_TABLE_FIELDS")
    require(
        identifier(table["id"]) and type(table["enabled"]) is bool,
        "INVALID_TABLE_IDENTITY",
    )
    source = table["source"]
    fields(
        source,
        {"database", "schema", "table", "topic_prefix"},
        "INVALID_SOURCE_FIELDS",
    )
    require(
        all(identifier(source[key]) for key in ("database", "schema", "table"))
        and identifier(source["topic_prefix"], PREFIX),
        "INVALID_SOURCE_IDENTITY",
    )
    require(source["database"] not in SYSTEM_DATABASES, "SYSTEM_DATABASE")
    require(table["mode"] in ("current", "append"), "INVALID_MODE")
    allowed = (
        ("remove", "retain") if table["mode"] == "current" else ("record",)
    )
    require(table["delete_policy"] in allowed, "INVALID_DELETE_POLICY")
    require(
        table["snapshot"] in ("initial", "never"), "INVALID_SNAPSHOT_POLICY"
    )
    destinations = table["destinations"]
    fields(
        destinations,
        {"raw", "current", "silver"},
        "INVALID_DESTINATION_FIELDS",
    )
    for kind, entry in destinations.items():
        names = {"namespace", "table"} | (
            {"stream"} if kind == "raw" else set()
        )
        fields(entry, names, "INVALID_DESTINATION_FIELDS")
        require(
            all(identifier(entry[key]) for key in names),
            "INVALID_DESTINATION_IDENTITY",
        )
    columns = table["columns"]
    require(
        isinstance(columns, list) and 1 <= len(columns) <= MAX_COLUMNS,
        "INVALID_COLUMNS",
    )
    known = {}
    for col in columns:
        validate_column(col)
        require(col["name"] not in known, "DUPLICATE_COLUMN")
        known[col["name"]] = col
    key = table["primary_key"]
    require(
        isinstance(key, list)
        and 1 <= len(key) <= MAX_PRIMARY_KEY_COLUMNS
        and all(identifier(name) for name in key),
        "INVALID_PRIMARY_KEY",
    )
    require(len(set(key)) == len(key), "DUPLICATE_PRIMARY_KEY")
    require(
        all(name in known and not known[name]["nullable"] for name in key),
        "INVALID_PRIMARY_KEY_COLUMN",
    )
    require(
        all(known[name].get("width") != "max" for name in key),
        "LOB_PRIMARY_KEY_COLUMN",
    )


def validate(document):
    """全宣言を照合し、例外のない出力衝突・名前の取り違えを拒否する。"""
    fields(document, {"version", "tables"}, "INVALID_CONTRACT_FIELDS")
    require(
        type(document["version"]) is int and document["version"] == 1,
        "UNSUPPORTED_CONTRACT_VERSION",
    )
    tables = document["tables"]
    require(
        isinstance(tables, list) and 1 <= len(tables) <= MAX_TABLES,
        "INVALID_TABLES",
    )
    seen, outputs, streams = set(), {}, {}
    registry, aliases = set(), set()
    for table in tables:
        validate_table(table)
        require(table["id"] not in seen, "DUPLICATE_TABLE_ID")
        seen.add(table["id"])
        source = table["source"]
        database, schema, name, prefix = (
            source[key]
            for key in ("database", "schema", "table", "topic_prefix")
        )
        physical = (database, schema, name)
        require(physical not in registry, "DUPLICATE_PHYSICAL_SOURCE")
        registry.add(physical)
        topics = {(prefix, schema, name), (prefix, *physical)}
        require(
            all(len(".".join(topic)) <= MAX_TOPIC_LENGTH for topic in topics),
            "TOPIC_ALIAS_TOO_LONG",
        )
        require(not (topics & aliases), "DUPLICATE_TOPIC_ALIAS")
        aliases.update(topics)
        for kind, dest in table["destinations"].items():
            output = dest["namespace"], dest["table"]
            stream = dest.get("stream")
            prior = outputs.get(output)
            if prior is not None:
                require(
                    kind == "raw" and prior == (kind, stream),
                    "DUPLICATE_DESTINATION",
                )
            outputs[output] = (kind, stream)
            if kind == "raw":
                require(
                    streams.setdefault(stream, output) == output,
                    "RAW_STREAM_DESTINATION_CHANGED",
                )
    return document


def widened(old, new):
    """旧値の全域が同じ SQL 型の新しい寸法に収まるかを判定する。"""
    if old["type"] != new["type"] or (old["nullable"] and not new["nullable"]):
        return False
    kind = old["type"]
    if kind in WIDTHS:
        a, b = old["width"], new["width"]
        return b == "max" or (a != "max" and b >= a)
    if kind == "decimal":
        return (
            new["scale"] >= old["scale"]
            and new["precision"] - new["scale"]
            >= old["precision"] - old["scale"]
        )
    if kind == "datetime2":
        return new["scale"] >= old["scale"]
    return True


def compatibility(old, new):
    """非互換変更を入力値ではなく固定コードと位置で示す。"""
    issues = []
    by_id = {item["id"]: item for item in new["tables"]}
    for table_index, previous in enumerate(old["tables"], 1):
        current = by_id.get(previous["id"])

        def issue(code, column=None):
            item = {"code": code, "table": table_index}
            if column is not None:
                item["column"] = column
            issues.append(item)

        if current is None:
            issue("TABLE_REMOVED")
            continue
        for key, code in (
            ("enabled", "ENABLED_CHANGED"),
            ("source", "SOURCE_CHANGED"),
            ("primary_key", "PRIMARY_KEY_CHANGED"),
            ("mode", "MODE_CHANGED"),
            ("destinations", "DESTINATION_CHANGED"),
            ("delete_policy", "DELETE_POLICY_CHANGED"),
            ("snapshot", "SNAPSHOT_POLICY_CHANGED"),
        ):
            if previous[key] != current[key]:
                issue(code)
        columns = {col["name"]: col for col in current["columns"]}
        old_names = {col["name"] for col in previous["columns"]}
        for index, col in enumerate(previous["columns"], 1):
            candidate = columns.get(col["name"])
            if candidate is None:
                issue("COLUMN_REMOVED", index)
            elif col["name"] in previous["primary_key"] and col != candidate:
                issue("PRIMARY_KEY_COLUMN_CHANGED", index)
            elif not widened(col, candidate):
                issue("COLUMN_NARROWED_OR_CHANGED", index)
        for index, col in enumerate(current["columns"], 1):
            if col["name"] not in old_names and not col["nullable"]:
                issue("REQUIRED_COLUMN_ADDED", index)
    return issues


def main(argv=None):
    """ファイルの内容を反映せず、固定 JSON と検査結果コードを返す。"""
    parser = ContractParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate").add_argument("contract", type=Path)
    diff = commands.add_parser("diff")
    diff.add_argument("old", type=Path)
    diff.add_argument("new", type=Path)
    try:
        args = parser.parse_args(argv)
    except ContractError:
        print('{"code": "INVALID_USAGE", "status": "invalid"}')
        return 2
    try:
        if args.command == "validate":
            doc = validate(read_document(args.contract))
            result = {"status": "valid", "table_count": len(doc["tables"])}
        else:
            old = validate(read_document(args.old))
            new = validate(read_document(args.new))
            issues = compatibility(old, new)
            result = {
                "status": "incompatible" if issues else "compatible",
                "issues": issues,
            }
    except ContractError as error:
        result = {"status": "invalid", "code": str(error)}
    except OSError:
        result = {"status": "invalid", "code": "CONTRACT_FILE_UNREADABLE"}
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 1 if result["status"] in {"invalid", "incompatible"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
