"""閉鎖した合成保存物だけでConsumer/Iceberg証跡アダプターを試験。"""

import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from test_cdc_contract_observation import GENERATION, NOW
from test_cdc_contract_registration import FIXTURE, observed

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
    adapter = importlib.import_module("cdc_local_fixture_evidence")
    collector = importlib.import_module("cdc_collect_fixture")

RUNTIME_READY = all(
    importlib.util.find_spec(name) is not None
    for name in ("pyiceberg", "pyarrow", "sqlalchemy")
)


def seal(scope, contract, registration):
    """fixture作成者として封印する。アダプターはsealを変更しない。"""
    connector = adapter.compile_spec(contract)["connectors"][0]
    route = connector["routes"][0]
    view = adapter.assessment(contract, registration, FIXTURE)
    hashes = {
        path.relative_to(scope).as_posix(): sha256(
            path.read_bytes()
        ).hexdigest()
        for path in scope.rglob("*")
        if path.is_file() and path.name != adapter.SEAL
    }
    record = {
        "version": 1,
        "environment": "synthetic",
        "fixture_id": FIXTURE,
        "generation": GENERATION,
        "route_sha256": adapter.digest(route),
        "topic_sha256": adapter.digest(route["topics"]["canonical"]),
        "group_sha256": adapter.digest([FIXTURE, connector["topic.prefix"]]),
        "registration_sha256": view["observation_sha256"],
        "desired_contract_sha256": view["desired_contract_sha256"],
        "observed_at": NOW - 1,
        "files": hashes,
    }
    payload = json.dumps(record).encode()
    path = scope / adapter.SEAL
    path.write_bytes(payload)
    path.chmod(0o600)
    return sha256(payload).hexdigest()


class GuardTests(unittest.TestCase):
    """PyIcebergがない source CI でも適用される基本検査。"""

    def test_external_authority_traversal_and_non_file_never_resolve(self):
        """ローカル封印倉庫外のURIは開かずに拒否する。"""
        root = Path("/fixture/warehouse")
        for bad in (
            "s3://fixture.invalid/file.parquet",
            "https://fixture.invalid/file.parquet",
            "file://fixture.invalid/fixture/warehouse/file.parquet",
            "file:///fixture/warehouse/%2e%2e/file.parquet",
            "file:///elsewhere/file.parquet",
            "file:///fixture/warehouse/file.parquet?query",
        ):
            with self.assertRaisesRegex(ValueError, "UNSAFE_ICEBERG_LOCATION"):
                adapter.contains_path(root, bad)
        self.assertEqual(
            adapter.contains_path(root, (root / "data").as_uri()),
            root / "data",
        )

    def test_file_and_parent_links_are_not_followed(self):
        """封印手順がどの段でもlinkを使わない。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            other = path / "other"
            other.mkdir(mode=0o700)
            content = other / "synthetic"
            content.write_bytes(b"dummy")
            content.chmod(0o600)
            (path / "link").symlink_to(other, target_is_directory=True)
            (path / "leaf").symlink_to(content)
            for relative in ("link/synthetic", "leaf"):
                with self.assertRaises(OSError):
                    adapter.read_owned(path, relative)

    def test_checkpoint_requires_exact_acceptance_and_one_named_partition(
        self,
    ):
        """完了位置と受理行を同じsealed SQLiteから確認する。"""
        with closing(sqlite3.connect(":memory:")) as writer:
            with writer:
                writer.execute(
                    "CREATE TABLE contract_experiment(identity TEXT)"
                )
                writer.execute(
                    "CREATE TABLE checkpoints(topic TEXT,part INT,off INT)"
                )
                writer.execute(
                    "CREATE TABLE accepted(seq INT,topic TEXT,"
                    "part INT,off INT)"
                )
                writer.execute(
                    "INSERT INTO contract_experiment VALUES ('digest')"
                )
                writer.execute(
                    "INSERT INTO checkpoints VALUES ('synthetic',0,0)"
                )
                writer.execute(
                    "INSERT INTO accepted VALUES (1,'synthetic',0,0)"
                )
            blob = writer.serialize()
            self.assertEqual(
                adapter.local_checkpoint(blob, "digest", "synthetic"),
                [{"partition": 0, "local_next": 1}],
            )
            with self.assertRaisesRegex(
                ValueError, "STATE_CHECKPOINT_MISMATCH"
            ):
                adapter.local_checkpoint(blob, "digest", "wrong")
            with writer:
                writer.execute("DELETE FROM accepted")
            with self.assertRaisesRegex(
                ValueError, "STATE_CHECKPOINT_MISMATCH"
            ):
                adapter.local_checkpoint(
                    writer.serialize(), "digest", "synthetic"
                )

    def test_enumeration_refuses_swapped_subdirectory(self):
        """実entryの検査後でも、開く直前のlink差替えを辿らない。"""
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary)
            root = top / FIXTURE
            warehouse = root / "warehouse"
            target = warehouse / "data"
            for directory in (root, warehouse, target, top / "outside"):
                directory.mkdir(mode=0o700)
            for name in (adapter.SEAL, "state.db", "catalog.db"):
                file = root / name
                file.touch(mode=0o600)
            original_open = os.open
            swaps = []

            def swap_before_open(name, mode, *args, **kwargs):
                if name == "data" and not swaps:
                    self.assertIsNotNone(kwargs.get("dir_fd"))
                    self.assertTrue(mode & os.O_NOFOLLOW)
                    swaps.append(True)
                    target.rename(warehouse / "old")
                    target.symlink_to(
                        top / "outside", target_is_directory=True
                    )
                return original_open(name, mode, *args, **kwargs)

            with patch.object(
                adapter.os, "open", side_effect=swap_before_open
            ):
                with self.assertRaises(OSError):
                    adapter.enumerate_owned(root)
            self.assertEqual(swaps, [True])

    def test_real_sql_scalars_are_not_considered_integer_offsets(self):
        """SQLite REAL 0.0を受理順やpartitionに流用しない。"""
        for category in ("checkpoint", "accepted"):
            with closing(sqlite3.connect(":memory:")) as writer:
                with writer:
                    writer.execute(
                        "CREATE TABLE contract_experiment(identity TEXT)"
                    )
                    # no affinity: preserve the SQLite REAL type itself
                    writer.execute("CREATE TABLE checkpoints(topic,part,off)")
                    writer.execute("CREATE TABLE accepted(seq,topic,part,off)")
                    writer.execute(
                        "INSERT INTO contract_experiment VALUES ('digest')"
                    )
                    writer.execute(
                        "INSERT INTO checkpoints VALUES (?,?,?)",
                        ("topic", 0.0 if category == "checkpoint" else 0, 0),
                    )
                    writer.execute(
                        "INSERT INTO accepted VALUES (?,?,?,?)",
                        (1, "topic", 0, 0.0 if category == "accepted" else 0),
                    )
                with self.assertRaisesRegex(
                    ValueError, "STATE_CHECKPOINT_MISMATCH"
                ):
                    adapter.local_checkpoint(
                        writer.serialize(), "digest", "topic"
                    )

    def test_sqlite_views_not_recognized_as_checkpoint_tables(self):
        """同名viewや副作用を持つスキーマは既存証跡として取り込まない。"""
        with closing(sqlite3.connect(":memory:")) as writer:
            with writer:
                writer.execute("CREATE VIEW checkpoints AS SELECT 1 AS topic")
            with self.assertRaisesRegex(
                ValueError, "UNSAFE_FIXTURE_DATABASE_SCHEMA"
            ):
                adapter.local_checkpoint(writer.serialize(), "digest", "topic")


@unittest.skipUnless(
    RUNTIME_READY, "requires pinned PyIceberg/Arrow/SqlCatalog"
)
class RealFixtureTests(unittest.TestCase):
    """実Phase 2Bのパイプラインが書いたSQLiteとParquetを閉じて読む。"""

    def setUp(self):
        """このテストが所有する一時領域に合成4イベントだけを書き込む。"""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.scope = root / FIXTURE
        self.scope.mkdir(mode=0o700)
        proof_path = ROOT / "tests/integration/cdc_contract_iceberg/proof.py"
        spec = importlib.util.spec_from_file_location(
            "owned_fixture_proof", proof_path
        )
        proof = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(ROOT / "infra/cdc"), *sys.path]):
            spec.loader.exec_module(proof)
            previous = os.umask(0o077)
            try:
                with proof.reopened(self.scope, existing=False) as (exp, ice):
                    for offset in range(4):
                        proof.bridge(exp, ice, proof.fixture_record(offset))
                self.contract = proof.DOC
                self.registration = observed(self.contract)
                self.fingerprint = seal(
                    self.scope, self.contract, self.registration
                )
            finally:
                os.umask(previous)

    def read(self, fingerprint=None):
        """署名ではない別経路のexact sealを、その試行の読取りに渡す。"""
        return adapter.collect_local(
            self.scope,
            fingerprint or self.fingerprint,
            self.contract,
            self.registration,
            FIXTURE,
            GENERATION,
            NOW,
        )

    def test_real_receipts_are_finite_and_keep_original_tree_unchanged(self):
        """実SQLite next=4とIcebergのsnapshot/Parquet countを再読する。"""

        def all_bytes():
            return {
                p: p.read_bytes() for p in self.scope.rglob("*") if p.is_file()
            }

        before = all_bytes()
        receipts = self.read()
        view = adapter.assessment(self.contract, self.registration, FIXTURE)
        for domain, receipt in receipts.items():
            at, rows = collector.envelope(
                receipt, domain, FIXTURE, GENERATION, NOW, view
            )
            self.assertEqual(at, NOW - 1)
            self.assertEqual(len(rows), 1)
        row = receipts["consumer"]["rows"][0]
        self.assertEqual(
            row["checkpoints"], [{"partition": 0, "local_next": 4}]
        )
        self.assertIsNone(row["last_error"])
        self.assertIsNone(row["last_event_age_seconds"])
        targets = receipts["iceberg"]["rows"][0]["targets"]
        self.assertEqual(set(targets), set(adapter.KINDS))
        for target in targets.values():
            self.assertGreater(target["snapshot_id"], 0)
            self.assertEqual(target["row_count"], 4)
        self.assertEqual(all_bytes(), before)

    def test_mismatched_seal_sidecars_and_changed_bytes_are_rejected(self):
        """sealの違い・WAL・書き換えたファイルを読取り前に拒否する。"""
        with self.assertRaisesRegex(ValueError, "FIXTURE_SEAL_MISMATCH"):
            self.read("0" * 64)
        unknown = self.scope / "state.db-wal"
        unknown.write_bytes(b"untrusted synthetic sidecar")
        with self.assertRaisesRegex(
            ValueError, "FIXTURE_ARTIFACT_SET_MISMATCH"
        ):
            self.read()
        unknown.unlink()
        state = self.scope / "state.db"
        state.write_bytes(state.read_bytes() + b"extra")
        with self.assertRaisesRegex(
            ValueError, "FIXTURE_ARTIFACT_HASH_MISMATCH"
        ):
            self.read()

    def test_sealed_foreign_pointer_fails_without_opening_resource(self):
        """試験内で再封印しても、外のURIをカタログが選べない。"""
        with closing(sqlite3.connect(self.scope / "catalog.db")) as writer:
            with writer:
                other_lane = writer.execute(
                    "SELECT metadata_location FROM iceberg_tables "
                    "WHERE table_name='silver'"
                ).fetchone()[0]
            for unowned in (
                "https://fixture.invalid/foreign.metadata.json",
                other_lane,
            ):
                with writer:
                    writer.execute(
                        "UPDATE iceberg_tables SET metadata_location=? "
                        "WHERE table_name='raw'",
                        (unowned,),
                    )
                updated = seal(self.scope, self.contract, self.registration)
                with self.assertRaisesRegex(
                    ValueError, "UNSAFE_ICEBERG_LOCATION"
                ):
                    self.read(updated)


if __name__ == "__main__":
    unittest.main()
