"""明示的な CDC 契約の配送を、接続せずに型とキーへ変換する。

裸 JSON・decimal string・SQL Server adaptive 時刻のみを受け入れる。
照合順序や CHAR の空白の同一視は推定しない。既存 Consumer は変更しない。
"""

import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from event_limits import MAX_RECORD_BYTES
from state import version

RECORD_FIELDS = {"topic", "partition", "offset", "key", "value", "ingested_at"}
ENVELOPE_FIELDS = {"op", "before", "after", "source"}
TRANSPORT_FIELDS = {"ts_ms", "ts_us", "ts_ns", "transaction"}
EPOCH = datetime(1970, 1, 1)
INT64 = 2**63


def require(condition, code):
    """値や生の例外を出さずに、固定診断だけを返す。"""
    if not condition:
        raise ValueError(code)


def unique_object(pairs):
    """配送のどの階層でも、重複した JSON キーを拒否する。"""
    result = {}
    for key, value in pairs:
        require(key not in result, "CDC_VALUE_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def unsupported_number(_):
    """配送に含まれる非整数を暗黙に丸めない。"""
    raise ValueError("CDC_VALUE_INVALID_JSON_NUMBER")


def parse(text):
    """スキーマ付き包みを推測せず、裸の JSON を読み取る。"""
    try:
        return json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_float=unsupported_number,
            parse_constant=unsupported_number,
        )
    except (json.JSONDecodeError, RecursionError):
        raise ValueError("CDC_VALUE_INVALID_JSON") from None
    except ValueError as error:
        if error.args in (
            ("CDC_VALUE_DUPLICATE_JSON_KEY",),
            ("CDC_VALUE_INVALID_JSON_NUMBER",),
        ):
            raise
        raise ValueError("CDC_VALUE_INVALID_JSON") from None


def exact_text(value, column, *, unicode_text):
    """空白は保存し、nchar相当でなく UTF-16 単位のnvarchar幅を測る。"""
    require(isinstance(value, str), "CDC_VALUE_INVALID_TEXT")
    try:
        units = len(value.encode("utf-16-le" if unicode_text else "ascii"))
    except UnicodeError:
        raise ValueError("CDC_VALUE_INVALID_TEXT") from None
    if unicode_text:
        units //= 2
    bound = column["width"]
    require(bound == "max" or units <= bound, "CDC_VALUE_TEXT_WIDTH")
    return value


def exact_decimal(value, column):
    """小数文字列を損失なく固定scaleに揃え、丸め・指数は許さない。"""
    require(
        isinstance(value, str)
        and len(value) <= 41
        and re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value)
        is not None,
        "CDC_VALUE_INVALID_DECIMAL",
    )
    integer, _, fraction = value.lstrip("-").partition(".")
    scale, precision = column["scale"], column["precision"]
    require(
        len(integer.lstrip("0")) <= precision - scale
        and len(fraction) <= scale,
        "CDC_VALUE_DECIMAL_PRECISION",
    )
    exact = Decimal(value)
    # SQL decimal のゼロは符号で区別しない。その他は固定scaleへゼロ補完。
    return format(exact.copy_abs() if exact.is_zero() else exact, f".{scale}f")


def exact_date(value):
    """adaptive の日数と完全一致の ISO 日付だけを受け入れる。"""
    try:
        if type(value) is int and -(2**31) <= value < 2**31:
            return (EPOCH.date() + timedelta(days=value)).isoformat()
        if isinstance(value, str) and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value
        ):
            return date.fromisoformat(value).isoformat()
    except (ValueError, OverflowError):
        pass
    raise ValueError("CDC_VALUE_INVALID_DATE")


def exact_timestamp(value, column):
    """宣言scaleからepoch単位を決め、7桁目も整数演算で保持する。"""
    require(
        type(value) is int and -INT64 <= value < INT64,
        "CDC_VALUE_INVALID_TIMESTAMP",
    )
    scale = column["scale"]
    wire_digits = 3 if scale <= 3 else 6 if scale <= 6 else 9
    seconds, fractional = divmod(value, 10**wire_digits)
    quantum = 10 ** (wire_digits - scale)
    require(fractional % quantum == 0, "CDC_VALUE_TIMESTAMP_PRECISION")
    try:
        head = (EPOCH + timedelta(seconds=seconds)).isoformat(
            timespec="seconds"
        )
    except (OverflowError, ValueError):
        raise ValueError("CDC_VALUE_INVALID_TIMESTAMP") from None
    if not scale:
        return head
    return head + "." + str(fractional // quantum).zfill(scale)


def scalar(value, column):
    """登録済みSQL型の範囲だけを返し、文字や数値を推測しない。"""
    if value is None:
        require(column["nullable"], "CDC_VALUE_NULL_NOT_ALLOWED")
        return None
    kind = column["type"]
    if kind in ("nvarchar", "char", "varchar"):
        return exact_text(value, column, unicode_text=kind == "nvarchar")
    if kind in ("int", "bigint"):
        limit = 2**31 if kind == "int" else INT64
        require(
            type(value) is int and -limit <= value < limit,
            "CDC_VALUE_INTEGER_RANGE",
        )
        return value
    if kind == "bit":
        require(type(value) is bool, "CDC_VALUE_INVALID_BIT")
        return value
    if kind == "decimal":
        return exact_decimal(value, column)
    if kind == "date":
        return exact_date(value)
    if kind == "datetime2":
        return exact_timestamp(value, column)
    raise ValueError("CDC_VALUE_UNSUPPORTED_TYPE")


def exact_position(record, source):
    """topic の完全一致と、有界な Kafka 順序情報を検査する。"""
    base = source["topic_prefix"]
    suffix = source["schema"] + "." + source["table"]
    require(
        record["topic"]
        in (
            base + "." + suffix,
            base + "." + source["database"] + "." + suffix,
        ),
        "CDC_VALUE_TOPIC_MISMATCH",
    )
    require(
        type(record["partition"]) is int
        and 0 <= record["partition"] < 2**31
        and type(record["offset"]) is int
        and 0 <= record["offset"] < INT64
        and isinstance(record["ingested_at"], str)
        and 0 < len(record["ingested_at"]) <= 64,
        "CDC_VALUE_INVALID_POSITION",
    )


def event_version(envelope, offset):
    """既存 LSN 順序を再利用し、serial の欠損・曖昧な型は拒否する。"""
    if envelope["op"] != "r":
        serial = envelope["source"].get("event_serial_no")
        require(
            (
                type(serial) is int
                or isinstance(serial, str)
                and re.fullmatch(r"[0-9]{1,19}", serial) is not None
            )
            and 0 <= int(serial) < INT64,
            "CDC_VALUE_INVALID_VERSION",
        )
    try:
        return version(envelope, offset)
    except ValueError:
        raise ValueError("CDC_VALUE_INVALID_VERSION") from None


def validate_transport(envelope):
    """座標外の transport 情報も、既知の有限な形だけを許す。"""
    require(
        all(
            envelope[name] is None
            or type(envelope[name]) is int
            and -INT64 <= envelope[name] < INT64
            for name in ("ts_ms", "ts_us", "ts_ns")
            if name in envelope
        ),
        "CDC_VALUE_TRANSPORT_METADATA",
    )
    transaction = envelope.get("transaction")
    if transaction is not None:
        require(
            isinstance(transaction, dict)
            and set(transaction)
            == {"id", "total_order", "data_collection_order"}
            and isinstance(transaction["id"], str)
            and 0 < len(transaction["id"]) <= 256
            and all(
                type(transaction[name]) is int
                and 0 < transaction[name] < INT64
                for name in ("total_order", "data_collection_order")
            ),
            "CDC_VALUE_TRANSPORT_METADATA",
        )


def exact_image(image, columns, key_columns, key_values):
    """全列の型を検査し、キーが画像と異なる配送を拒否する。"""
    require(
        isinstance(image, dict) and set(image) == set(columns),
        "CDC_VALUE_IMAGE_FIELDS",
    )
    row = {name: scalar(image[name], spec) for name, spec in columns.items()}
    require(
        [row[name] for name in key_columns] == key_values,
        "CDC_VALUE_KEY_IMAGE_MISMATCH",
    )
    return row


def decode(table, record):
    """検証済みの一表と配送から op、順序付きkey、現在行、版を返す。

    DELETE は検証した before を新しい行として返さない。
    tombstone はキーだけ検証し、current操作と版は作らない。
    """
    require(
        isinstance(record, dict) and set(record) == RECORD_FIELDS,
        "CDC_VALUE_RECORD_FIELDS",
    )
    source = table["source"]
    exact_position(record, source)
    key, payload = record["key"], record["value"]
    require(
        isinstance(key, str) and (payload is None or isinstance(payload, str)),
        "CDC_VALUE_INVALID_WIRE",
    )
    require(
        len(key) + len(payload or "") <= MAX_RECORD_BYTES,
        "CDC_VALUE_WIRE_LIMIT",
    )
    try:
        size = len(key.encode("utf-8")) + len((payload or "").encode("utf-8"))
    except UnicodeError:
        raise ValueError("CDC_VALUE_INVALID_WIRE") from None
    require(size <= MAX_RECORD_BYTES, "CDC_VALUE_WIRE_LIMIT")
    decoded = parse(key)
    key_columns = table["primary_key"]
    columns = {column["name"]: column for column in table["columns"]}
    require(
        isinstance(decoded, dict)
        and set(decoded) == set(key_columns)
        and all(decoded[name] is not None for name in key_columns),
        "CDC_VALUE_KEY_FIELDS",
    )
    key_values = [scalar(decoded[name], columns[name]) for name in key_columns]
    canonical_key = json.dumps(
        key_values, ensure_ascii=True, separators=(",", ":")
    )
    if payload is None:
        return "tombstone", canonical_key, None, None
    envelope = parse(payload)
    require(
        isinstance(envelope, dict)
        and ENVELOPE_FIELDS <= set(envelope)
        and set(envelope) <= ENVELOPE_FIELDS | TRANSPORT_FIELDS,
        "CDC_VALUE_ENVELOPE_FIELDS",
    )
    validate_transport(envelope)
    op = envelope["op"]
    require(op in ("r", "c", "u", "d"), "CDC_VALUE_UNSUPPORTED_OPERATION")
    origin = envelope["source"]
    require(
        isinstance(origin, dict)
        and all(
            type(val) in (int, bool, str, type(None))
            for val in origin.values()
        )
        and tuple(origin.get(key) for key in ("db", "schema", "table"))
        == tuple(source[key] for key in ("database", "schema", "table")),
        "CDC_VALUE_SOURCE_MISMATCH",
    )
    before, after = envelope["before"], envelope["after"]
    if op in ("r", "c"):
        require(before is None, "CDC_VALUE_IMAGE_FIELDS")
    else:
        exact_image(before, columns, key_columns, key_values)
    row = None
    if op == "d":
        require(after is None, "CDC_VALUE_IMAGE_FIELDS")
    else:
        row = exact_image(after, columns, key_columns, key_values)
    return op, canonical_key, row, event_version(envelope, record["offset"])
