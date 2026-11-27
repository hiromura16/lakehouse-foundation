"""合成5表の実ローカル保存、障害、再送を検証する。"""

import importlib.util
import sqlite3
import unittest
from contextlib import closing

import fixture
import storage

RUNTIME_READY = all(
    importlib.util.find_spec(name) is not None
    for name in ("pyarrow", "pyiceberg", "sqlalchemy")
)


@unittest.skipUnless(RUNTIME_READY, "requires pinned local Arrow/SqlCatalog")
class StorageTests(unittest.TestCase):
    """既存パイプラインやネットワークを使わず、試験倉庫だけを使う。"""

    def test_exact_five_tables_and_typed_rows_in_real_iceberg(self):
        """112初期、1024変更、256 nullを個別に照合する。"""
        records = list(fixture.deliveries())
        with storage.Harness() as harness:
            for start in range(0, len(records), storage.MAX_BATCH):
                window = slice(start, start + storage.MAX_BATCH)
                receipt = harness.write(records[window])
                self.assertLessEqual(receipt["accepted"], storage.MAX_BATCH)
            self.assertEqual(harness.experiment.quarantine_count(), 0)
            harness.reopen()
            self.assertEqual(
                harness.verify_final(records),
                {
                    "tables": 5,
                    "snapshot": 112,
                    "insert": 256,
                    "update": 512,
                    "delete": 256,
                    "tombstone": 256,
                    "raw_rows": 1392,
                    "current_rows": 112,
                    "silver_rows": 112,
                },
            )

    def test_real_silver_fault_preserves_all_durable_positions_until_retry(
        self,
    ):
        """実catalogの一領域だけを失敗させ、部分commitと再送を確認。"""
        records = list(fixture.deliveries())[:10]
        first, second = records[:5], records[5:]
        with storage.Harness() as harness:
            self.assertEqual(harness.write(first)["accepted"], 5)
            baseline = {spec.topic: 0 for spec in fixture.TABLES}
            self.assertEqual(harness.offsets(), baseline)
            path = harness.root / "catalog.db"
            with closing(sqlite3.connect(path)) as controller:
                controller.execute("""
                    CREATE TRIGGER fail_fixture_silver
                    BEFORE UPDATE OF metadata_location ON iceberg_tables
                    WHEN OLD.catalog_name = 'contract-scale-proof'
                      AND OLD.table_namespace = 'fixture_scale'
                      AND OLD.table_name = 'silver_fixture_echo'
                    BEGIN
                      SELECT RAISE(ABORT, 'FIXTURE_SCALE_SILVER_REJECT');
                    END
                """)
                try:
                    with self.assertRaisesRegex(
                        ValueError, "DERIVED_STORAGE_FAILED"
                    ):
                        harness.write(second)
                finally:
                    controller.execute("DROP TRIGGER fail_fixture_silver")
            self.assertTrue(harness.observed_fault)
            self.assertEqual(harness.offsets(), baseline)
            for spec in fixture.TABLES:
                self.assertEqual(len(harness.read(spec.name, "raw")), 2)
            self.assertEqual(len(harness.read("fixture_echo", "current")), 2)
            self.assertEqual(len(harness.read("fixture_echo", "silver")), 1)
            self.assertEqual(harness.experiment.quarantine_count(), 1)
            harness.reopen()
            self.assertEqual(harness.offsets(), baseline)
            self.assertEqual(harness.write(second)["accepted"], 5)
            self.assertEqual(
                harness.offsets(), {spec.topic: 1 for spec in fixture.TABLES}
            )
            for spec in fixture.TABLES:
                self.assertEqual(len(harness.read(spec.name, "raw")), 2)
                self.assertEqual(len(harness.read(spec.name, "current")), 2)
                self.assertEqual(len(harness.read(spec.name, "silver")), 2)

    def test_independent_wire_check_rejects_writer_mapping_change(self):
        """書込み変換が列を間違えても、自分自身の期待値で合格しない。"""
        event = next(iter(fixture.deliveries()))
        good = storage.raw_row(event)
        self.assertTrue(storage.same_wire([good], [event]))
        for changes in (
            {"topic": "unrelated.synthetic"},
            {"offset": 1},
            {"partition": 1},
            {"key_json": event["value"]},
            {"value_json": None},
        ):
            self.assertFalse(
                storage.same_wire([dict(good, **changes)], [event])
            )

    def test_unexpected_position_and_oversize_stop_before_raw_append(self):
        """余分な配送座標のまま原本へ流入させない。"""
        sample = next(iter(fixture.deliveries()))
        with storage.Harness() as harness:
            with self.assertRaisesRegex(ValueError, "SCALE_BATCH_LIMIT"):
                harness.write([sample] * (storage.MAX_BATCH + 1))
            with self.assertRaisesRegex(ValueError, "SCALE_TOPIC_POSITION"):
                harness.write([dict(sample, offset=4)])
            self.assertEqual(harness.offsets(), {})
            for spec in fixture.TABLES:
                self.assertEqual(harness.read(spec.name, "raw"), [])


if __name__ == "__main__":
    unittest.main()
