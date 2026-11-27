"""継続適用しない契約デコーダを合成 Kafka レコードで検証する。"""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra/cdc"))
from contract_values import decode  # noqa: E402


def column(name, kind, *, nullable=False, **shape):
    """テスト用の検証済み列宣言を作る。"""
    return {"name": name, "type": kind, "nullable": nullable, **shape}


TABLE = {
    "source": {
        "database": "fixture_db",
        "schema": "fixture_schema",
        "table": "items",
        "topic_prefix": "fixture-stream",
    },
    "primary_key": ["tenant", "id"],
    "columns": [
        column("tenant", "char", width=5),
        column("id", "bigint"),
        column("label", "nvarchar", width=4),
        column("optional", "varchar", width=8, nullable=True),
        column("total", "decimal", precision=38, scale=7),
        column("flag", "bit"),
        column("count", "int"),
        column("day", "date"),
        column("stamp", "datetime2", scale=7),
    ],
}

ROW = {
    "tenant": " A ",
    "id": -(2**63),
    "label": "🍎🍏",
    "optional": "",
    "total": "1234567890123456789012345678901.1234567",
    "flag": False,
    "count": 2**31 - 1,
    "day": -1,
    "stamp": -100,
}


def envelope(op="u"):
    """登録境界に合う bare JSON の配送を用意する。"""
    return {
        "op": op,
        "source": {
            "db": "fixture_db",
            "schema": "fixture_schema",
            "table": "items",
            "commit_lsn": "00000001:00000001:0001",
            "change_lsn": "00000001:00000001:0002",
            "event_serial_no": "3",
        },
        "before": dict(ROW) if op in ("u", "d") else None,
        "after": dict(ROW) if op != "d" else None,
    }


def record(event):
    """配送座標と key JSON を作る。key の挿入順と宣言順は逆。"""
    return {
        "topic": "fixture-stream.fixture_db.fixture_schema.items",
        "partition": 0,
        "offset": 91,
        "ingested_at": "2026-01-01T00:00:00Z",
        "key": json.dumps({"id": ROW["id"], "tenant": ROW["tenant"]}),
        "value": json.dumps(event) if event is not None else None,
    }


class ContractValuesTests(unittest.TestCase):
    """型、時刻精度、配送元とキーを完全一致で検査する。"""

    def test_full_row_preserves_empty_utf16_decimal_and_temporal_precision(
        self,
    ):
        op, key, row, version = decode(TABLE, record(envelope()))
        self.assertEqual(op, "u")
        self.assertEqual(json.loads(key), [ROW["tenant"], ROW["id"]])
        self.assertEqual(row["optional"], "")
        self.assertEqual(row["label"], "🍎🍏")
        self.assertEqual(row["total"], ROW["total"])
        self.assertEqual(row["day"], "1969-12-31")
        self.assertEqual(row["stamp"], "1969-12-31T23:59:59.9999999")
        self.assertEqual(version, [1, 1, 1, 1, 1, 1, 2, 3])

    def test_snapshot_create_delete_and_tombstone_have_distinct_events(self):
        for op in ("r", "c", "d"):
            with self.subTest(op=op):
                action, _, row, ver = decode(TABLE, record(envelope(op)))
                self.assertEqual(action, op)
                self.assertEqual(row is None, op == "d")
                if op == "r":
                    self.assertEqual(ver, [0, 0, 0, 0, 0, 0, 0, 91])
        message = record(None)
        action, _, row, ver = decode(TABLE, message)
        self.assertEqual((action, row, ver), ("tombstone", None, None))
        message["key"] = '{"id": null, "tenant": " A "}'
        with self.assertRaisesRegex(ValueError, "^CDC_VALUE_KEY_FIELDS$"):
            decode(TABLE, message)

    def test_millis_micros_and_nanos_follow_declared_scale(self):
        for scale, value, expected in (
            (0, 1000, "1970-01-01T00:00:01"),
            (3, 1001, "1970-01-01T00:00:01.001"),
            (4, 1000100, "1970-01-01T00:00:01.0001"),
            (6, 1000001, "1970-01-01T00:00:01.000001"),
            (7, 1000000100, "1970-01-01T00:00:01.0000001"),
        ):
            contract = copy.deepcopy(TABLE)
            contract["columns"][-1]["scale"] = scale
            event = envelope("c")
            event["after"]["stamp"] = value
            self.assertEqual(
                decode(contract, record(event))[2]["stamp"], expected
            )
        event = envelope("c")
        event["after"]["stamp"] = 1  # Cannot silently round one nanosecond.
        with self.assertRaisesRegex(
            ValueError, "^CDC_VALUE_TIMESTAMP_PRECISION$"
        ):
            decode(TABLE, record(event))

    def test_null_is_not_empty_and_decimal_is_not_rounded(self):
        event = envelope("c")
        event["after"].update(optional=None, total="1.2", day="2024-02-29")
        row = decode(TABLE, record(event))[2]
        self.assertIsNone(row["optional"])
        self.assertEqual(row["total"], "1.2000000")
        self.assertEqual(row["day"], "2024-02-29")
        for value in (1, "1e2", "+1", " 1", "1.00000001"):
            event["after"]["total"] = value
            with self.assertRaisesRegex(ValueError, "^CDC_VALUE_.*DECIMAL"):
                decode(TABLE, record(event))

    def test_declared_values_reject_implicit_coercion_and_overflow(self):
        for name, value in (
            ("count", True),
            ("count", 2**31),
            ("flag", 1),
            ("label", "🍎🍏x"),
            ("optional", "é"),
            ("id", 2**63),
            ("day", "2023-02-29"),
            ("stamp", "2026-01-01T00:00:00.0000000"),
            ("label", "\ud800"),
        ):
            with self.subTest(name=name):
                event = envelope("c")
                event["after"][name] = value
                with self.assertRaisesRegex(ValueError, "^CDC_VALUE_[A-Z_]+$"):
                    decode(TABLE, record(event))

    def test_previous_and_next_images_and_origin_are_never_guessed(self):
        def denied(event, code):
            with self.assertRaisesRegex(ValueError, "^" + code + "$"):
                decode(TABLE, record(event))

        event = envelope()
        event["before"]["id"] = 17
        denied(event, "CDC_VALUE_KEY_IMAGE_MISMATCH")
        event = envelope()
        event["after"]["private_fixture"] = []
        denied(event, "CDC_VALUE_IMAGE_FIELDS")
        event = envelope()
        event["source"]["db"] = "private_fixture"
        denied(event, "CDC_VALUE_SOURCE_MISMATCH")
        for token in (True, "-1", "١", 2**63):
            event = envelope()
            event["source"]["event_serial_no"] = token
            denied(event, "CDC_VALUE_INVALID_VERSION")

    def test_duplicate_json_and_malformed_nested_metadata_fail_closed(self):
        message = record(envelope())
        message["value"] = message["value"].replace(
            '"count": 2147483647', '"count": 1, "count": 2147483647', 1
        )
        with self.assertRaisesRegex(
            ValueError, "^CDC_VALUE_DUPLICATE_JSON_KEY$"
        ):
            decode(TABLE, message)
        event = envelope()
        event["transaction"] = {"id": {"private_fixture": 1}}
        with self.assertRaisesRegex(
            ValueError, "^CDC_VALUE_TRANSPORT_METADATA$"
        ):
            decode(TABLE, record(event))

    def test_topic_alias_and_kafka_position_bounds(self):
        message = record(envelope())
        message["topic"] = "fixture-stream.fixture_schema.items"
        self.assertEqual(decode(TABLE, message)[0], "u")
        message["partition"] = False
        with self.assertRaisesRegex(
            ValueError, "^CDC_VALUE_INVALID_POSITION$"
        ):
            decode(TABLE, message)
        message["topic"] = "fixture-stream.other_db.fixture_schema.items"
        with self.assertRaisesRegex(ValueError, "^CDC_VALUE_TOPIC_MISMATCH$"):
            decode(TABLE, message)
