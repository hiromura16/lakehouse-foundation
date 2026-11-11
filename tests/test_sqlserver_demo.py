"""Unit tests for fixed synthetic SQL safety; no SQL Server is contacted.

Pure conversion tests and injected cursor/driver tests are deliberately
separate from the DDL's static contract tests. A mock result is not evidence
that SQL Server accepted a statement or that CDC delivered a real event.
"""

import copy
import importlib.util
import json
import os
import re
import sys
import unittest
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    """Load the real implementation without a CLI or connection side effect."""
    specification = importlib.util.spec_from_file_location(
        name, ROOT / relative
    )
    result = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(result)
    return result


DEMO = load("fixed_sql_unit", "scripts/sqlserver_demo.py")
GENERATOR = load("fixed_sql_generator", "scripts/generate-demo.py")
RUN = "0123456789ab"
VALUES = {
    "DEMO_SQLSERVER_HOST": "demo-db.example.invalid",
    "DEMO_SQLSERVER_PORT": "1433",
    "DEMO_SQLSERVER_DATABASE": "synthetic_demo",
    "DEMO_SQLSERVER_USER": "synthetic_writer",
    "DEMO_SQLSERVER_PASSWORD": "synthetic-writer-secret",
    "SOURCE_SQLSERVER_HOST": "demo-db.example.invalid",
    "SOURCE_SQLSERVER_PORT": "1433",
    "SOURCE_SQLSERVER_DATABASE": "synthetic_demo",
    "SOURCE_SQLSERVER_USER": "synthetic_reader",
    "SOURCE_SQLSERVER_PASSWORD": "synthetic-reader-secret",
    "SOURCE_SQLSERVER_CONTROL_USER": "synthetic_control",
    "SOURCE_SQLSERVER_CONTROL_PASSWORD": "synthetic-control-secret",
    "DEMO_SQLSERVER_ALLOW_SHARED_SOURCE_DATABASE": "true",
    "DEMO_SQLSERVER_INSTALLATION_ID": "a" * 32,
    "DEMO_SQLSERVER_OWNERSHIP_TOKEN": "b" * 64,
}


class Cursor:
    """Record parameterized DML and maintain only synthetic in-memory rows."""

    def __init__(self):
        self.calls = []
        self.rows = {}
        self.next = None
        self.fail_insert = None

    def execute(self, statement, arguments=()):
        """Interpret the tested DML subset; never delegate to a SQL driver."""
        self.calls.append((statement, arguments))
        self.next = None
        table = re.search(r"\[lh_demo_(?:erp|mes)\]\.\[[a-z_]+\]", statement)
        if (
            statement.startswith("SELECT ")
            and ", run_id, original_values FROM " in statement
        ):
            # Model a case-insensitive SQL collation with space padding, while
            # keeping the actual stored PK visible in the returned row.
            requested = arguments[0].casefold().rstrip(" ")
            for (origin, key), values in self.rows.items():
                if (
                    origin == table[0]
                    and key.casefold().rstrip(" ") == requested
                ):
                    self.next = (key, *values)
                    break
        elif statement.startswith("INSERT INTO ") and table:
            if self.fail_insert == table[0]:
                raise RuntimeError("synthetic-driver-private-information")
            columns = re.search(r"\(([^)]+)\) VALUES", statement)[1]
            names = [part.strip("[] ") for part in columns.split(",")]
            row = dict(zip(names, arguments, strict=True))
            self.rows[(table[0], arguments[0])] = (
                row["run_id"],
                row["original_values"],
            )
        elif statement.startswith("UPDATE ") and table:
            self.rows[(table[0], arguments[-2])] = (
                arguments[-1],
                arguments[-3],
            )
        elif statement.startswith("DELETE FROM ") and table:
            self.rows.pop((table[0], arguments[0]), None)

    def fetchone(self):
        """Return the prepared synthetic response to the previous statement."""
        return self.next


class Handle:
    """Track commit/rollback separately from the cursor's staged row state."""

    def __init__(self):
        self.reader = Cursor()
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self):
        """Return the synthetic cursor owned by this isolated test handle."""
        return self.reader

    def commit(self):
        """Observe successful completion without committing a real database."""
        self.commits += 1

    def rollback(self):
        """Observe transaction failure without contacting a real database."""
        self.rollbacks += 1

    def close(self):
        """Observe that production context cleanup always closes its handle."""
        self.closes += 1


@contextmanager
def injected_connection(handle):
    """Model rollback so staged fake rows expose partial failures."""
    before = copy.deepcopy(handle.reader.rows)
    try:
        yield handle
    except Exception:
        handle.reader.rows = before
        handle.rollback()
        raise


class BoundaryTests(unittest.TestCase):
    """Reject dangerous inputs before any driver or filesystem operation."""

    def test_shared_database_is_default_deny(self):
        values = dict(VALUES)
        values.pop("DEMO_SQLSERVER_ALLOW_SHARED_SOURCE_DATABASE")
        with self.assertRaisesRegex(DEMO.SqlServerError, "Shared source"):
            DEMO.validate_settings(values)

    def test_explicit_shared_flag_requires_exact_host_port_database(self):
        self.assertEqual(
            DEMO.validate_settings(VALUES)[
                "DEMO_SQLSERVER_ALLOW_SHARED_SOURCE_DATABASE"
            ],
            "true",
        )
        for key, value in (
            ("SOURCE_SQLSERVER_HOST", "another.example.invalid"),
            ("SOURCE_SQLSERVER_PORT", "1444"),
        ):
            with self.subTest(setting=key):
                with self.assertRaisesRegex(
                    DEMO.SqlServerError, "Shared source"
                ):
                    DEMO.validate_settings({**VALUES, key: value})

    def test_dbo_arbitrary_schema_and_unknown_table_are_rejected(self):
        for value in ("dbo", "arbitrary_schema", "lh_demo_erp;DELETE"):
            with self.subTest(schema=value):
                with self.assertRaises(DEMO.SqlServerError):
                    DEMO.validate_settings(
                        {**VALUES, "DEMO_SQLSERVER_ERP_SCHEMA": value}
                    )
        with self.assertRaises(DEMO.SqlServerError):
            DEMO.table_name("dbo.legacy_table")

    def test_write_and_mutate_require_strict_consent_before_connection(self):
        with mock.patch.object(DEMO, "connection") as driver:
            for value in (False, "true", 1):
                with self.subTest(consent=value):
                    with self.assertRaises(DEMO.SqlServerError):
                        DEMO.write_data({}, VALUES, run_id=RUN, consent=value)
                    with self.assertRaises(DEMO.SqlServerError):
                        DEMO.mutate(
                            VALUES, "insert", run_id=RUN, consent=value
                        )
        driver.assert_not_called()

    def test_mismatching_marker_fails_before_permission_or_business_queries(
        self,
    ):
        cursor = mock.Mock()
        cursor.fetchone.side_effect = [
            ("synthetic_demo",),
            (1,),
            (1, "a" * 32, "incorrect-hash"),
        ]
        with self.assertRaisesRegex(DEMO.SqlServerError, "ownership"):
            DEMO.check_writer(cursor, VALUES)
        self.assertFalse(
            any(
                "INSERT INTO" in call.args[0] or "UPDATE " in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )

    def test_missing_control_credentials_never_fall_back_to_reader(self):
        for key in (
            "SOURCE_SQLSERVER_CONTROL_USER",
            "SOURCE_SQLSERVER_CONTROL_PASSWORD",
        ):
            values = dict(VALUES)
            values.pop(key)
            with self.subTest(setting=key):
                with self.assertRaises(DEMO.SqlServerError):
                    DEMO.validate_settings(values, role="control")

    def test_control_principal_cannot_reuse_source_or_demo_identity(self):
        for reused in ("SOURCE_SQLSERVER_USER", "DEMO_SQLSERVER_USER"):
            with self.subTest(reused=reused):
                settings = {
                    **VALUES,
                    "SOURCE_SQLSERVER_CONTROL_USER": VALUES[reused].upper(),
                }
                driver = mock.Mock()
                with mock.patch.dict(
                    sys.modules, {"pymssql": SimpleNamespace(connect=driver)}
                ):
                    with self.assertRaisesRegex(
                        DEMO.SqlServerError, "must differ"
                    ):
                        with DEMO.connection(settings, role="control"):
                            self.fail("A reused control principal connected")
                driver.assert_not_called()

    def test_control_missing_comparison_identity_is_not_assumed_safe(self):
        for missing in ("SOURCE_SQLSERVER_USER", "DEMO_SQLSERVER_USER"):
            values = dict(VALUES)
            values.pop(missing)
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(DEMO.SqlServerError, "identities"):
                    DEMO.validate_settings(values, role="control")

    def test_missing_writer_is_reported_without_echoing_secrets(self):
        values = {**VALUES, "DEMO_SQLSERVER_USER": ""}
        result = DEMO.diagnose(values)
        self.assertFalse(result["connected"])
        rendered = json.dumps(result)
        for private in (
            "synthetic-writer-secret",
            "demo-db.example.invalid",
            "synthetic_reader",
            "b" * 64,
        ):
            self.assertNotIn(private, rendered)


class DiagnosticTests(unittest.TestCase):
    """Test safe diagnostics through injected metadata responses."""

    def test_success_shape_contains_no_endpoint_user_or_ownership_token(self):
        handle = Handle()
        cursor = mock.Mock()
        cursor.fetchone.side_effect = [
            value
            for _ in DEMO.TABLES
            for value in ((1,), (3, "2026-02-01T08:00:00.1234567"), (1,))
        ]
        cursor.fetchall.side_effect = [
            [(DEMO.CAPTURE_INSTANCES[name],), ("unrelated_private_capture",)]
            for name in DEMO.TABLES
        ]
        handle.cursor = lambda: cursor
        with (
            mock.patch.object(
                DEMO, "connection", return_value=injected_connection(handle)
            ),
            mock.patch.object(DEMO, "ownership", return_value=1),
            mock.patch.object(DEMO, "permissions_safe", return_value=True),
        ):
            result = DEMO.diagnose(VALUES)
        self.assertEqual(
            set(result),
            {
                "connected",
                "ownership",
                "schema_version",
                "permissions_safe",
                "tables",
            },
        )
        self.assertTrue(result["connected"])
        self.assertTrue(result["ownership"])
        for name, table in result["tables"].items():
            self.assertEqual(table["rows"], 3)
            self.assertEqual(
                table["last_updated_at"], "2026-02-01T08:00:00.1234567Z"
            )
            self.assertEqual(
                table["capture_instance"], DEMO.CAPTURE_INSTANCES[name]
            )
        rendered = json.dumps(result)
        for hidden in (
            "demo-db.example.invalid",
            "synthetic_writer",
            "synthetic_demo",
            "synthetic-writer-secret",
            "unrelated_private_capture",
            "b" * 64,
        ):
            self.assertNotIn(hidden, rendered)

    def test_connection_failure_does_not_invent_connection_or_row_success(
        self,
    ):
        with mock.patch.object(
            DEMO,
            "connection",
            side_effect=RuntimeError("synthetic-secret-vendor-error"),
        ):
            result = DEMO.diagnose(VALUES)
        self.assertFalse(result["connected"])
        self.assertFalse(result["ownership"])
        self.assertTrue(
            all(table["rows"] is None for table in result["tables"].values())
        )
        self.assertNotIn("synthetic-secret", json.dumps(result))


class TransportTests(unittest.TestCase):
    """Inspect real context behavior using an injected pymssql module."""

    def test_source_is_readonly_and_control_uses_distinct_credentials(self):
        for role, user, readonly in (
            ("source", "synthetic_reader", True),
            ("control", "synthetic_control", False),
            ("demo", "synthetic_writer", False),
        ):
            with self.subTest(role=role):
                handle = Handle()
                driver = mock.Mock(return_value=handle)
                with mock.patch.dict(
                    sys.modules, {"pymssql": SimpleNamespace(connect=driver)}
                ):
                    with DEMO.connection(VALUES, role=role):
                        config = Path(os.environ["FREETDSCONF"])
                        contents = config.read_text()
                        self.assertIn("encryption = require", contents)
                        self.assertIn(
                            "check certificate hostname = yes", contents
                        )
                        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                self.assertEqual(
                    driver.call_args.kwargs["read_only"], readonly
                )
                self.assertEqual(driver.call_args.kwargs["user"], user)
                self.assertEqual(
                    driver.call_args.kwargs["encryption"], "require"
                )
                self.assertEqual(handle.closes, 1)
                self.assertFalse(config.exists())

    def test_explicit_poc_trust_preserves_encryption_and_restores_environment(
        self,
    ):
        handle = Handle()
        with (
            mock.patch.dict(
                sys.modules,
                {
                    "pymssql": SimpleNamespace(
                        connect=mock.Mock(return_value=handle)
                    )
                },
            ),
            mock.patch.dict(os.environ, {"FREETDS": "/example/old.conf"}),
        ):
            previous = dict(os.environ)
            with DEMO.connection(
                {**VALUES, "DEMO_SQLSERVER_TRUST_SERVER_CERTIFICATE": "true"}
            ):
                config = Path(os.environ["FREETDSCONF"]).read_text()
                self.assertIn("encryption = require", config)
                self.assertNotIn("check certificate hostname = yes", config)
            self.assertEqual(dict(os.environ), previous)
        with self.assertRaisesRegex(DEMO.SqlServerError, "require encryption"):
            DEMO.validate_settings(
                {**VALUES, "DEMO_SQLSERVER_ENCRYPT": "false"}
            )

    def test_driver_failure_rolls_back_and_returns_no_vendor_text(self):
        handle = Handle()
        with mock.patch.dict(
            sys.modules,
            {
                "pymssql": SimpleNamespace(
                    connect=mock.Mock(return_value=handle)
                )
            },
        ):
            with self.assertRaises(DEMO.SqlServerError) as error:
                with DEMO.connection(VALUES):
                    raise RuntimeError("synthetic-secret-from-driver")
        self.assertEqual(handle.rollbacks, 1)
        self.assertEqual(handle.closes, 1)
        self.assertNotIn("synthetic-secret", str(error.exception))


class TypedSourceTests(unittest.TestCase):
    """Preserve original values independently of SQL-compatible projections."""

    def test_exact_decimal_null_empty_and_japanese_survive_originals(self):
        data = GENERATOR.build_data(42, 1, 7, "clean")
        data["erp_products"][0].update(
            product_name="日本語の合成部品",
            product_family=None,
            unit="",
            standard_cycle_seconds="25.123",
        )
        prepared, _, _ = DEMO.prepare_data(data)
        _, typed, payload = prepared["erp_products"][0]
        self.assertEqual(typed["standard_cycle_seconds"], Decimal("25.123"))
        self.assertIsNone(typed["product_family"])
        self.assertEqual(typed["unit"], "")
        self.assertEqual(
            json.loads(payload)["product_name"], "日本語の合成部品"
        )
        self.assertIsNone(json.loads(payload)["product_family"])
        self.assertEqual(json.loads(payload)["unit"], "")

    def test_invalid_typed_values_become_null_but_raw_is_unchanged(self):
        data = GENERATOR.build_data(42, 1, 7, "clean")
        data["erp_products"][0]["standard_cycle_seconds"] = "invalid-number"
        data["erp_work_orders"][0]["planned_date"] = "2026-99-99"
        prepared, _, normalized = DEMO.prepare_data(data)
        _, typed, payload = prepared["erp_products"][0]
        self.assertIsNone(typed["standard_cycle_seconds"])
        self.assertEqual(
            json.loads(payload)["standard_cycle_seconds"], "invalid-number"
        )
        self.assertGreaterEqual(normalized, 2)

    def test_datetime2_fraction_is_not_rounded_to_python_microseconds(self):
        self.assertEqual(
            DEMO.timestamp("2026-02-01T12:34:56.1234567+09:00"),
            "2026-02-01T03:34:56.1234567",
        )
        self.assertEqual(
            DEMO.timestamp("2026-02-01T00:00:00Z"),
            "2026-02-01T00:00:00.0000000",
        )

    def test_latest_revision_and_exact_duplicate_collapse_deterministically(
        self,
    ):
        data = GENERATOR.build_data(42, 1, 7, "edge")
        prepared, duplicates, _ = DEMO.prepare_data(data)
        reversed_data = {
            key: list(reversed(rows)) for key, rows in data.items()
        }
        reversed_prepared, reversed_duplicates, _ = DEMO.prepare_data(
            reversed_data
        )
        self.assertEqual(prepared, reversed_prepared)
        self.assertEqual(duplicates, reversed_duplicates)
        self.assertEqual(duplicates, 2)
        first = [
            row for row in prepared["mes_events"] if row[0] == "EV00001-1"
        ][0]
        self.assertEqual(
            json.loads(first[2])["updated_at"], "2026-01-14T10:00:00Z"
        )

    def test_missing_key_and_extra_contract_cannot_be_written(self):
        data = GENERATOR.build_data(42, 1, 7, "clean")
        data["erp_products"][0]["product_id"] = ""
        with self.assertRaisesRegex(DEMO.SqlServerError, "primary key"):
            DEMO.prepare_data(data)
        with self.assertRaises(DEMO.SqlServerError):
            DEMO.prepare_data({**data, "arbitrary": []})


class WriteTests(unittest.TestCase):
    """Test replay, ownership collisions and transaction failures."""

    def setUp(self):
        self.handle = Handle()
        self.guard = mock.patch.object(DEMO, "check_writer")
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.patch = mock.patch.object(
            DEMO,
            "connection",
            side_effect=lambda *args, **kwargs: injected_connection(
                self.handle
            ),
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_replay_skips_without_changing_source_watermark(self):
        data = GENERATOR.build_data(42, 1, 7, "clean")
        first = DEMO.write_data(data, VALUES, run_id=RUN, consent=True)
        count = len(self.handle.reader.calls)
        replay = DEMO.write_data(data, VALUES, run_id=RUN, consent=True)
        self.assertGreater(first["inserted"], 0)
        self.assertEqual(replay["inserted"], 0)
        self.assertEqual(replay["updated"], 0)
        self.assertEqual(replay["skipped"], first["inserted"])
        self.assertFalse(
            any(
                statement.startswith("UPDATE [lh_demo_erp]")
                for statement, _ in self.handle.reader.calls[count:]
            )
        )
        self.assertEqual(self.handle.commits, 2)

    def test_case_or_space_equivalent_source_key_does_not_update_any_row(self):
        for physical in ("p001", "P001 "):
            with self.subTest(physical=physical):
                self.handle = Handle()
                self.handle.reader.rows[
                    ("[lh_demo_erp].[products]", physical)
                ] = (RUN, "existing-original")
                before = copy.deepcopy(self.handle.reader.rows)
                with self.assertRaisesRegex(DEMO.SqlServerError, "collation"):
                    DEMO.write_data(
                        GENERATOR.build_data(42, 1, 7, "clean"),
                        VALUES,
                        run_id=RUN,
                        consent=True,
                    )
                self.assertEqual(self.handle.reader.rows, before)
                self.assertEqual(self.handle.commits, 0)
                self.assertEqual(self.handle.rollbacks, 1)
                self.assertFalse(
                    any(
                        statement.startswith(("INSERT ", "UPDATE ", "DELETE "))
                        for statement, _ in self.handle.reader.calls
                    )
                )

    def test_case_or_space_equivalent_cdc_key_rejects_each_mutation(self):
        key = "CDC-" + RUN
        for physical in (key.lower(), key + " "):
            for action in ("insert", "update", "delete"):
                with self.subTest(physical=physical, action=action):
                    self.handle = Handle()
                    self.handle.reader.rows[
                        ("[lh_demo_erp].[products]", physical)
                    ] = (RUN, "existing-original")
                    before = copy.deepcopy(self.handle.reader.rows)
                    with self.assertRaisesRegex(
                        DEMO.SqlServerError, "collation"
                    ):
                        DEMO.mutate(VALUES, action, run_id=RUN, consent=True)
                    self.assertEqual(self.handle.reader.rows, before)
                    self.assertEqual(self.handle.commits, 0)
                    self.assertEqual(self.handle.rollbacks, 1)
                    self.assertFalse(
                        any(
                            statement.startswith(
                                ("INSERT ", "UPDATE ", "DELETE ")
                            )
                            for statement, _ in self.handle.reader.calls
                        )
                    )

    def test_source_updated_at_is_server_owned_not_generator_history(self):
        data = GENERATOR.build_data(42, 1, 7, "clean")
        DEMO.write_data(data, VALUES, run_id=RUN, consent=True)
        inserts = [
            (statement, values)
            for statement, values in self.handle.reader.calls
            if statement.startswith("INSERT INTO [lh_demo_")
        ]
        self.assertTrue(inserts)
        for statement, values in inserts:
            columns = re.search(r"\(([^)]+)\) VALUES", statement)[1]
            self.assertNotIn("[updated_at]", columns)
            self.assertIn("[original_values]", columns)
            self.assertIn("updated_at", json.loads(values[-1]))

    def test_partial_failure_does_not_commit_any_synthetic_rows(self):
        self.handle.reader.fail_insert = "[lh_demo_mes].[events]"
        with self.assertRaises(RuntimeError):
            DEMO.write_data(
                GENERATOR.build_data(42, 1, 7, "clean"),
                VALUES,
                run_id=RUN,
                consent=True,
            )
        self.assertEqual(self.handle.commits, 0)
        self.assertEqual(self.handle.rollbacks, 1)
        self.assertEqual(self.handle.reader.rows, {})

    def test_other_run_key_is_preserved(self):
        self.handle.reader.rows[("[lh_demo_erp].[products]", "P001")] = (
            "fedcba987654",
            "existing-original",
        )
        before = copy.deepcopy(self.handle.reader.rows)
        with self.assertRaisesRegex(DEMO.SqlServerError, "another run"):
            DEMO.write_data(
                GENERATOR.build_data(42, 1, 7, "clean"),
                VALUES,
                run_id=RUN,
                consent=True,
            )
        self.assertEqual(self.handle.reader.rows, before)
        self.assertEqual(self.handle.commits, 0)

    def test_fixed_cdc_insert_update_delete_and_no_arbitrary_target(self):
        for action in ("insert", "update", "delete"):
            with self.subTest(action=action):
                result = DEMO.mutate(VALUES, action, run_id=RUN, consent=True)
                self.assertEqual(
                    result,
                    {
                        "action": action,
                        "changed_rows": 1,
                        "run_id": RUN,
                        "source_only": True,
                    },
                )
        self.assertEqual(self.handle.reader.rows, {})
        mutations = [
            statement
            for statement, _ in self.handle.reader.calls
            if statement.startswith(("INSERT INTO", "UPDATE ", "DELETE FROM"))
        ]
        self.assertTrue(
            all(
                "[lh_demo_erp].[products]" in statement
                for statement in mutations
            )
        )
        with self.assertRaises(DEMO.SqlServerError):
            DEMO.mutate(VALUES, "DROP DATABASE", run_id=RUN, consent=True)


class SideEffectTests(unittest.TestCase):
    """Mock protocol checks; live SQL Server acceptance is still required."""

    def cursor(self, *, definition=None, identity=1, safe=1, missing=False):
        cursor = mock.Mock()
        definition = (
            DEMO.SAFETY_DEFINITION if definition is None else definition
        )
        cursor.fetchone.side_effect = [
            None if missing else (definition, identity),
            (safe,),
            None,
        ]
        return cursor

    def test_exact_dbo_checker_is_required(self):
        cursor = self.cursor()
        self.assertTrue(DEMO.side_effects_safe(cursor))
        self.assertEqual(
            cursor.execute.call_args_list[-1].args,
            ("EXEC [lh_demo_ctl].[check_write_safety_v1]", ()),
        )

    def test_missing_modified_hidden_or_non_dbo_checker_is_refused(self):
        for options in (
            {"missing": True},
            {"definition": ""},
            {"definition": DEMO.SAFETY_DEFINITION + "--tampered"},
            {"identity": 0},
            {"identity": None},
        ):
            with self.subTest(options=options):
                cursor = self.cursor(**options)
                self.assertFalse(DEMO.side_effects_safe(cursor))
                self.assertEqual(cursor.execute.call_count, 1)

    def test_hidden_legacy_cascade_or_active_trigger_is_refused(self):
        # The caller need not see the legacy child. The dbo checker reports
        # unsafe with complete metadata, regardless of caller visibility.
        for role in ("demo", "control"):
            with self.subTest(role=role):
                self.assertFalse(
                    DEMO.side_effects_safe(self.cursor(safe=0), role)
                )
                self.assertTrue(DEMO.side_effects_safe(self.cursor(), role))

    def test_unknown_extra_rows_and_checker_failure_never_pass(self):
        self.assertFalse(DEMO.side_effects_safe(self.cursor(safe=None)))
        cursor = self.cursor()
        cursor.fetchone.side_effect = [(DEMO.SAFETY_DEFINITION, 1), (1,), (1,)]
        self.assertFalse(DEMO.side_effects_safe(cursor))
        cursor = self.cursor()
        cursor.execute.side_effect = [None, RuntimeError("checker failed")]
        with self.assertRaises(RuntimeError):
            DEMO.side_effects_safe(cursor)

    def test_source_reader_does_not_execute_checker(self):
        cursor = mock.Mock()
        self.assertTrue(DEMO.side_effects_safe(cursor, role="source"))
        cursor.execute.assert_not_called()


class PermissionTests(unittest.TestCase):
    """Use fixed cursor responses to check fail-closed permission decisions."""

    def cursor(
        self,
        *,
        broad=False,
        dangerous=False,
        unknown=False,
        denied_object=None,
        column_permission=None,
        role="demo",
        object_type="U",
        unknown_object=False,
        unknown_permission=False,
    ):
        cursor = mock.Mock()
        result = [None]
        statements = []

        def execute(statement, arguments=()):
            statements.append((statement, arguments))
            if statement.startswith("SELECT IS_MEMBER"):
                count = statement.count("IS_MEMBER(") + statement.count(
                    "IS_SRVROLEMEMBER("
                )
                result[0] = tuple(
                    [None if unknown else int(broad)] + [0] * (count - 1)
                )
            elif statement.startswith("SELECT permission_name FROM"):
                result[0] = [("CONTROL",)] if dangerous else []
            elif "FROM sys.sql_modules m" in statement:
                result[0] = (DEMO.SAFETY_DEFINITION, 1)
            elif statement.startswith("EXEC "):
                result[0] = (1,)
            elif "FROM sys.columns c" in statement:
                # Effective column-only role grant; the object probe below
                # still reports no object-level permission.
                result[0] = None
                if column_permission:
                    operation, name, column, effective = column_permission
                    self.assertIn("c.name, N'COLUMN'", statement)
                    self.assertIn(" IS NULL OR ", statement)
                    if operation == arguments[0] and (
                        effective is None
                        or (effective == 1 and name not in arguments[2:])
                    ):
                        result[0] = (999, 1)
            elif "o.type NOT IN" in statement:
                result[0] = (999,) if unknown_object else None
            elif "sys.objects o" in statement:
                operation = arguments[0]
                name = denied_object[1] if denied_object else None
                audited_kind = "'" + object_type + "'" in statement
                unsafe = (
                    audited_kind
                    and denied_object
                    and operation == denied_object[0]
                    and name not in arguments[1:]
                )
                result[0] = (999,) if unsafe or unknown_permission else None
            elif "SELECT COUNT_BIG(*) FROM sys.tables" in statement:
                result[0] = (len(arguments),)
            elif (
                "sys.schemas s WHERE" in statement
                or "FROM sys.database_principals" in statement
                or "FROM sys.triggers" in statement
                or "FROM sys.foreign_keys" in statement
            ):
                result[0] = None
            else:
                result[0] = (1,)

        cursor.execute.side_effect = execute

        def fetchone():
            value = result[0]
            if statements and statements[-1][0].startswith("EXEC "):
                result[0] = None
            return value

        cursor.fetchone.side_effect = fetchone
        cursor.fetchall.side_effect = lambda: result[0]
        cursor.statements = statements
        return cursor

    def test_clr_synonym_external_sequence_and_queue_grants_are_refused(self):
        cases = (
            ("PC", "EXECUTE", "source"),
            ("FN", "EXECUTE", "source"),
            ("FS", "EXECUTE", "source"),
            ("AF", "EXECUTE", "source"),
            ("IF", "SELECT", "demo"),
            ("TF", "SELECT", "demo"),
            ("FT", "SELECT", "demo"),
            ("SN", "INSERT", "demo"),
            ("SN", "UPDATE", "source"),
            ("SN", "DELETE", "control"),
            ("SN", "EXECUTE", "source"),
            ("ET", "SELECT", "demo"),
            ("SO", "UPDATE", "source"),
            ("SQ", "RECEIVE", "source"),
        )
        for kind, operation, role in cases:
            with self.subTest(kind=kind, operation=operation, role=role):
                cursor = self.cursor(
                    object_type=kind,
                    denied_object=(operation, "legacy.unrelated"),
                    role=role,
                )
                self.assertFalse(DEMO.permissions_safe(cursor, role=role))

    def test_unknown_object_or_missing_permission_metadata_is_refused(self):
        for options in (
            {"unknown_object": True},
            {"unknown_permission": True},
        ):
            with self.subTest(options=options):
                self.assertFalse(DEMO.permissions_safe(self.cursor(**options)))

    def test_query_contract_uses_applicable_permissions_not_guessed_pairs(
        self,
    ):
        """Inspect SQL contracts; this is not a SQL Server execution test."""
        cursor = self.cursor()
        self.assertTrue(DEMO.permissions_safe(cursor))
        object_queries = [
            statement
            for statement, _ in cursor.statements
            if "sys.objects o" in statement
        ]
        self.assertTrue(object_queries)
        unknown = [
            statement
            for statement in object_queries
            if "o.type NOT IN" in statement
        ]
        self.assertEqual(len(unknown), 1)
        applicable = [
            statement
            for statement in object_queries
            if "OUTER APPLY sys.fn_my_permissions" in statement
        ]
        self.assertEqual(len(applicable), 9)
        for statement in applicable:
            self.assertIn("COALESCE(o.parent_object_id,0)>0", statement)
            self.assertIn("p.permission_name IS NULL", statement)
            self.assertNotIn("HAS_PERMS_BY_NAME", statement)
            for kind in ("PC", "FT", "SN", "ET", "SO", "SQ"):
                self.assertIn("'" + kind + "'", statement)
        self.assertTrue(
            {"C", "D", "F", "PK", "UQ", "EC"}
            <= set(DEMO.NON_GRANTABLE_CHILD_TYPES)
        )
        self.assertFalse(
            {"C", "F", "PK", "UQ", "EC"} & set(DEMO.PERMISSION_OBJECT_TYPES)
        )

    def test_broad_role_unknown_membership_and_dangerous_grant_are_denied(
        self,
    ):
        for options in (
            {"broad": True},
            {"unknown": True},
            {"dangerous": True},
        ):
            with self.subTest(options=options):
                self.assertFalse(DEMO.permissions_safe(self.cursor(**options)))

    def test_installation_marker_update_and_external_access_are_denied(self):
        for operation, target in (
            ("UPDATE", "lh_demo_ctl.installation"),
            ("SELECT", "legacy.unrelated"),
            ("ALTER", "lh_demo_erp.products"),
        ):
            with self.subTest(operation=operation):
                self.assertFalse(
                    DEMO.permissions_safe(
                        self.cursor(denied_object=(operation, target))
                    )
                )

    def test_execute_exception_is_only_the_fixed_checker_for_writers(self):
        for role, target, expected in (
            ("demo", DEMO.SAFETY_CHECKER, True),
            ("control", DEMO.SAFETY_CHECKER, True),
            ("source", DEMO.SAFETY_CHECKER, False),
            ("demo", "legacy.any_procedure", False),
        ):
            with self.subTest(role=role, target=target):
                self.assertEqual(
                    DEMO.permissions_safe(
                        self.cursor(denied_object=("EXECUTE", target)),
                        role=role,
                    ),
                    expected,
                )

    def test_column_grants_cannot_tamper_with_ownership(self):
        for column in ("ownership_hash", "installation_id", "schema_version"):
            with self.subTest(column=column):
                self.assertFalse(
                    DEMO.permissions_safe(
                        self.cursor(
                            column_permission=(
                                "UPDATE",
                                "lh_demo_ctl.installation",
                                column,
                                1,
                            )
                        )
                    )
                )

    def test_legacy_column_only_grants_are_denied(self):
        for operation in ("SELECT", "UPDATE"):
            with self.subTest(operation=operation):
                self.assertFalse(
                    DEMO.permissions_safe(
                        self.cursor(
                            column_permission=(
                                operation,
                                "legacy.orders",
                                "amount",
                                1,
                            )
                        )
                    )
                )

    def test_fixed_column_permissions_follow_roles(self):
        for role, operation, target, expected in (
            ("demo", "UPDATE", "lh_demo_erp.products", True),
            ("control", "UPDATE", "lh_demo_ctl.batch_watermarks", True),
            ("source", "SELECT", "legacy.orders", True),
            ("source", "UPDATE", "lh_demo_erp.products", False),
            ("control", "UPDATE", "lh_demo_erp.products", False),
        ):
            with self.subTest(role=role, operation=operation):
                self.assertEqual(
                    DEMO.permissions_safe(
                        self.cursor(
                            column_permission=(operation, target, "value", 1)
                        ),
                        role=role,
                    ),
                    expected,
                )

    def test_unknown_column_permission_fails_closed(self):
        self.assertFalse(
            DEMO.permissions_safe(
                self.cursor(
                    column_permission=(
                        "UPDATE",
                        "lh_demo_erp.products",
                        "name",
                        None,
                    )
                )
            )
        )

    def test_source_dml_and_control_business_dml_are_denied(self):
        for role in ("source", "control"):
            with self.subTest(role=role):
                self.assertFalse(
                    DEMO.permissions_safe(
                        self.cursor(
                            denied_object=("UPDATE", "lh_demo_erp.products"),
                            role=role,
                        ),
                        role=role,
                    )
                )
        self.assertTrue(DEMO.permissions_safe(self.cursor()))
        self.assertTrue(DEMO.permissions_safe(self.cursor(), role="source"))
        self.assertTrue(DEMO.permissions_safe(self.cursor(), role="control"))


if __name__ == "__main__":
    unittest.main()
