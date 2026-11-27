"""5表の実source driverのdispatchと拒否境界をnetworkなしで確認。"""

import os
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import fixture
import source


class SourceScaleTest(unittest.TestCase):
    """native scopeは将来のCIでだけ、ここでは固定contractを確認。"""

    def test_fixed_sql_plan_has_exact_workload_and_no_tombstone_dml(self):
        """初期112行とI/U/D1024件を、将来の実execute用に再現する。"""
        seeds = list(source.seeds())
        changes = list(source.mutations())
        self.assertEqual(len(seeds), 112)
        self.assertEqual(len(changes), 1024)
        self.assertEqual(set(item.op for _, item in seeds), {"r"})
        self.assertEqual(
            Counter(item.op for _, item in changes),
            {"c": 256, "u": 512, "d": 256},
        )
        self.assertTrue(
            all(fixture.sql_step(spec, step) for spec, step in changes)
        )

    def test_config_five_snapshot_overrides_tls_and_immutable_endpoints(self):
        """本番defaultに触れず固定fixtureだけを照合する。"""
        config = source.configuration()
        prefix = "snapshot.select.statement.overrides"
        names = [f"{fixture.DATABASE}.dbo.{t.name}" for t in fixture.TABLES]
        self.assertEqual(config[prefix].split(","), names)
        for qualified, spec in zip(names, fixture.TABLES, strict=True):
            query = config[prefix + "." + qualified]
            self.assertIn(f"[{fixture.DATABASE}].[dbo].[{spec.name}]", query)
            self.assertTrue(query.endswith("ORDER BY site_id, record_id"))
        self.assertEqual(config["driver.trustServerCertificate"], "false")
        self.assertEqual(config["driver.encrypt"], "true")
        self.assertEqual(config["decimal.handling.mode"], "string")
        self.assertEqual(config["tombstones.on.delete"], "true")
        topics = source.create_topic_plan()
        self.assertEqual(len(topics), 9)
        self.assertEqual(topics[source.HISTORY]["retention.ms"], "-1")
        self.assertTrue(
            all(
                topics[t.topic]["cleanup.policy"] == "delete"
                for t in fixture.TABLES
            )
        )

    def test_foreign_or_reused_broker_never_creates_topics(self):
        """実brokerの所有識別かfresh条件が異なる場合、変更前に止まる。"""
        admin = Mock()
        owner = "A" * 22
        worker = source.Scale(
            None, None, Mock(return_value=admin), Mock(), owner, "", ""
        )
        for ident, topics, err in (
            ("B" * 22, {}, "SCALE_BROKER_IDENTITY"),
            (owner, {"old": 1}, "SCALE_BROKER_NOT_EMPTY"),
        ):
            admin.list_topics.return_value = SimpleNamespace(
                cluster_id=ident, topics=topics
            )
            with self.assertRaisesRegex(source.base.Failure, err):
                worker.prepare_topics()
            admin.create_topics.assert_not_called()

    def test_receive_native_handling_never_invents_source_lsn(self):
        """型上妥当なデータでも、現行connector/LSNが欠けていれば拒否。"""
        spec = fixture.TABLES[0]
        plan = source.plans()[spec.topic]
        native = fixture.modeled(spec, 0, plan[0])
        with self.assertRaisesRegex(
            source.base.Failure, "SCALE_SOURCE_PLUGIN_IDENTITY"
        ):
            source.verify_origin(native, "r")
        doc = {
            "source": {
                "connector": "sqlserver",
                "version": "3.7.1.Final",
                "commit_lsn": "00000000:00000000:0000",
            }
        }
        with self.assertRaisesRegex(
            source.base.Failure, "SCALE_STREAMING_LOG_POSITION"
        ):
            source.verify_origin({"value": fixture.encode(doc)}, "u")
        source.verify_origin({"value": None}, "tombstone")

    def test_sql_dml_counts_server_result_and_does_not_continue_on_zero(self):
        """実セッションで各DMLのrowcountを発行後に確認する。"""
        admin = Mock()
        worker = source.Scale(
            None, None, Mock(return_value=admin), Mock(), "A" * 22, "", ""
        )
        ctx = Mock()
        connection = Mock()
        cursor = connection.cursor.return_value
        ctx.__enter__ = Mock(return_value=connection)
        ctx.__exit__ = Mock(return_value=False)
        changes = list(source.mutations())[:2]
        with patch.object(worker, "connect", return_value=ctx):
            cursor.fetchall.side_effect = [[(1,)], [(1,)]]
            worker.write_sql(changes, 2)
            self.assertEqual(
                cursor.execute.call_args_list[0], call("SET NOCOUNT ON")
            )
            self.assertTrue(
                all(
                    "SELECT CONVERT(int,@@ROWCOUNT)" in q.args[0]
                    for q in cursor.execute.call_args_list[1:]
                )
            )
            cursor.execute.reset_mock()
            cursor.fetchall.side_effect = [[(0,)]]
            with self.assertRaisesRegex(
                source.base.Failure, "SCALE_SQL_AFFECTED_ROWS"
            ):
                worker.write_sql(changes, 2)
            self.assertEqual(cursor.execute.call_count, 2)

    def test_without_hosted_lifecycle_and_explicit_owned_cluster_is_refused(
        self,
    ):
        """通常Bakuから入っても、DB・Kafka・Dockerを呼ばない。"""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                source.base.Failure, "DISPOSABLE_HOSTED_REQUIRED"
            ):
                source.require_hosted()
        with patch.object(source.base, "validate_hosted"):
            with patch.dict(
                os.environ,
                {
                    "CDC_SQL_SCALE_HOSTED": "true",
                    "CDC_SQL_SCALE_KAFKA_ID": "bad",
                },
                clear=True,
            ):
                with self.assertRaisesRegex(
                    source.base.Failure, "SCALE_OWNED_RUNNER_REQUIRED"
                ):
                    source.require_hosted()
