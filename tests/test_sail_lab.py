"""Synthetic contracts and fakes, never live Spark/Sail/Iceberg acceptance."""

import copy
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager, redirect_stdout
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from infra.sail import catalog, contract, evidence, runner, server


def metadata(table, snapshot=1):
    """Build a synthetic Iceberg REST response with a fixed owner marker."""
    return {
        "metadata": {
            "format-version": 2,
            "properties": {"lab.owner": contract.OWNER},
            "current-schema-id": 1,
            "schemas": [
                {
                    "schema-id": 1,
                    "fields": [
                        {
                            "name": name,
                            "type": kind.replace("bigint", "long"),
                            "required": False,
                        }
                        for name, kind in contract.schema_for(table)
                    ],
                }
            ],
            "current-snapshot-id": snapshot,
            "snapshots": [{"snapshot-id": snapshot}],
        }
    }


class Response:
    """One bounded in-memory HTTP response, with no actual network access."""

    def __init__(self, body, status=200):
        self.body = io.BytesIO(json.dumps(body).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read1(self, maximum):
        return self.body.read(maximum)


class Opener:
    """Return predetermined pages and retain only requests for assertions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response(value)


class FixtureTests(unittest.TestCase):
    """Check lossless edge cases and true duplicate-multiset comparisons."""

    def setUp(self):
        self.fixture, self.checksum = contract.fixture_bytes()
        self.expected = contract.reference(self.fixture)

    def test_immutable_six_source_fixture(self):
        self.assertEqual(
            self.checksum,
            "34507de1fc2622c82a31591602c01c728f47eb3704252fd99f3d09846166f824",
        )
        self.assertEqual(set(self.fixture["sources"]), set(contract.SOURCES))
        self.assertEqual(sum(map(len, self.fixture["sources"].values())), 145)
        self.assertEqual(len(self.expected["bronze_mes_events"]), 41)
        self.assertEqual(len(self.expected["silver_mes_events"]), 40)
        self.assertEqual(len(self.expected["factory_daily_production"]), 14)

    def test_null_empty_japanese_decimal_are_distinct(self):
        rows = self.expected["silver_erp_products"]
        self.assertEqual(rows[0]["product_name"], "合成部品・耐熱Ω")
        self.assertEqual(rows[1]["product_name"], "")
        self.assertIsNone(rows[2]["product_name"])
        self.assertEqual(
            rows[0]["standard_cycle_seconds"], Decimal("99999999.125")
        )
        order = self.expected["silver_erp_sales_orders"][0]
        self.assertEqual(order["unit_price"], Decimal("99999999999999.99"))

    def test_utc_offset_and_seventh_digit_preserved(self):
        text, ticks = contract.utc7("2026-01-04T09:00:00.1234567+09:00")
        self.assertEqual(text, "2026-01-04T00:00:00.1234567Z")
        self.assertEqual(ticks % 10000000, 1234567)
        self.assertEqual(
            contract.utc7("2026-01-04T00:00:00.1234567Z")[1], ticks
        )
        self.assertEqual(contract.utc7("1969-12-31T23:59:59.9999999Z")[1], -1)

    def test_native_timestamp7_is_documented_negative_not_live_success(self):
        with self.assertRaisesRegex(contract.LabFailure, "NATIVE_TIMESTAMP7"):
            contract.native_timestamp7_probe()
        result = contract.native_timestamp7_probe(
            "2026-01-05T00:00:00.1234560Z"
        )
        self.assertEqual(result["status"], "not_run")

    def test_invalid_timestamps_never_truncated(self):
        for value in (
            "2026-01-05T00:00:00.12345678Z",
            "2026-01-05T00:00:00",
            "2026-01-05T00:00:60Z",
            "2026-01-05T00:00:00+99:00",
        ):
            with (
                self.subTest(value=value),
                self.assertRaises(contract.LabFailure),
            ):
                contract.utc7(value)

    def test_decimal_and_integer_narrowing_rejected(self):
        for value, kind in (
            (1.25, "decimal(18,2)"),
            ("1.234", "decimal(18,2)"),
            ("NaN", "decimal(18,2)"),
            ("10000000000000000", "decimal(18,2)"),
            (True, "bigint"),
            (2**63, "bigint"),
            ("1.0", "bigint"),
        ):
            with (
                self.subTest(value=value),
                self.assertRaises(contract.LabFailure),
            ):
                contract.typed(value, kind)

    def test_multiplicity_not_set_equality(self):
        rows = self.expected["bronze_mes_events"]
        report = contract.compare("bronze_mes_events", rows, rows[:-1])
        self.assertFalse(report["equal"])
        self.assertEqual(report["missing_count"], 1)
        self.assertNotEqual(
            report["expected_sha256"], report["observed_sha256"]
        )
        self.assertLessEqual(len(report["synthetic_diffs"]["missing"]), 5)

    def test_full_multiset_is_order_independent_without_losing_duplicates(
        self,
    ):
        rows = self.expected["bronze_mes_events"]
        self.assertTrue(
            contract.compare("bronze_mes_events", rows, rows[::-1])["equal"]
        )

    def test_utc_limits_are_padded_or_rejected_without_overflow(self):
        stamp, _ = contract.utc7("0001-01-01T00:00:00.0000001Z")
        self.assertEqual(stamp, "0001-01-01T00:00:00.0000001Z")
        for value in (
            "0001-01-01T00:00:00+01:00",
            "9999-12-31T23:59:59-01:00",
        ):
            with self.subTest(value=value):
                with self.assertRaises(contract.LabFailure):
                    contract.utc7(value)

    def test_full_business_column_mismatch_explained(self):
        expected = self.expected["silver_erp_products"]
        observed = copy.deepcopy(expected)
        observed[2]["product_name"] = ""
        report = contract.compare("silver_erp_products", expected, observed)
        self.assertFalse(report["equal"])
        self.assertEqual(report["missing_count"], 1)
        self.assertEqual(report["extra_count"], 1)
        self.assertIn("null", report["synthetic_diffs"]["missing"][0])

    def test_column_shape_change_rejected(self):
        row = dict(self.expected["silver_erp_products"][0], unexpected="value")
        with self.assertRaisesRegex(contract.LabFailure, "SCHEMA_MISMATCH"):
            contract.canonical_row("silver_erp_products", row)

    def test_fixed_destination_allowlist(self):
        self.assertEqual(
            contract.target("spark", "factory_daily_production"),
            "lakehouse.spark_lab.factory_daily_production",
        )
        self.assertEqual(
            contract.target("sail", "silver_mes_events", "iceberg"),
            "iceberg.sail_lab.silver_mes_events",
        )
        for args in (
            ("gold", "factory_daily_production"),
            ("sail", "compatibility.spark_roundtrip"),
            ("sail", "silver_mes_events", "external"),
            ("sail", "silver_mes_events; DROP TABLE x"),
        ):
            with self.assertRaises(contract.LabFailure):
                contract.target(*args)

    def test_changed_fixture_refused_before_any_session(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.json"
            path.write_text("{}")
            with self.assertRaisesRegex(contract.LabFailure, "INPUT_CHANGED"):
                contract.fixture_bytes(path)

    def test_conflicting_same_revision_refused(self):
        fixture = copy.deepcopy(self.fixture)
        conflict = dict(fixture["sources"]["mes_events"][0], good_qty="999")
        fixture["sources"]["mes_events"].append(conflict)
        with self.assertRaisesRegex(contract.LabFailure, "FIXTURE_INVALID"):
            contract.reference(fixture)

    def test_conflicting_older_revision_refused_after_newer_winner(self):
        fixture = copy.deepcopy(self.fixture)
        original = fixture["sources"]["mes_events"][0]
        newer = dict(original, updated_at="2026-12-01T00:00:00.0000000Z")
        conflict = dict(original, good_qty="999")
        fixture["sources"]["mes_events"] += [newer, conflict]
        with self.assertRaisesRegex(contract.LabFailure, "FIXTURE_INVALID"):
            contract.reference(fixture)

    def test_gold_aggregates_do_not_multiply_event_and_downtime_joins(self):
        first = self.expected["factory_daily_production"][0]
        self.assertEqual(first["factory_id"], "F01")
        self.assertEqual(first["production_date"], date(2026, 1, 5))
        events = [
            row
            for row in self.expected["silver_mes_events"]
            if row["work_order_id"] in {"WO00001", "WO00015"}
        ]
        self.assertEqual(
            first["good_qty"], sum(row["good_qty"] for row in events)
        )
        self.assertEqual(first["downtime_ticks_100ns"] % 10, 1)
        self.assertIsInstance(first["yield_pct"], Decimal)

    def test_shared_sql_has_explicit_grains_and_no_table_destination(self):
        sql = contract.gold_select()
        self.assertIn("WITH plans AS", sql)
        self.assertIn("production AS", sql)
        self.assertIn("downtime AS", sql)
        self.assertNotIn("CREATE", sql)
        self.assertNotIn("iceberg.gold", sql)
        self.assertIn("row_number()", contract.silver_select("mes_events"))


class CatalogTests(unittest.TestCase):
    """Fixed-origin HTTP, ownership and schema checks using fake pages."""

    def config(self, *pages):
        opener = Opener([{"defaults": {"prefix": "warehouse-id"}}, *pages])
        return catalog.Catalog("synthetic-warehouse", opener), opener

    def test_namespace_create_then_verify_is_owned(self):
        missing = urllib.error.HTTPError(
            "http://example.invalid", 404, "", {}, None
        )
        state, opener = self.config(
            missing, {}, {"properties": {"lab.owner": contract.OWNER}}
        )
        state.namespace("sail", approve=True)
        payload = json.loads(opener.requests[2].data)
        self.assertEqual(payload["namespace"], ["sail_lab"])
        self.assertEqual(payload["properties"], {"lab.owner": contract.OWNER})

    def test_namespace_existing_owner_required(self):
        state, _ = self.config({"properties": {"lab.owner": "foreign"}})
        with self.assertRaisesRegex(contract.LabFailure, "OWNERSHIP_MISMATCH"):
            state.namespace("spark", approve=True)

    def test_namespace_create_requires_explicit_consent(self):
        missing = urllib.error.HTTPError(
            "http://example.invalid", 404, "", {}, None
        )
        state, opener = self.config(missing)
        with self.assertRaisesRegex(contract.LabFailure, "WRITE_APPROVAL"):
            state.namespace("spark")
        self.assertEqual(len(opener.requests), 2)

    def test_catalog_server_prefix_cannot_change_origin_or_traverse(self):
        for prefix in (
            "https://example.invalid",
            "../outside",
            "a/../b",
            "a?x",
        ):
            with (
                self.subTest(prefix=prefix),
                self.assertRaises(contract.LabFailure),
            ):
                catalog.Catalog(
                    "synthetic", Opener([{"defaults": {"prefix": prefix}}])
                )

    def test_table_owner_format_schema_and_snapshot_verified(self):
        state, _ = self.config(metadata("silver_erp_products"))
        result = state.table("sail", "silver_erp_products")
        self.assertEqual(result["format_version"], 2)
        self.assertEqual(result["snapshot_id"], "1")
        self.assertEqual(
            result["schema"],
            [list(row) for row in contract.schema_for("silver_erp_products")],
        )

    def test_table_foreign_marker_or_native_timestamp_narrowing_refused(self):
        for mutation in ("owner", "type", "format", "snapshot"):
            response = metadata("silver_erp_products")
            values = response["metadata"]
            if mutation == "owner":
                values["properties"]["lab.owner"] = "foreign"
            elif mutation == "type":
                values["schemas"][0]["fields"][4]["type"] = "double"
            elif mutation == "format":
                values["format-version"] = 3
            else:
                values["current-snapshot-id"] = None
            state, _ = self.config(response)
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(contract.LabFailure),
            ):
                state.table("sail", "silver_erp_products")

    def test_no_redirect_handler_and_proxy_disabled(self):
        self.assertIsNone(
            catalog.NoRedirect().redirect_request(
                None, None, 302, "", {}, "http://example.invalid"
            )
        )
        opener = catalog.internal_opener()
        proxies = [
            handler
            for handler in opener.handlers
            if hasattr(handler, "proxies")
        ]
        self.assertTrue(all(handler.proxies == {} for handler in proxies))

    def test_response_body_and_deadline_bounded(self):
        with patch.object(catalog, "MAX_BODY", 8):
            with self.assertRaises(contract.LabFailure):
                catalog.json_request(
                    Opener([{"oversize": "x" * 100}]), "fixed", float("inf")
                )
        with patch.object(catalog.time, "monotonic", return_value=2):
            with self.assertRaises(contract.LabFailure):
                catalog.json_request(Opener([]), "fixed", 1)

    def test_trino_transport_preserves_decimal_null_empty_and_unicode(self):
        table = "silver_erp_products"
        rows = contract.reference(contract.fixture_bytes()[0])[table]
        schema = contract.schema_for(table)
        payload = {
            "columns": [{"name": name} for name, _ in schema],
            "data": [
                [
                    None if row[name] is None else str(row[name])
                    for name, _ in schema
                ]
                for row in rows
            ],
        }
        actual = catalog.trino_rows("sail", table, opener=Opener([payload]))
        self.assertTrue(contract.compare(table, rows, actual)["equal"])

    def test_trino_external_next_uri_rejected_no_credential_forwarding(self):
        opener = Opener(
            [{"nextUri": "https://example.invalid/v1/statement/x"}]
        )
        with self.assertRaisesRegex(contract.LabFailure, "TRINO_READ_FAILED"):
            catalog.trino_rows(
                "sail", "factory_daily_production", opener=opener
            )
        self.assertEqual(len(opener.requests), 1)

    def test_trino_error_message_never_leaks(self):
        opener = Opener(
            [{"error": {"message": "synthetic-private-credential"}}]
        )
        with self.assertRaises(contract.LabFailure) as caught:
            catalog.trino_rows(
                "sail", "factory_daily_production", opener=opener
            )
        self.assertEqual(str(caught.exception), "TRINO_READ_FAILED")

    def test_trino_first_page_schema_failure_cancels_approved_continuation(
        self,
    ):
        continuation = "http://trino:8080/v1/statement/synthetic/1"
        for invalid in (
            {"columns": [{"name": "wrong"}]},
            {"error": {"message": "synthetic failure"}},
        ):
            opener = Opener([{**invalid, "nextUri": continuation}, {}])
            with self.assertRaises(contract.LabFailure):
                catalog.trino_rows(
                    "sail", "factory_daily_production", opener=opener
                )
            self.assertEqual(len(opener.requests), 2)
            self.assertEqual(opener.requests[-1].full_url, continuation)
            self.assertEqual(opener.requests[-1].get_method(), "DELETE")

    def test_partial_http_failure_cancels_only_approved_trino_query(self):
        """Protocol errors must take the same bounded failure/cleanup path."""
        uri = "http://trino:8080/v1/statement/synthetic/1"
        for cancellation in ({}, http.client.BadStatusLine("synthetic")):
            opener = Opener(
                [
                    {"nextUri": uri},
                    http.client.IncompleteRead(b"synthetic-private-fragment"),
                    cancellation,
                ]
            )
            with self.assertRaises(contract.LabFailure) as caught:
                catalog.trino_rows(
                    "sail", "silver_erp_products", opener=opener
                )
            self.assertEqual(str(caught.exception), "TRINO_READ_FAILED")
            self.assertEqual(
                [request.get_method() for request in opener.requests],
                ["POST", "GET", "DELETE"],
            )
            self.assertEqual(opener.requests[-1].full_url, uri)


class FakeFrame:
    """Stand-in for a collected typed DataFrame; not an engine simulator."""

    def __init__(self, table, rows):
        self.rows = rows
        self.schema = [
            SimpleNamespace(
                name=name,
                dataType=SimpleNamespace(simpleString=lambda kind=kind: kind),
            )
            for name, kind in contract.schema_for(table)
        ]

    def take(self, maximum):
        return [
            SimpleNamespace(asDict=lambda recursive, row=row: row)
            for row in self.rows[:maximum]
        ]

    def createOrReplaceTempView(self, name):
        self.view_name = name


class FakeSession:
    """Only fulfill the runner's result-contract interactions."""

    version = "4.1.4"

    def __init__(self, expected):
        self.expected, self.statements = expected, []
        self.stop = Mock()

    def sql(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(take=lambda size: [[1]], collect=lambda: [])

    def table(self, destination):
        table = destination.rsplit(".", 1)[1]
        return FakeFrame(table, self.expected[table])


class RunnerTests(unittest.TestCase):
    """Mocked stage contracts, consent and cleanup, not live acceptance."""

    def setUp(self):
        self.expected = contract.reference(contract.fixture_bytes()[0])
        self.session = FakeSession(self.expected)
        self.catalog = Mock()
        self.locked, self.blocked = False, False

    @contextmanager
    def lock(self, engine):
        self.locked = True

        def retain():
            self.blocked = True

        try:
            yield retain
        finally:
            self.locked = False

    def workload(self, session, catalog, engine, fixture):
        return {
            table: {
                "format_version": 2,
                "snapshot_id": "1",
                "snapshot_count": 1,
                "schema": [list(row) for row in contract.schema_for(table)],
                "nullable": [True] * len(contract.schema_for(table)),
            }
            for table in contract.TABLES
        }

    def run_mocked(self, engine="sail", **overrides):
        options = dict(
            approve=True,
            session_factory=lambda engine: self.session,
            catalog_factory=lambda: self.catalog,
            lock_factory=self.lock,
            workload_function=self.workload,
            cross_read=lambda engine, table: self.expected[table],
        )
        options.update(overrides)
        return runner.run(engine, **options)

    def test_pure_plan_never_touches_network_or_starts_session(self):
        with (
            patch.object(runner, "create_session") as session,
            patch.object(catalog, "internal_opener") as opener,
        ):
            result = runner.plan()
        session.assert_not_called()
        opener.assert_not_called()
        self.assertEqual(result["status"], "plan")
        self.assertTrue(
            all(value == "not_run" for value in result["stages"].values())
        )
        self.assertIsNone(result["resource_metrics"]["engine_memory_average"])

    def test_missing_write_consent_fails_before_session_or_lock(self):
        session = Mock()
        result = runner.run("sail", session_factory=session)
        session.assert_not_called()
        self.assertEqual(result["code"], "WRITE_APPROVAL_REQUIRED")
        self.assertEqual(result["exit_code"], 1)

    def test_fake_contract_success_is_labeled_measurements_pending(self):
        result = self.run_mocked()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(set(result["tables"]), set(contract.TABLES))
        self.assertTrue(
            all(value == "passed" for value in result["stages"].values())
        )
        self.assertEqual(len(result["job_seconds"]), 2)
        self.assertTrue(
            all(row["replay_verified"] for row in result["tables"].values())
        )
        self.assertEqual(
            result["performance_verdict"], "pending_runtime_measurements"
        )
        self.assertEqual(result["image_identity"], "unknown")
        self.assertFalse(self.blocked)

    def test_partial_failure_stops_session_while_lock_held(self):
        self.session.stop.side_effect = lambda: self.assertTrue(self.locked)
        result = self.run_mocked(
            workload_function=Mock(
                side_effect=RuntimeError("synthetic-private-credential")
            )
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "ENGINE_QUERY_FAILED")
        self.session.stop.assert_called_once()
        self.assertEqual(result["stages"]["replay"], "not_run")
        self.assertNotIn("synthetic-private-credential", json.dumps(result))

    def test_stop_failure_retains_uncertain_marker(self):
        self.session.stop.side_effect = RuntimeError(
            "synthetic-private-credential"
        )
        result = self.run_mocked()
        self.assertTrue(self.blocked)
        self.assertEqual(result["code"], "SESSION_STOP_FAILED")
        self.assertEqual(result["exit_code"], 1)

    def test_client_creation_failure_never_falls_back(self):
        result = self.run_mocked(
            session_factory=Mock(side_effect=ImportError("missing"))
        )
        self.assertEqual(result["status"], "failed")
        self.assertTrue(self.blocked)
        self.session.stop.assert_not_called()

    def test_interrupt_keeps_durable_marker_even_if_client_stop_returns(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(KeyboardInterrupt):
                self.run_mocked(
                    lock_factory=lambda engine: runner.exclusive_run(
                        engine, root
                    ),
                    workload_function=Mock(side_effect=KeyboardInterrupt),
                )
            self.session.stop.assert_called_once()
            self.assertEqual(
                (root / "sail.lock").read_bytes(), b"RUN_IN_PROGRESS\n"
            )
            with self.assertRaisesRegex(
                contract.LabFailure, "PREVIOUS_RUN_UNCERTAIN"
            ):
                with runner.exclusive_run("sail", root):
                    pass

    def test_replay_value_change_fails(self):
        calls = 0

        def cross_read(engine, table):
            nonlocal calls
            calls += 1
            rows = self.expected[table]
            return rows[:-1] if calls > len(contract.TABLES) else rows

        result = self.run_mocked(cross_read=cross_read)
        self.assertEqual(result["code"], "REPLAY_MISMATCH")
        self.assertEqual(result["status"], "failed")

    def test_interrupt_during_stop_preserves_marker_and_interrupt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.session.stop.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                self.run_mocked(
                    lock_factory=lambda engine: runner.exclusive_run(
                        engine, root
                    )
                )
            self.assertEqual(
                (root / "sail.lock").read_bytes(), b"RUN_IN_PROGRESS\n"
            )
            with self.assertRaisesRegex(
                contract.LabFailure, "PREVIOUS_RUN_UNCERTAIN"
            ):
                with runner.exclusive_run("sail", root):
                    pass

    def test_create_once_and_owned_full_overwrite_not_replace(self):
        frame = FakeFrame(
            "silver_mes_events", self.expected["silver_mes_events"]
        )
        self.catalog.table.side_effect = [None, {"snapshot_id": "1"}]
        runner.write_table(
            self.session, self.catalog, "sail", "silver_mes_events", frame
        )
        self.assertTrue(
            self.session.statements[-1].startswith(
                "CREATE TABLE lakehouse.sail_lab."
            )
        )
        self.assertNotIn("REPLACE TABLE", self.session.statements[-1])
        self.catalog.table.side_effect = [
            {"snapshot_id": "1"},
            {"snapshot_id": "2"},
        ]
        runner.write_table(
            self.session, self.catalog, "sail", "silver_mes_events", frame
        )
        self.assertTrue(
            self.session.statements[-1].startswith(
                "INSERT OVERWRITE TABLE lakehouse.sail_lab."
            )
        )

    def test_compare_reports_requires_oracle_schema_replay_and_same_fixture(
        self,
    ):
        spark, sail = self.run_mocked("spark"), self.run_mocked("sail")
        self.assertEqual(
            evidence.compare_reports(spark, sail)["correctness"], "passed"
        )
        for change in ("hash", "schema", "replay", "fixture", "failed"):
            wrong = copy.deepcopy(sail)
            entry = wrong["tables"]["silver_erp_products"]
            if change == "hash":
                entry["engine"]["observed_sha256"] = "0" * 64
            elif change == "schema":
                entry["iceberg"]["nullable"][0] = False
            elif change == "replay":
                entry["replay_verified"] = False
            elif change == "fixture":
                wrong["fixture_sha256"] = "0" * 64
            else:
                wrong["status"] = "failed"
            with (
                self.subTest(change=change),
                self.assertRaises(contract.LabFailure),
            ):
                evidence.compare_reports(spark, wrong)

    def test_cli_plan_and_negative_probe_machine_json(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(runner.main(["plan"]), 0)
        self.assertEqual(
            json.loads(output.getvalue().split("=", 1)[1])["status"], "plan"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(runner.main(["timestamp7-probe"]), 2)
        self.assertIn("not-live-engine", output.getvalue())

    def test_public_cli_plan_has_no_ambient_pythonpath_requirement(self):
        """Run CI's real interface from an unrelated disposable directory."""
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="sail-cli-plan-") as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/sail_benchmark.py"),
                    "plan",
                ],
                cwd=directory,
                env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 0, "The public plan CLI failed.")
        receipt = json.loads(result.stdout.split("=", 1)[1])
        self.assertEqual(receipt["status"], "plan")
        workflow = (root / ".github/workflows/validate.yml").read_text()
        self.assertIn("python scripts/sail_benchmark.py plan", workflow)
        self.assertNotIn("python infra/sail/runner.py plan", workflow)

    def test_comparison_rejects_missing_stage_unknown_cleanup_and_bool_counts(
        self,
    ):
        spark, sail = self.run_mocked("spark"), self.run_mocked("sail")
        for mutation in ("stage", "cleanup", "bool", "both-wrong-schema"):
            left, right = copy.deepcopy(spark), copy.deepcopy(sail)
            if mutation == "stage":
                right["stages"] = {"one": "passed"}
            elif mutation == "cleanup":
                right["cleanup"] = "unknown"
            elif mutation == "bool":
                right["tables"]["silver_erp_products"]["engine"][
                    "missing_count"
                ] = False
            else:
                for report in (left, right):
                    report["tables"]["silver_erp_products"]["iceberg"][
                        "schema"
                    ][0][1] = "double"
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(contract.LabFailure),
            ):
                evidence.compare_reports(left, right)

    def test_nested_report_shapes_fail_with_fixed_diagnostics(self):
        spark, sail = self.run_mocked("spark"), self.run_mocked("sail")
        for field in ("tables", "stages", "entry", "engine", "iceberg"):
            wrong = copy.deepcopy(sail)
            if field in {"tables", "stages"}:
                wrong[field] = None
            elif field == "entry":
                wrong["tables"]["silver_erp_products"] = []
            else:
                wrong["tables"]["silver_erp_products"][field] = None
            with self.subTest(field=field):
                with self.assertRaises(contract.LabFailure):
                    evidence.compare_reports(spark, wrong)

    def test_ownership_failure_precedes_overwrite(self):
        self.catalog.table.side_effect = contract.LabFailure(
            "CATALOG_OWNERSHIP_MISMATCH"
        )
        with self.assertRaises(contract.LabFailure):
            runner.write_table(
                self.session,
                self.catalog,
                "sail",
                "silver_mes_events",
                FakeFrame("silver_mes_events", []),
            )
        self.assertEqual(self.session.statements, [])


class IsolatedLinuxStateTests(unittest.TestCase):
    """Actual owned-temp Linux filesystem checks; no host permission apply."""

    def test_lock_serialization_marker_and_no_metadata_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initial = root.stat().st_mtime_ns
            with runner.exclusive_run("sail", root):
                with self.assertRaisesRegex(contract.LabFailure, "RUN_ACTIVE"):
                    with runner.exclusive_run("sail", root):
                        pass
            self.assertEqual((root / "sail.lock").read_bytes(), b"")
            before = (root / "sail.lock").stat().st_ino
            with runner.exclusive_run("sail", root) as block:
                block()
            self.assertEqual((root / "sail.lock").stat().st_ino, before)
            self.assertEqual(
                (root / "sail.lock").read_bytes(), b"RUN_IN_PROGRESS\n"
            )
            with self.assertRaisesRegex(
                contract.LabFailure, "PREVIOUS_RUN_UNCERTAIN"
            ):
                with runner.exclusive_run("sail", root):
                    pass
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertGreaterEqual(root.stat().st_mtime_ns, initial)

    def test_lock_symlink_hardlink_and_unsafe_directory_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            sentinel = root / "notebook.txt"
            sentinel.write_text("unchanged")
            link = root / "spark.lock"
            link.symlink_to(sentinel)
            with self.assertRaises(contract.LabFailure):
                with runner.exclusive_run("spark", root):
                    pass
            link.unlink()
            os.link(sentinel, link)
            with self.assertRaises(contract.LabFailure):
                with runner.exclusive_run("spark", root):
                    pass
            self.assertEqual(sentinel.read_text(), "unchanged")
            root.chmod(0o755)
            with self.assertRaises(contract.LabFailure):
                with runner.exclusive_run("spark", root):
                    pass

    def test_private_evidence_record_extracts_only_one_tagged_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "summary.json"
            path.write_text(
                'JVM progress\nLAKEHOUSE_SAIL_RESULT={"status":"plan"}\n'
            )
            path.chmod(0o600)
            self.assertEqual(evidence.read_report(path), {"status": "plan"})
            path.write_text(
                "LAKEHOUSE_SAIL_RESULT={}\nLAKEHOUSE_SAIL_RESULT={}\n"
            )
            with self.assertRaises(contract.LabFailure):
                evidence.read_report(path)
            path.chmod(0o644)
            with self.assertRaises(contract.LabFailure):
                evidence.read_report(path)

    def test_evidence_fifo_rejected_without_open_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            fifo = Path(folder) / "synthetic-fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(contract.LabFailure):
                evidence.read_report(fifo)


class ServerPackagingTests(unittest.TestCase):
    """Pin/protocol configuration tests, not package installation evidence."""

    def test_existing_private_keys_map_to_documented_native_sail_settings(
        self,
    ):
        values = {
            "WAREHOUSE_NAME": "synthetic-warehouse",
            "S3_ACCESS_KEY": "synthetic-key",
            "S3_SECRET_KEY": "synthetic-secret",
        }
        mapped = server.environment(values)
        self.assertEqual(mapped["AWS_ENDPOINT"], "http://storage:8333")
        self.assertEqual(mapped["AWS_ACCESS_KEY_ID"], values["S3_ACCESS_KEY"])
        self.assertEqual(mapped["AWS_VIRTUAL_HOSTED_STYLE_REQUEST"], "false")
        self.assertIn('type="iceberg-rest"', mapped["SAIL_CATALOG__LIST"])
        self.assertNotIn("synthetic-secret", mapped["SAIL_CATALOG__LIST"])
        self.assertNotIn("spark.sql.catalog", json.dumps(mapped))

    def test_missing_private_configuration_fails_without_value_leak(self):
        with self.assertRaisesRegex(
            ValueError, "^SAIL_CONFIGURATION_INVALID$"
        ):
            server.environment(
                {"S3_SECRET_KEY": "synthetic-private-credential"}
            )

    def test_official_binary_only_server_and_local_derived_client(self):
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "infra/sail/Dockerfile").read_text()
        self.assertIn("--only-binary :all:", dockerfile)
        self.assertNotIn("cargo", dockerfile)
        self.assertNotIn("git clone", dockerfile)
        self.assertIn("USER ${WORKER_UID}:${WORKER_GID}", dockerfile)
        lock = (root / "infra/sail/requirements-server.lock").read_text()
        self.assertIn("pysail==0.7.3", lock)
        self.assertEqual(lock.count("--hash=sha256:"), 2)
        client = (root / "infra/sail/Dockerfile.client").read_text()
        self.assertIn("ARG SPARK_IMAGE", client)
        self.assertIn("FROM ${SPARK_IMAGE}", client)
        self.assertIn("UV_OFFLINE=0", client)
        self.assertNotIn("pyspark==", client)

    def test_optional_compose_never_auto_runs_or_exposes_connect(self):
        root = Path(__file__).resolve().parents[1]
        config = yaml.safe_load((root / "infra/sail/compose.yaml").read_text())
        services = config["services"]
        self.assertEqual(set(services), {"sail-server", "sail-benchmark"})
        for service in services.values():
            self.assertEqual(service["profiles"], ["sail-lab"])
            self.assertNotIn("ports", service)
            self.assertNotIn("privileged", service)
            self.assertNotIn("network_mode", service)
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertNotIn("SQLSERVER", json.dumps(service))
            environment = service["environment"]
            self.assertTrue(
                {
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "NO_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "no_proxy",
                }.issubset(environment)
            )
            self.assertEqual(environment["NO_PROXY"], environment["no_proxy"])
        client = services["sail-benchmark"]
        self.assertEqual(client["deploy"]["replicas"], 0)
        self.assertEqual(client["command"], ["plan"])
        # This service owns its derived image build. Only shared-image
        # consumers without a build definition use pull_policy: never.
        self.assertEqual(client["pull_policy"], "build")
        self.assertEqual(
            client["build"]["dockerfile"], "infra/sail/Dockerfile.client"
        )
        self.assertEqual(
            client["build"]["args"]["SPARK_IMAGE"],
            "${COMPOSE_PROJECT_NAME:-lakehouse}-spark-check:local",
        )
        self.assertEqual(
            client["volumes"], ["sail-lab-state:/opt/lakehouse/lab-state"]
        )
        self.assertEqual(set(config["volumes"]), {"sail-lab-state"})

    def test_recorded_upstream_evidence_matches_exact_binary_lock(self):
        root = Path(__file__).resolve().parents[1]
        directory = root / "infra/sail"
        manifest = json.loads(
            (directory / "upstream-evidence.json").read_text()
        )
        lock = (directory / "requirements-server.lock").read_text()
        self.assertEqual(manifest["sail_version"], "0.7.3")
        self.assertIsNone(manifest["official_sail_container_digest"])
        self.assertEqual(manifest["rest_catalog_write"]["runtime"], "not_run")
        self.assertEqual(manifest["spark_connect_protocol_runtime"], "not_run")
        self.assertEqual(len(manifest["linux_wheels"]), 2)
        for filename, digest in manifest["linux_wheels"].items():
            self.assertTrue(filename.startswith("pysail-0.7.3-cp38-abi3-"))
            self.assertIn("--hash=sha256:" + digest, lock)
        for key in ("python",):
            pinned = json.loads(
                (root / "infra/lakehouse/images.lock.json").read_text()
            )[key]["pinned"]
            self.assertIn(pinned, (directory / "Dockerfile").read_text())


if __name__ == "__main__":
    unittest.main()
