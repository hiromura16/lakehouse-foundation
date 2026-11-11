"""Test fixed-source snapshot contracts with a fake SQL driver, never a DB."""

import contextlib
import copy
import importlib.util
import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "fixed_snapshot_subject", ROOT / "scripts/sqlserver_batch.py"
)
BATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BATCH)
CONTRACTS = json.loads((ROOT / "medallion/sources.json").read_text())[
    "sources"
]
TABLES = {
    source["table"]: {
        "schema": "lh_demo_erp"
        if source["table"].startswith("erp_")
        else ("lh_demo_mes"),
        "table": source["table"].split("_", 1)[1],
        "columns": source["columns"],
        "pk": source["columns"][0],
    }
    for source in CONTRACTS
}
OBSERVED = "2026-04-01T12:00:00.1234567Z"
UPDATED = "2026-04-01T11:00:00.7654321"
RUN_ID = "012345abcdef"
VALUES = {
    "SOURCE_SQLSERVER_HOST": "sql-source.example.invalid",
    "SOURCE_SQLSERVER_PORT": "1433",
    "SOURCE_SQLSERVER_DATABASE": "demo_database",
    "SOURCE_SQLSERVER_TIMEZONE": "UTC",
    "SOURCE_SQLSERVER_USER": "example_reader",
    "SOURCE_SQLSERVER_PASSWORD": "synthetic-reader-value",
    "DEMO_SQLSERVER_INSTALLATION_ID": "a" * 32,
}


def fixture_data():
    """Include original values that CSV cannot distinguish."""
    data = {}
    for source in CONTRACTS:
        row = {name: "value" for name in source["columns"]}
        row[source["columns"][0]] = "K1"
        row["updated_at"] = "2026-01-05T08:00:00.0000001Z"
        if source["table"] == "erp_products":
            row.update(
                product_name="日本語の製品",
                product_family=None,
                unit="",
                standard_cycle_seconds="123456789.123",
            )
        if source["table"] == "erp_sales_orders":
            row["unit_price"] = "1234567890123456.78"
        data[source["table"]] = [
            (
                row[source["columns"][0]],
                json.dumps(row, ensure_ascii=False),
                UPDATED,
                RUN_ID,
            )
        ]
    return data


class Cursor:
    """Interpret only the fixed SELECT/SET protocol used by the reader."""

    def __init__(
        self,
        data,
        observed=OBSERVED,
        *,
        mismatch=False,
        oversize=False,
        fail_table=None,
    ):
        self.data = data
        self.observed = observed
        self.mismatch = mismatch
        self.oversize = oversize
        self.fail_table = fail_table
        self.statements = []
        self.pending = []

    def execute(self, statement, parameters=None):
        """Retain issued SQL so tests can reject source DML and weak reads."""
        self.statements.append((statement, parameters))
        if statement.startswith("SET "):
            self.pending = []
            return
        if "SYSUTCDATETIME()" in statement:
            self.pending = [(self.observed.removesuffix("Z"),)]
            return
        name = next(
            name
            for name, table in TABLES.items()
            if "[" + table["schema"] + "].[" + table["table"] + "]"
            in statement
        )
        if self.fail_table == name:
            raise RuntimeError("synthetic-private-driver-detail")
        rows = self.data[name]
        if "WITH (TABLOCK, HOLDLOCK)" in statement:
            self.pending = [(rows[0][0],)] if rows else []
        elif "COUNT_BIG" in statement:
            self.pending = [(len(rows) + int(self.mismatch),)]
        elif "DATALENGTH" in statement:
            self.pending = [(1,)] if self.oversize else []
        else:
            self.pending = list(rows)

    def fetchone(self):
        """Return one prepared row, including metadata/count observations."""
        return self.pending.pop(0) if self.pending else None

    def fetchmany(self, size):
        """Read bounded driver chunks rather than preloading all SQL rows."""
        result = self.pending[:size]
        del self.pending[:size]
        return result


def adapter_fixture(data=None, **options):
    """Inject the reader connection, ownership and permission checks."""
    cursor = Cursor(data or fixture_data(), **options)
    handle = SimpleNamespace(cursor=lambda: cursor, commit=mock.Mock())
    roles = []

    @contextlib.contextmanager
    def connection(values, *, role):
        del values
        roles.append(role)
        yield handle

    adapter = SimpleNamespace(
        TABLES=TABLES,
        validate_settings=mock.Mock(side_effect=lambda values, **_: values),
        connection=connection,
        ownership=mock.Mock(),
        permissions_safe=mock.Mock(return_value=True),
        table_name=lambda name: (
            "[" + TABLES[name]["schema"] + "].[" + TABLES[name]["table"] + "]"
        ),
    )
    return adapter, cursor, handle, roles


class SnapshotReadTests(unittest.TestCase):
    """Exercise export contracts without SQL Server integration."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sql-snapshot-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.landing = self.directory / "landing"
        self.adapter, self.cursor, self.handle, self.roles = adapter_fixture()

    def prepare(self, **options):
        """Prepare a batch using synthetic settings and an injected reader."""
        return BATCH.prepare_snapshot(
            VALUES, self.landing, adapter=self.adapter, **options
        )

    def document(self, prepared):
        """Inspect private test output without printing its source contents."""
        path = self.landing / "sql-batches" / (prepared["batch_id"] + ".json")
        return json.loads(path.read_bytes())

    def test_snapshot_preserves_null_empty_japanese_decimal_and_seventh_digit(
        self,
    ):
        prepared = self.prepare()
        document = self.document(prepared)
        product = document["tables"]["erp_products"]["rows"][0]
        self.assertEqual(product["values"]["product_name"], "日本語の製品")
        self.assertIsNone(product["values"]["product_family"])
        self.assertEqual(product["values"]["unit"], "")
        self.assertEqual(
            product["values"]["standard_cycle_seconds"], "123456789.123"
        )
        self.assertEqual(product["source_updated_at"], UPDATED + "Z")
        self.assertEqual(
            product["values"]["updated_at"], "2026-01-05T08:00:00.0000001Z"
        )
        price = document["tables"]["erp_sales_orders"]["rows"][0]
        self.assertEqual(price["values"]["unit_price"], "1234567890123456.78")
        self.assertEqual(prepared["observed_at"], OBSERVED)

    def test_reader_is_separate_and_issues_only_fixed_select_and_set(self):
        self.prepare()
        self.assertEqual(self.roles, ["source"])
        self.adapter.validate_settings.assert_called_once_with(
            VALUES, role="source"
        )
        self.adapter.ownership.assert_called_once_with(
            self.cursor, VALUES, role="source"
        )
        self.adapter.permissions_safe.assert_called_once_with(
            self.cursor, role="source"
        )
        self.handle.commit.assert_called_once()
        statements = [sql for sql, _ in self.cursor.statements]
        self.assertTrue(
            all(sql.startswith(("SELECT ", "SET ")) for sql in statements)
        )
        self.assertIn("SET LOCK_TIMEOUT 5000", statements)
        self.assertIn(
            "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE", statements
        )
        locks = [i for i, sql in enumerate(statements) if "TABLOCK" in sql]
        observation = next(
            i for i, sql in enumerate(statements) if "SYSUTCDATETIME()" in sql
        )
        self.assertEqual(len(locks), 6)
        self.assertLess(max(locks), observation)
        self.assertEqual(
            sum(
                "COLLATE Latin1_General_100_BIN2" in sql for sql in statements
            ),
            6,
        )

    def test_unchanged_source_has_stable_content_id_across_new_observations(
        self,
    ):
        first = self.prepare()
        self.cursor.observed = "2026-04-01T12:01:00.0000001Z"
        second = self.prepare()
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertNotEqual(first["observed_at"], second["observed_at"])
        self.assertNotEqual(
            BATCH.apply_identifier(first["batch_id"], first["observed_at"]),
            BATCH.apply_identifier(second["batch_id"], second["observed_at"]),
        )
        files = list((self.landing / "sql-batches").iterdir())
        self.assertEqual(len(files), 2)
        ready = next(path for path in files if path.name.endswith(".ready"))
        self.assertEqual(ready.read_text(), first["batch_id"] + "\n")

    def test_different_endpoint_changes_identity_without_exporting_credentials(
        self,
    ):
        first = self.prepare()
        altered = dict(VALUES, SOURCE_SQLSERVER_HOST="other.example.invalid")
        second = BATCH.prepare_snapshot(
            altered, self.landing, adapter=self.adapter
        )
        self.assertNotEqual(first["batch_id"], second["batch_id"])
        output = json.dumps(self.document(second))
        for key in (
            "SOURCE_SQLSERVER_HOST",
            "SOURCE_SQLSERVER_USER",
            "SOURCE_SQLSERVER_PASSWORD",
        ):
            self.assertNotIn(altered[key], output)

    def test_count_mismatch_or_partial_reader_failure_publishes_nothing(self):
        for options in ({"mismatch": True}, {"fail_table": "mes_events"}):
            with self.subTest(options=options):
                self.adapter, _, _, _ = adapter_fixture(**options)
                with self.assertRaises(BATCH.BatchError) as error:
                    self.prepare()
                self.assertNotIn(
                    "synthetic-private-driver-detail", str(error.exception)
                )
                self.assertFalse(self.landing.exists())

    def test_permissions_or_ownership_failure_happens_before_source_reads(
        self,
    ):
        self.adapter.permissions_safe.return_value = False
        with self.assertRaises(BATCH.BatchError):
            self.prepare()
        self.assertFalse(
            any("FROM " in sql for sql, _ in self.cursor.statements)
        )
        self.assertFalse(self.landing.exists())
        self.adapter.permissions_safe.return_value = True
        self.adapter.ownership.side_effect = RuntimeError("private-ownership")
        with self.assertRaises(BATCH.BatchError) as error:
            self.prepare()
        self.assertNotIn("private-ownership", str(error.exception))

    def test_table_and_global_bounds_prevent_publication(self):
        for bound in (0, True, 100001):
            with self.subTest(bound=bound):
                with self.assertRaises(BATCH.BatchError):
                    self.prepare(max_rows=bound)
        with mock.patch.object(BATCH, "MAX_BYTES", 33000):
            with self.assertRaises(BATCH.BatchError):
                self.prepare()
        self.assertFalse(self.landing.exists())

    def test_oversized_sql_nvarchar_is_rejected_before_driver_fetch(self):
        self.adapter, cursor, _, _ = adapter_fixture(oversize=True)
        with self.assertRaises(BATCH.BatchError):
            self.prepare()
        self.assertTrue(
            any("DATALENGTH" in sql for sql, _ in cursor.statements)
        )
        self.assertFalse(
            any("[original_values]," in sql for sql, _ in cursor.statements)
        )
        self.assertFalse(self.landing.exists())

    def test_invalid_originals_duplicate_keys_and_native_pk_mismatch_fail(
        self,
    ):
        source = fixture_data()
        row = json.loads(source["erp_products"][0][1])
        failures = [
            "{not-json}",
            json.dumps({**row, "product_id": "OTHER"}),
            json.dumps({**row, "product_name": 1.25}),
            json.dumps({**row, "extra": "unsupported"}),
            '{"product_id":"K1","product_id":"K2"}',
        ]
        for payload in failures:
            with self.subTest(payload_kind="invalid original"):
                altered = copy.deepcopy(source)
                altered["erp_products"][0] = ("K1", payload, UPDATED, RUN_ID)
                self.adapter, _, _, _ = adapter_fixture(altered)
                with self.assertRaises(BATCH.BatchError):
                    self.prepare()
        altered = copy.deepcopy(source)
        altered["erp_products"].append(altered["erp_products"][0])
        self.adapter, _, _, _ = adapter_fixture(altered)
        with self.assertRaises(BATCH.BatchError):
            self.prepare()
        self.assertFalse(self.landing.exists())

    def test_source_timezone_and_output_redirection_are_rejected(self):
        with self.assertRaises(BATCH.BatchError):
            BATCH.prepare_snapshot(
                dict(VALUES, SOURCE_SQLSERVER_TIMEZONE="Asia/Tokyo"),
                self.landing,
                adapter=self.adapter,
            )
        linked = self.directory / "linked"
        linked.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(BATCH.BatchError):
            BATCH.prepare_snapshot(VALUES, linked, adapter=self.adapter)

    def test_only_exact_completed_worker_ack_is_returned(self):
        observed = []

        def worker(batch_id, observed_at):
            observed.append((batch_id, observed_at))
            return {
                "batch_id": batch_id,
                "observed_at": observed_at,
                "apply_id": BATCH.apply_identifier(batch_id, observed_at),
                "completed": True,
                "counts": {name: 1 for name in TABLES},
                "skipped": False,
                "sql_statements": 14,
                "driver_detail": "private-extra-value",
            }

        result = BATCH.run_snapshot(
            VALUES, self.landing, worker, adapter=self.adapter
        )
        self.assertEqual(observed, [(result["batch_id"], OBSERVED)])
        self.assertNotIn("driver_detail", result)
        self.assertTrue(result["completed"])
        prepared = self.prepare()
        for key, invalid in (
            ("completed", False),
            ("apply_id", "a" * 64),
            ("observed_at", "2026-04-02T00:00:00.0000000Z"),
            ("counts", {}),
            ("skipped", 1),
        ):
            with self.subTest(field=key):
                ack = worker(prepared["batch_id"], prepared["observed_at"])
                ack[key] = invalid
                with self.assertRaises(BATCH.BatchError):
                    BATCH.validate_acknowledgement(ack, prepared)

    def test_timestamp_validation_keeps_seven_digits_without_float_roundtrip(
        self,
    ):
        self.assertEqual(BATCH.canonical_timestamp(UPDATED), UPDATED + "Z")
        self.assertEqual(
            BATCH.canonical_timestamp("2026-04-01 12:00:00.1"),
            "2026-04-01T12:00:00.1000000Z",
        )
        for value in (
            "2026-02-30T00:00:00",
            1710000000.1,
            "2026-04-01T12:00:00.12345678",
        ):
            with self.assertRaises(BATCH.BatchError):
                BATCH.canonical_timestamp(value)

    def test_content_id_matches_exact_private_file_bytes(self):
        result = self.prepare()
        path = self.landing / "sql-batches" / (result["batch_id"] + ".json")
        self.assertEqual(
            sha256(path.read_bytes()).hexdigest(), result["batch_id"]
        )
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class WindowCursor(Cursor):
    """Model only the fixed composite range predicate, not a SQL engine."""

    def execute(self, statement, parameters=None):
        super().execute(statement, parameters)
        if "WHERE ([updated_at]" not in statement:
            return
        name = next(
            name
            for name, table in TABLES.items()
            if "[" + table["schema"] + "].[" + table["table"] + "]"
            in statement
        )
        lower, _, key, upper = parameters
        lower, upper = (
            BATCH.canonical_timestamp(value) for value in (lower, upper)
        )
        rows = [
            row
            for row in self.data[name]
            if (
                BATCH.canonical_timestamp(row[2]) > lower
                or (
                    BATCH.canonical_timestamp(row[2]) == lower and row[0] > key
                )
            )
            and BATCH.canonical_timestamp(row[2]) <= upper
        ]
        rows.sort(key=lambda row: (row[2], row[0]))
        self.pending = [(len(rows),)] if "COUNT_BIG" in statement else rows


class ControlCursor:
    """Stage watermark changes until a simulated transaction commits."""

    def __init__(self, connection):
        self.connection = connection
        self.pending = []
        self.rowcount = -1
        self.statements = []

    def execute(self, statement, parameters=None):
        self.statements.append((statement, parameters))
        connection = self.connection
        if statement.startswith("SET "):
            return
        if statement.startswith("SELECT "):
            if connection.busy:
                raise RuntimeError("synthetic-private-lock-error")
            connection.leased = True
            self.pending = [
                (
                    name,
                    row["timestamp"].removesuffix("Z")
                    if row["timestamp"]
                    else None,
                    row["primary_key"],
                    row["snapshot_id"],
                )
                for name, row in sorted(connection.working.items())
            ][:7]
            return
        if statement.startswith("INSERT "):
            name, stamp, key, batch_id = parameters
            if name in connection.working:
                raise RuntimeError("synthetic-duplicate-checkpoint")
        elif statement.startswith("UPDATE "):
            (
                stamp,
                key,
                batch_id,
                name,
                previous,
                _,
                previous_key,
                _,
                (previous_batch),
            ) = parameters
            expected = {
                "timestamp": BATCH.canonical_timestamp(previous)
                if previous
                else None,
                "primary_key": previous_key,
                "snapshot_id": previous_batch,
            }
            if (
                connection.cas_failure
                or connection.working.get(name) != expected
            ):
                self.rowcount = 0
                return
        else:
            raise AssertionError("Unexpected control statement")
        connection.events.append("control-dml:" + name)
        if connection.fail_table == name:
            raise RuntimeError("synthetic-private-control-write-error")
        connection.working[name] = {
            "timestamp": BATCH.canonical_timestamp(stamp) if stamp else None,
            "primary_key": key,
            "snapshot_id": batch_id,
        }
        self.rowcount = 1

    def fetchmany(self, size):
        result = self.pending[:size]
        del self.pending[:size]
        return result


class ControlConnection:
    """Verify client transactions without database integration."""

    def __init__(self):
        self.committed = {}
        self.working = {}
        self.events = []
        self.leased = False
        self.busy = False
        self.cas_failure = False
        self.fail_table = None
        self.commits = 0
        self.rollbacks = 0
        self.handle = ControlCursor(self)

    def cursor(self):
        return self.handle

    def commit(self):
        self.events.append("control-commit")
        self.committed = copy.deepcopy(self.working)
        self.commits += 1
        self.leased = False

    def rollback(self):
        self.events.append("control-rollback")
        self.working = copy.deepcopy(self.committed)
        self.rollbacks += 1
        self.leased = False


class WatermarkBatchTests(unittest.TestCase):
    """Exercise separate roles, composite windows and commit gates."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sql-window-test-")
        self.addCleanup(temporary.cleanup)
        self.landing = Path(temporary.name) / "landing"
        self.values = dict(
            VALUES,
            DEMO_SQLSERVER_USER="example_writer",
            SOURCE_SQLSERVER_CONTROL_USER="example_control",
            SOURCE_SQLSERVER_CONTROL_PASSWORD="synthetic-control-value",
        )
        self.control = ControlConnection()
        self.source = WindowCursor(fixture_data())
        self.source_handle = SimpleNamespace(
            cursor=lambda: self.source,
            commit=mock.Mock(
                side_effect=lambda: self.control.events.append(
                    "source-read-commit"
                )
            ),
        )
        self.roles = []

        @contextlib.contextmanager
        def connection(values, *, role):
            del values
            self.roles.append(role)
            if role == "source":
                yield self.source_handle
            else:
                self.assertEqual(role, "control")
                self.control.working = copy.deepcopy(self.control.committed)
                try:
                    yield self.control
                except Exception:
                    self.control.rollback()
                    raise

        def validate(values, *, role):
            if role == "control":
                names = [
                    values.get(key)
                    for key in (
                        "SOURCE_SQLSERVER_USER",
                        "DEMO_SQLSERVER_USER",
                        "SOURCE_SQLSERVER_CONTROL_USER",
                    )
                ]
                if (
                    not all(names)
                    or len(set(names)) != 3
                    or not values.get("SOURCE_SQLSERVER_CONTROL_PASSWORD")
                ):
                    raise BATCH.BatchError(
                        "Separate control credentials required."
                    )
            return values

        self.adapter = SimpleNamespace(
            TABLES=TABLES,
            validate_settings=mock.Mock(side_effect=validate),
            connection=connection,
            ownership=mock.Mock(),
            permissions_safe=mock.Mock(return_value=True),
            table_name=lambda name: (
                "["
                + TABLES[name]["schema"]
                + "].["
                + TABLES[name]["table"]
                + "]"
            ),
        )
        self.seen = []

    def worker(self, batch_id, observed_at):
        """Confirm the exact exported contract without a fake live service."""
        self.assertTrue(self.control.leased)
        self.assertIn("source-read-commit", self.control.events)
        self.control.events.append("worker-ack")
        body = json.loads(
            (self.landing / "sql-batches" / (batch_id + ".json")).read_bytes()
        )
        self.seen.append(body)
        return {
            "batch_id": batch_id,
            "observed_at": observed_at,
            "apply_id": BATCH.apply_identifier(batch_id, observed_at),
            "completed": True,
            "counts": {
                name: len(table["rows"])
                for name, table in body["tables"].items()
            },
            "skipped": False,
            "sql_statements": 14,
        }

    def snapshot(self, worker=None):
        return BATCH.run_snapshot(
            self.values,
            self.landing,
            worker or self.worker,
            adapter=self.adapter,
        )

    def incremental(self, worker=None):
        return BATCH.run_incremental(
            self.values,
            self.landing,
            worker or self.worker,
            adapter=self.adapter,
        )

    def test_snapshot_seeds_all_six_only_after_exact_worker_completion(self):
        result = self.snapshot()
        self.assertTrue(result["checkpointed"])
        self.assertEqual(self.roles, ["control", "source"])
        self.assertEqual(set(self.control.committed), set(TABLES))
        self.assertEqual(self.control.commits, 1)
        events = self.control.events
        self.assertLess(
            events.index("source-read-commit"), events.index("worker-ack")
        )
        self.assertLess(
            events.index("worker-ack"),
            events.index("control-dml:erp_products"),
        )
        self.assertEqual(events[-1], "control-commit")
        for row in self.control.committed.values():
            self.assertEqual(row["timestamp"], UPDATED + "Z")
            self.assertEqual(row["primary_key"], "K1")
            self.assertEqual(row["snapshot_id"], result["batch_id"])
        control_sql = [sql for sql, _ in self.control.handle.statements]
        self.assertTrue(
            any("WITH (UPDLOCK, HOLDLOCK)" in sql for sql in control_sql)
        )
        self.assertTrue(
            all(
                sql.startswith(("SET ", "SELECT ", "INSERT "))
                for sql in control_sql
            )
        )
        self.assertTrue(
            all(
                sql.startswith(("SET ", "SELECT "))
                for sql, _ in self.source.statements
            )
        )

    def test_incremental_needs_all_baselines_and_never_falls_back_to_reader(
        self,
    ):
        for update in (
            {
                "SOURCE_SQLSERVER_CONTROL_USER": "",
                "SOURCE_SQLSERVER_CONTROL_PASSWORD": "",
            },
            {"SOURCE_SQLSERVER_CONTROL_PASSWORD": ""},
            {"SOURCE_SQLSERVER_CONTROL_USER": VALUES["SOURCE_SQLSERVER_USER"]},
        ):
            with self.subTest(configuration=tuple(update)):
                with self.assertRaises(BATCH.BatchError):
                    BATCH.run_incremental(
                        {**self.values, **update},
                        self.landing,
                        self.worker,
                        adapter=self.adapter,
                    )
        self.assertEqual(self.roles, [])
        with self.assertRaises(BATCH.BatchError):
            self.incremental()
        self.assertNotIn("source", self.roles)
        self.assertFalse(self.landing.exists())

    def test_equal_timestamp_pk_tie_overlap_late_arrival_and_safety_lag(self):
        self.snapshot()
        product = self.source.data["erp_products"][0]
        original = json.loads(product[1])
        extra = []
        for key, stamp in (
            ("K2", UPDATED),
            ("LATE", "2026-04-01T10:59:30.7654321"),
            ("OUTSIDE", "2026-04-01T10:50:00.7654321"),
            ("DEFERRED", "2026-04-01T11:59:58.0000001"),
        ):
            extra.append(
                (
                    key,
                    json.dumps({**original, "product_id": key}),
                    stamp,
                    RUN_ID,
                )
            )
        self.source.data["erp_products"] += extra
        result = self.incremental()
        body = self.seen[-1]
        self.assertEqual(body["mode"], "incremental")
        self.assertEqual(body["overlap_seconds"], 300)
        self.assertEqual(body["safety_lag_seconds"], 5)
        self.assertEqual(body["upper_bound"], "2026-04-01T11:59:55.1234567Z")
        selected = body["tables"]["erp_products"]["rows"]
        self.assertEqual(
            {row["source_pk"] for row in selected}, {"K1", "K2", "LATE"}
        )
        self.assertEqual(result["counts"]["erp_products"], 3)
        checkpoint = self.control.committed["erp_products"]
        self.assertEqual(checkpoint["timestamp"], UPDATED + "Z")
        self.assertEqual(checkpoint["primary_key"], "K2")
        self.assertTrue(
            all(
                sql.startswith(("SET ", "SELECT "))
                for sql, _ in self.source.statements
            )
        )

    def test_overlap_cannot_regress_the_composite_checkpoint(self):
        self.snapshot()
        previous = copy.deepcopy(self.control.committed)
        self.source.data["erp_products"][0] = (
            self.source.data["erp_products"][0][0],
            self.source.data["erp_products"][0][1],
            "2026-04-01T10:59:30.0000001",
            RUN_ID,
        )
        self.incremental()
        for name in TABLES:
            self.assertEqual(
                self.control.committed[name]["timestamp"],
                previous[name]["timestamp"],
            )
            self.assertEqual(
                self.control.committed[name]["primary_key"],
                previous[name]["primary_key"],
            )

    def test_worker_unknown_or_invalid_ack_rolls_back_every_checkpoint(self):
        self.snapshot()
        before = copy.deepcopy(self.control.committed)
        commits = self.control.commits

        class WorkerUnknown(RuntimeError):
            """Represent a safe unknown-worker-outcome exception."""

        def unknown(*_):
            raise WorkerUnknown("worker outcome unknown")

        with self.assertRaises(WorkerUnknown):
            self.incremental(unknown)
        self.assertEqual(self.control.committed, before)
        with self.assertRaises(BATCH.BatchError):
            self.incremental(lambda *_: {"completed": True})
        self.assertEqual(self.control.committed, before)
        self.assertEqual(self.control.commits, commits)
        self.assertGreaterEqual(self.control.rollbacks, 2)

    def test_partial_control_dml_failure_and_cas_conflict_preserve_baseline(
        self,
    ):
        self.snapshot()
        before = copy.deepcopy(self.control.committed)
        commits = self.control.commits
        self.control.fail_table = "erp_sales_orders"
        with self.assertRaises(BATCH.BatchError) as error:
            self.incremental()
        self.assertNotIn(
            "synthetic-private-control-write-error", str(error.exception)
        )
        self.assertEqual(self.control.committed, before)
        self.assertEqual(self.control.commits, commits)
        self.control.fail_table = None
        self.control.cas_failure = True
        with self.assertRaises(BATCH.BatchError):
            self.incremental()
        self.assertEqual(self.control.committed, before)

    def test_busy_control_lease_does_not_read_or_publish_source(self):
        self.control.busy = True
        with self.assertRaises(BATCH.BatchError) as error:
            self.snapshot()
        self.assertNotIn("synthetic-private-lock-error", str(error.exception))
        self.assertEqual(self.source.statements, [])
        self.assertFalse(self.landing.exists())

    def test_partial_or_unknown_checkpoint_rows_are_not_repaired_silently(
        self,
    ):
        self.control.committed = {
            "erp_products": {
                "timestamp": UPDATED + "Z",
                "primary_key": "K1",
                "snapshot_id": "a" * 64,
            }
        }
        with self.assertRaises(BATCH.BatchError):
            self.snapshot()
        self.assertEqual(self.source.statements, [])

    def test_empty_snapshot_seeds_null_positions_then_accepts_first_rows(self):
        self.source.data = {name: [] for name in TABLES}
        self.snapshot()
        self.assertEqual(set(self.control.committed), set(TABLES))
        self.assertTrue(
            all(
                row["timestamp"] is None and row["primary_key"] is None
                for row in self.control.committed.values()
            )
        )
        self.source.data = fixture_data()
        self.incremental()
        self.assertTrue(
            all(
                row["timestamp"] == UPDATED + "Z"
                for row in self.control.committed.values()
            )
        )

    def test_reader_failure_preserves_control_and_never_calls_worker(self):
        self.snapshot()
        before = copy.deepcopy(self.control.committed)
        self.source.fail_table = "mes_events"
        with self.assertRaises(BATCH.BatchError):
            self.incremental()
        self.assertEqual(self.control.committed, before)
        self.assertEqual(len(self.seen), 1)

    def test_policy_is_strict_and_preserves_seventh_digit_in_subtraction(self):
        self.assertEqual(BATCH.batch_policy({}), (300, 5))
        self.assertEqual(
            BATCH.subtract_seconds(OBSERVED, 5), "2026-04-01T11:59:55.1234567Z"
        )
        for changes in (
            {"SOURCE_SQLSERVER_BATCH_OVERLAP_SECONDS": "0"},
            {"SOURCE_SQLSERVER_BATCH_OVERLAP_SECONDS": "yes"},
            {"SOURCE_SQLSERVER_BATCH_SAFETY_LAG_SECONDS": "-1"},
            {"SOURCE_SQLSERVER_BATCH_OVERLAP_SECONDS": "3"},
        ):
            with self.subTest(policy=tuple(changes)):
                with self.assertRaises(BATCH.BatchError):
                    BATCH.run_incremental(
                        {**self.values, **changes},
                        self.landing,
                        self.worker,
                        adapter=self.adapter,
                    )
        self.assertEqual(self.roles, [])


if __name__ == "__main__":
    unittest.main()
