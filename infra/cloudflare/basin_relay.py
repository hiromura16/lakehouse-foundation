"""KafkaのCDCを、選択列だけのBasinイベントへ変換・有限送信する。

読む順序は load_policy → project_event → deliver → run。
planは設定検査のみで、Kafka参加もHTTP送信も行わない。applyは専用の
consumer groupを使い、HTTP受付成功後だけKafkaの位置を同期確定する。
HTTP成功はIcebergへの反映完了ではない。HTTP成功後・commit前の障害では
再送が起こるため、後段ではevent_idによる重複処理が必要である。
"""

import argparse
import hashlib
import json
import os
import re
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_BATCH_BYTES = 256 * 1024
MAX_BATCH_RECORDS = 100
NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
COLUMN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,127}$")
SENSITIVE_COLUMN = re.compile(
    r"password|passwd|secret|token|api_key|connection_string|"
    r"server_ip|host_ip",
    re.IGNORECASE,
)
KAFKA_KEYS = {
    "bootstrap.servers",
    "security.protocol",
    "sasl.mechanism",
    "sasl.username",
    "sasl.password",
    "ssl.ca.location",
    "ssl.certificate.location",
    "ssl.key.location",
    "ssl.key.password",
}


class RelayError(ValueError):
    """接続先・原文・秘密値を含めず、固定エラーコードだけを運ぶ。"""


def strict_json(raw: bytes | str):
    """重複キー、非有限数、巨大入力を拒否し、曖昧なCDCを確定しない。"""
    if len(raw) > MAX_MESSAGE_BYTES:
        raise RelayError("INPUT_TOO_LARGE")

    def unique(pairs):
        """後勝ちの重複キーで別の値を隠せないようにする。"""
        result = {}
        for key, value in pairs:
            if key in result:
                raise RelayError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def reject_constant(_value):
        """JSONに定義されないNaN/Infinityを明示的に拒否する。"""
        raise RelayError("NON_FINITE_JSON")

    try:
        return json.loads(
            raw, object_pairs_hook=unique, parse_constant=reject_constant
        )
    except (UnicodeError, ValueError, RecursionError):
        raise RelayError("INVALID_JSON") from None


def private_bytes(path: Path, maximum: int) -> bytes:
    """Git外の本人所有0600ファイルだけを、値を表示せずに読み取る。"""
    path = path.expanduser().absolute()
    if path.resolve().is_relative_to(ROOT) or any(
        item.is_symlink() for item in (path, *path.parents)
    ):
        raise RelayError("PRIVATE_PATH_REQUIRED")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > maximum
            ):
                raise RelayError("PRIVATE_FILE_REQUIRED")
            result = source.read(maximum + 1)
    except OSError:
        raise RelayError("PRIVATE_FILE_UNAVAILABLE") from None
    if len(result) > maximum:
        raise RelayError("PRIVATE_FILE_TOO_LARGE")
    return result


def validate_endpoint(endpoint: str) -> str:
    """認証情報を送る先を、公式のHTTPS ingestホストだけに限定する。"""
    try:
        parts = urlsplit(endpoint)
        valid = (
            parts.scheme == "https"
            and re.fullmatch(
                r"[a-z0-9][a-z0-9-]{0,62}\.ingest\.cloudflare\.com",
                parts.hostname or "",
            )
            and parts.port in {None, 443}
            and parts.username is None
            and parts.password is None
            and parts.path in {"", "/"}
            and not parts.query
            and not parts.fragment
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise RelayError("BASIN_ENDPOINT_INVALID")
    return endpoint


@dataclass(frozen=True)
class TopicPolicy:
    """実topicをクラウドへ出さず、論理名・表・選択列を保持する。"""

    alias: str
    table: str
    columns: tuple[str, ...]
    key_columns: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    """接続情報はオンプレに保持し、送信できる業務列の境界を定義する。"""

    source_id: str
    capture_epoch: str
    group_id: str
    endpoint: str
    topics: dict[str, TopicPolicy]
    kafka: dict[str, str]
    initial_offset_policy: str = "error"


def load_policy(path: Path) -> Policy:
    """厳密なprivate JSONを検査する。DB・Kafka・Cloudflareへ接続しない。"""
    value = strict_json(private_bytes(path, MAX_MESSAGE_BYTES))
    required = {
        "source_id",
        "capture_epoch",
        "group_id",
        "endpoint",
        "topics",
        "kafka",
    }
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or set(value) - required - {"initial_offset_policy"}
        or any(
            not isinstance(value[key], str) or not NAME.fullmatch(value[key])
            for key in ("source_id", "capture_epoch")
        )
        or not isinstance(value["group_id"], str)
        or not re.fullmatch(
            r"lakehouse-basin-[a-zA-Z0-9_-]{1,80}", value["group_id"]
        )
        or value.get("initial_offset_policy", "error")
        not in {"error", "earliest"}
    ):
        raise RelayError("POLICY_INVALID")
    endpoint = validate_endpoint(value["endpoint"])
    kafka = value["kafka"]
    topics = value["topics"]
    if (
        not isinstance(kafka, dict)
        or "bootstrap.servers" not in kafka
        or set(kafka) - KAFKA_KEYS
        or any(not isinstance(v, str) or not v for v in kafka.values())
        or not isinstance(topics, dict)
        or not 1 <= len(topics) <= 50
    ):
        raise RelayError("SOURCE_POLICY_INVALID")
    result = {}
    aliases = set()
    for topic, item in topics.items():
        if (
            not isinstance(topic, str)
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,249}", topic)
            or not isinstance(item, dict)
            or set(item) != {"alias", "table", "columns", "key_columns"}
            or any(
                not isinstance(item[key], str) or not NAME.fullmatch(item[key])
                for key in ("alias", "table")
            )
        ):
            raise RelayError("TOPIC_POLICY_INVALID")
        for key in ("columns", "key_columns"):
            columns = item[key]
            if (
                not isinstance(columns, list)
                or not 1 <= len(columns) <= 128
                or any(
                    not isinstance(column, str)
                    or not COLUMN.fullmatch(column)
                    or SENSITIVE_COLUMN.search(column)
                    for column in columns
                )
                or len(set(columns)) != len(columns)
            ):
                raise RelayError("COLUMN_POLICY_INVALID")
        if (
            not set(item["key_columns"]).issubset(item["columns"])
            or item["alias"] in aliases
        ):
            raise RelayError("TOPIC_IDENTITY_INVALID")
        aliases.add(item["alias"])
        result[topic] = TopicPolicy(
            item["alias"],
            item["table"],
            tuple(item["columns"]),
            tuple(item["key_columns"]),
        )
    return Policy(
        value["source_id"],
        value["capture_epoch"],
        value["group_id"],
        endpoint,
        result,
        kafka,
        value.get("initial_offset_policy", "error"),
    )


def unwrap(raw: bytes | str):
    """schema付き・なしのDebezium JSONを同じpayloadとして扱う。"""
    value = strict_json(raw)
    if isinstance(value, dict) and "payload" in value:
        value = value["payload"]
    return value


def project_event(message, policy: Policy) -> dict:
    """主キーと選択列だけを残し、実topic・DB名・接続先を送信しない。"""
    topic = message.topic()
    if topic not in policy.topics:
        raise RelayError("TOPIC_NOT_ALLOWED")
    partition, offset = message.partition(), message.offset()
    if any(type(v) is not int or v < 0 for v in (partition, offset)):
        raise RelayError("SOURCE_POSITION_INVALID")
    selected = policy.topics[topic]
    if message.key() is None:
        raise RelayError("PRIMARY_KEY_MISSING")
    key = unwrap(message.key())
    if not isinstance(key, dict) or any(
        column not in key or key[column] is None
        for column in selected.key_columns
    ):
        raise RelayError("PRIMARY_KEY_MISSING")
    key = {column: key[column] for column in selected.key_columns}
    body = None if message.value() is None else unwrap(message.value())
    if body is not None and not isinstance(body, dict):
        raise RelayError("ENVELOPE_INVALID")
    operation = "tombstone" if body is None else body.get("op")
    if operation not in {"c", "u", "d", "r", "tombstone"}:
        raise RelayError("OPERATION_INVALID")

    def row(name):
        """削除前・更新後を混ぜず、選択した業務列だけを射影する。"""
        value = None if body is None else body.get(name)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RelayError("ROW_INVALID")
        return {
            column: value[column]
            for column in selected.columns
            if column in value
        }

    before, after = row("before"), row("after")
    if operation in {"c", "u", "r"} and after is None:
        raise RelayError("CURRENT_ROW_MISSING")
    identity = json.dumps(
        [
            policy.source_id,
            policy.capture_epoch,
            selected.alias,
            partition,
            offset,
        ],
        separators=(",", ":"),
    ).encode()
    return {
        "event_id": hashlib.sha256(identity).hexdigest(),
        "source_id": policy.source_id,
        "capture_epoch": policy.capture_epoch,
        "source_topic": selected.alias,
        "source_table": selected.table,
        "partition": partition,
        "offset": offset,
        "operation": operation,
        "key_json": json.dumps(key, ensure_ascii=False, allow_nan=False),
        "before_json": json.dumps(before, ensure_ascii=False, allow_nan=False),
        "after_json": json.dumps(after, ensure_ascii=False, allow_nan=False),
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Bearer認証情報を別のoriginへ転送しない。"""

    def redirect_request(self, *_args, **_kwargs):
        """すべてのredirectを拒否し、元のHTTP応答を失敗として扱う。"""
        return None


class BasinHTTP:
    """公式の認証付きHTTP endpointへ、有界JSON配列を送信する。"""

    def __init__(self, endpoint: str, token: str):
        """標準TLS検証を使い、shell proxyや全体管理tokenは引き継がない。"""
        self.endpoint = validate_endpoint(endpoint)
        if not re.fullmatch(r"[!-~]{16,2048}", token):
            raise RelayError("SEND_TOKEN_INVALID")
        self.token = token
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def send(self, payload: bytes) -> None:
        """HTTP成功だけを受付確認とする。R2への反映完了は保証しない。"""
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                if response.status not in {200, 202, 204}:
                    raise RelayError("BASIN_SEND_REJECTED")
        except (OSError, urllib.error.URLError):
            raise RelayError("BASIN_SEND_UNCONFIRMED") from None


def deliver(
    messages: list, policy: Policy, sender, commit: Callable[[dict], None]
) -> int:
    """全件検査→HTTP受付→位置確定の順を守り、失敗時は先へ進めない。"""
    if not 1 <= len(messages) <= MAX_BATCH_RECORDS:
        raise RelayError("BATCH_RECORD_LIMIT")
    events = [project_event(message, policy) for message in messages]
    payload = json.dumps(
        events,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    if len(payload) > MAX_BATCH_BYTES:
        raise RelayError("BATCH_BYTE_LIMIT")
    positions = {}
    for message in messages:
        key = (message.topic(), message.partition())
        positions[key] = max(positions.get(key, 0), message.offset() + 1)
    sender.send(payload)
    commit(positions)
    return len(events)


def run(
    policy: Policy,
    sender: BasinHTTP,
    maximum: int,
    batch_size: int,
    idle_seconds: int,
) -> int:
    """専用groupで有限件を処理する。既存CDCのgroupや設定を変更しない。"""
    try:
        from confluent_kafka import Consumer, TopicPartition
    except ImportError:
        raise RelayError("KAFKA_DEPENDENCY_REQUIRED") from None
    if (
        any(
            type(value) is not int
            for value in (maximum, batch_size, idle_seconds)
        )
        or not 1 <= maximum <= 10000
        or not 1 <= batch_size <= MAX_BATCH_RECORDS
        or not 1 <= idle_seconds <= 300
    ):
        raise RelayError("RUN_LIMIT_INVALID")
    consumer = None
    completed = 0
    try:
        consumer = Consumer(
            {
                **policy.kafka,
                "group.id": policy.group_id,
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
                "auto.offset.reset": policy.initial_offset_policy,
                "allow.auto.create.topics": False,
                "isolation.level": "read_committed",
                "log_level": 0,
            }
        )
        consumer.subscribe(list(policy.topics))

        def commit(positions):
            """Basin受付後だけ次のoffsetを同期commitし、部分失敗も止める。"""
            result = consumer.commit(
                offsets=[
                    TopicPartition(topic, partition, offset)
                    for (topic, partition), offset in positions.items()
                ],
                asynchronous=False,
            )
            if any(partition.error is not None for partition in result):
                raise RelayError("KAFKA_COMMIT_UNCONFIRMED")

        last_message = time.monotonic()
        while completed < maximum:
            messages = consumer.consume(
                num_messages=min(batch_size, maximum - completed),
                timeout=1.0,
            )
            if not messages:
                if time.monotonic() - last_message >= idle_seconds:
                    break
                continue
            if any(message.error() is not None for message in messages):
                raise RelayError("KAFKA_READ_UNCONFIRMED")
            completed += deliver(messages, policy, sender, commit)
            last_message = time.monotonic()
    except RelayError:
        raise
    except Exception:
        # broker応答は接続先や認証情報を含み得るため、外へ反射しない。
        raise RelayError("RELAY_UNCONFIRMED") from None
    finally:
        if consumer is not None:
            try:
                consumer.close()
            except Exception:
                raise RelayError("KAFKA_CLOSE_UNCONFIRMED") from None
    return completed


def main() -> int:
    """既定のplanは接続せず、applyを明示した場合だけ有限の外部送信を行う。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Git外・本人所有0600の専用JSON。実接続情報はオンプレに保持",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        help="Git外0600のBasin送信専用token。apply時だけ必要",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="専用Kafka groupへの参加とCloudflareへの有限送信を実行",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=100,
        help="1回の全送信件数上限。1～10000、既定100",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="HTTP 1回の最大件数。1～100、既定50。上限256KiBも適用",
    )
    parser.add_argument(
        "--idle-seconds",
        type=int,
        default=10,
        help="新着がないときの終了待機秒数。1～300、既定10",
    )
    args = parser.parse_args()
    if not (
        1 <= args.max_records <= 10000
        and 1 <= args.batch_size <= MAX_BATCH_RECORDS
        and 1 <= args.idle_seconds <= 300
    ):
        parser.error("件数・待機時間を許可範囲で指定してください。")
    try:
        policy = load_policy(args.config)
        if not args.apply:
            print(
                json.dumps(
                    {
                        "mode": "plan",
                        "network_used": False,
                        "topics": len(policy.topics),
                        "max_records": args.max_records,
                        "initial_offset_policy": policy.initial_offset_policy,
                    }
                )
            )
            return 0
        if args.token_file is None:
            raise RelayError("SEND_TOKEN_FILE_REQUIRED")
        token = private_bytes(args.token_file, 4096).decode("ascii").strip()
        count = run(
            policy,
            BasinHTTP(policy.endpoint, token),
            args.max_records,
            args.batch_size,
            args.idle_seconds,
        )
        print(json.dumps({"mode": "apply", "accepted_records": count}))
        return 0
    except (RelayError, UnicodeError) as error:
        code = str(error) if isinstance(error, RelayError) else "TOKEN_INVALID"
        print(json.dumps({"ok": False, "code": code}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
