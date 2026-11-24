"""Raw 読み取り境界を、SQLite・派生処理・ネットワークなしで試験する。"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra/cdc"))
import raw_history  # noqa: E402


class RawHistoryTests(unittest.TestCase):
    """リプレイ前の読み取り契約を、呼出し順・版・診断で固定する。"""

    def test_pinned_snapshot_survives_current_change(self):
        """選択後に current が動いても、header と本文は同じ版を読む。"""
        header = {
            "topic": "synthetic",
            "partition": "0",
            "offset": "0",
            "event_id": '["synthetic",0,0]',
        }
        body = {
            **header,
            "key_json": "",
            "envelope_json": None,
            "ingested_at": "2026-01-01T00:00:00Z",
            "tombstone": "true",
        }
        table = Mock(properties={"lakehouse.fixed-cdc.identity": "fixture"})
        table.current_snapshot.return_value.snapshot_id = 42
        catalog = Mock()
        catalog.load_table.return_value = table
        selected, pinned = raw_history.open_raw_snapshot(
            catalog, lambda: "fixture"
        )
        self.assertIs(selected, table)
        table.current_snapshot.return_value.snapshot_id = 43
        table.scan.return_value.to_arrow.return_value.to_pylist.side_effect = [
            [header],
            [body],
        ]
        headers = raw_history.raw_headers(
            table, pinned, {"synthetic": "items"}, {}, maximum=1
        )
        expressions = SimpleNamespace(EqualTo=lambda key, value: (key, value))
        with patch.dict(sys.modules, {"pyiceberg.expressions": expressions}):
            record, size = raw_history.read_record(table, pinned, headers[0])
        self.assertEqual(record["offset"], 0)
        self.assertIsNone(record["value"])
        self.assertEqual(record["key"], "")
        self.assertEqual(size, 0)
        scans = [call.kwargs for call in table.scan.call_args_list]
        self.assertEqual([scan["snapshot_id"] for scan in scans], [42, 42])
        self.assertEqual([scan["limit"] for scan in scans], [2, 2])
        self.assertNotIn("envelope_json", scans[0]["selected_fields"])
        self.assertEqual(
            scans[1]["row_filter"], ("event_id", header["event_id"])
        )
        table.current_snapshot.assert_called_once()

    def test_missing_table_does_not_read_state_or_open_snapshot(self):
        """Raw 表がない場合、従来どおり識別値を取得する前に停止する。"""
        catalog = Mock()
        catalog.table_exists.return_value = False
        read_identity = Mock(
            side_effect=AssertionError("unexpected state read")
        )
        with self.assertRaisesRegex(
            raw_history.RebuildError, "^CDC_RAW_HISTORY_MISSING$"
        ):
            raw_history.open_raw_snapshot(catalog, read_identity)
        read_identity.assert_not_called()
        catalog.load_table.assert_not_called()

    def test_identity_mismatch_blocks_snapshot_read(self):
        """ソースが違う場合は固定理由だけ返し、履歴を読まない。"""
        catalog = Mock()
        table = Mock(properties={"lakehouse.fixed-cdc.identity": "other"})
        catalog.load_table.return_value = table
        with self.assertRaisesRegex(
            raw_history.RebuildError, "^CDC_RAW_SOURCE_MISMATCH$"
        ):
            raw_history.open_raw_snapshot(catalog, lambda: "fixture")
        table.current_snapshot.assert_not_called()
        table.scan.assert_not_called()
