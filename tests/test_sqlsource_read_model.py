"""SQL 観測を、UI と HTTP を import せずに固定する。"""

import sys
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra/demo"))
import sqlsource_read_model as model  # noqa: E402


class ReadModelTests(unittest.TestCase):
    """層と完了・進捗を独立して観測する契約を確認する。"""

    def test_exact_fixed_count_sql(self):
        """抽出前と同じ固定 SQL を既知の二経路にだけ使う。"""
        for origin, digest in (
            (
                "batch",
                "b6ba103333ade5194ea170f284a6c676"
                "d9ca4cd6e3969bb5e2347c4f05923010",
            ),
            (
                "cdc",
                "11c42e431fb0b996b872259fe5ce7241"
                "bd61a4d83bd37e0b9c1d1811b4261182",
            ),
        ):
            self.assertEqual(
                sha256(model.count_statement(origin).encode()).hexdigest(),
                digest,
            )
        query = Mock()
        with self.assertRaises(model.ObservationUnavailable):
            model.read_layers("unknown", query)
        query.assert_not_called()

    def test_zero_counts_and_unknown_progress_stay_distinct(self):
        """失敗した進捗が実測ゼロや完了履歴を上書きしない。"""
        rows = [[name, 0, 0, 0] for name in reversed(model.IDS)]
        query = Mock(side_effect=[rows, [], RuntimeError("fixture-private")])
        result = model.pipeline_status(query)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(
            result["layers"],
            [
                {
                    "table": name,
                    "bronze_rows": 0,
                    "silver_rows": 0,
                    "quarantine_rows": 0,
                }
                for name in model.IDS
            ],
        )
        self.assertTrue(result["history_observed"])
        self.assertIsNone(result["last_batch"])
        self.assertFalse(result["progress_observed"])
        self.assertIsNone(result["pending"])
        self.assertFalse(result["consistent_snapshot"])
        self.assertNotIn("fixture-private", repr(result))
