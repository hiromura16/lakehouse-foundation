"""Unit tests for fixed synthetic SQL safety; no SQL Server is contacted.

Pure conversion tests and injected cursor/driver tests are deliberately
separate from the DDL's static contract tests. A mock result is not evidence
that SQL Server accepted a statement or that CDC delivered a real event.
"""

import contextlib
import importlib.util
import json
import os
import sys
import unittest
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
    "DEMO_SQLSERVER_INSTALLATION_ID": "a" * 32,
    "DEMO_SQLSERVER_OWNERSHIP_TOKEN": "b" * 64,
}


class Handle:
    """Track commit/rollback separately from the cursor's staged row state."""

    def __init__(self):
        self.rollbacks = 0
        self.closes = 0

    def rollback(self):
        """Observe transaction failure without contacting a real database."""
        self.rollbacks += 1

    def close(self):
        """Observe that production context cleanup always closes its handle."""
        self.closes += 1


class BoundaryTests(unittest.TestCase):
    """旧writerの接続役割を拒否し、source/controlを分離する。"""

    def test_old_demo_role_and_write_entrypoints_are_no_longer_exposed(self):
        for name in ("write_data", "mutate", "writer_permissions_safe"):
            self.assertFalse(hasattr(DEMO, name))
        with self.assertRaisesRegex(DEMO.SqlServerError, "role"):
            DEMO.validate_settings(VALUES, role="demo")
        self.assertFalse(DEMO.permissions_safe(mock.Mock(), role="demo"))

    def test_dbo_arbitrary_schema_and_unknown_table_are_rejected(self):
        for value in ("dbo", "arbitrary_schema", "lh_demo_erp;DELETE"):
            with self.subTest(schema=value):
                with self.assertRaises(DEMO.SqlServerError):
                    DEMO.validate_settings(
                        {**VALUES, "DEMO_SQLSERVER_ERP_SCHEMA": value}
                    )
        with self.assertRaises(DEMO.SqlServerError):
            DEMO.table_name("dbo.legacy_table")

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
            DEMO.ownership(cursor, VALUES)
        self.assertFalse(
            any(
                "INSERT INTO" in call.args[0] or "UPDATE " in call.args[0]
                for call in cursor.execute.call_args_list
            )
        )

    def test_connected_database_case_alias_is_not_adopted(self):
        """Match the DBA SQL's literal database identity on every server."""
        cursor = mock.Mock()
        cursor.fetchone.return_value = (
            VALUES["SOURCE_SQLSERVER_DATABASE"].upper(),
        )
        with self.assertRaisesRegex(DEMO.SqlServerError, "database"):
            DEMO.ownership(cursor, VALUES)
        self.assertEqual(cursor.execute.call_count, 1)

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

    def test_control_principal_cannot_reuse_source_identity(self):
        for reused in ("SOURCE_SQLSERVER_USER",):
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
                        DEMO.SqlServerError, "distinct"
                    ):
                        with DEMO.connection(settings, role="control"):
                            self.fail("A reused control principal connected")
                driver.assert_not_called()

    def test_control_missing_comparison_identity_is_not_assumed_safe(self):
        for missing in ("SOURCE_SQLSERVER_USER",):
            values = dict(VALUES)
            values.pop(missing)
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(DEMO.SqlServerError, "identities"):
                    DEMO.validate_settings(values, role="control")

    def test_missing_reader_is_reported_without_echoing_secrets(self):
        values = {**VALUES, "SOURCE_SQLSERVER_USER": ""}
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
        handle = mock.Mock()
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
                DEMO, "connection", return_value=contextlib.nullcontext(handle)
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
                {**VALUES, "SOURCE_SQLSERVER_TRUST_SERVER_CERTIFICATE": "true"}
            ):
                config = Path(os.environ["FREETDSCONF"]).read_text()
                self.assertIn("encryption = require", config)
                self.assertNotIn("check certificate hostname = yes", config)
            self.assertEqual(dict(os.environ), previous)
        with self.assertRaisesRegex(DEMO.SqlServerError, "require encryption"):
            DEMO.validate_settings(
                {**VALUES, "SOURCE_SQLSERVER_ENCRYPT": "false"}
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
        for definition in (
            DEMO.SAFETY_DEFINITION,
            DEMO.CONTROL_SAFETY_DEFINITION,
        ):
            cursor = self.cursor(definition=definition)
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
        for role in ("control",):
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
        role="control",
        object_type="U",
        unknown_object=False,
        unknown_permission=False,
        denied_object_id=999,
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
                        or (
                            effective == 1
                            and name not in arguments[2:]
                            and denied_object_id not in arguments[2:]
                        )
                    ):
                        result[0] = (999, 1)
            elif "LEFT JOIN sys.tables t" in statement:
                result[0] = []
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
                    and denied_object_id not in arguments[1:]
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
            ("IF", "SELECT", "control"),
            ("TF", "SELECT", "control"),
            ("FT", "SELECT", "control"),
            ("SN", "INSERT", "control"),
            ("SN", "UPDATE", "source"),
            ("SN", "DELETE", "control"),
            ("SN", "EXECUTE", "source"),
            ("ET", "SELECT", "control"),
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
                self.assertFalse(
                    DEMO.permissions_safe(cursor, role=role, full_audit=True)
                )

    def test_unknown_object_or_missing_permission_metadata_is_refused(self):
        for options in (
            {"unknown_object": True},
            {"unknown_permission": True},
        ):
            with self.subTest(options=options):
                self.assertFalse(
                    DEMO.permissions_safe(
                        self.cursor(**options), role="control"
                    )
                )

    def test_query_contract_uses_applicable_permissions_not_guessed_pairs(
        self,
    ):
        """Inspect SQL contracts; this is not a SQL Server execution test."""
        cursor = self.cursor()
        self.assertTrue(DEMO.permissions_safe(cursor, role="control"))
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
                self.assertFalse(
                    DEMO.permissions_safe(
                        self.cursor(**options), role="control"
                    )
                )

    def test_installation_marker_update_and_external_access_are_denied(self):
        for operation, target in (
            ("UPDATE", "lh_demo_ctl.installation"),
            ("SELECT", "legacy.unrelated"),
            ("ALTER", "lh_demo_erp.products"),
        ):
            with self.subTest(operation=operation):
                self.assertFalse(
                    DEMO.permissions_safe(
                        self.cursor(denied_object=(operation, target)),
                        role="control",
                    )
                )

    def test_execute_exception_is_only_the_fixed_checker_for_writers(self):
        for role, target, expected in (
            ("control", DEMO.SAFETY_CHECKER, True),
            ("source", DEMO.SAFETY_CHECKER, False),
            ("control", "legacy.any_procedure", False),
        ):
            with self.subTest(role=role, target=target):
                self.assertEqual(
                    DEMO.permissions_safe(
                        self.cursor(denied_object=("EXECUTE", target)),
                        role=role,
                        full_audit=True,
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
                        ),
                        role="control",
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
                        ),
                        role="control",
                    )
                )

    def test_fixed_column_permissions_follow_roles(self):
        for role, operation, target, expected in (
            ("control", "INSERT", "lh_demo_ctl.batch_watermarks", True),
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
                        full_audit=True,
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
                ),
                role="control",
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
                        full_audit=True,
                    )
                )
        self.assertTrue(DEMO.permissions_safe(self.cursor(), role="control"))
        self.assertTrue(
            DEMO.permissions_safe(
                self.cursor(), role="source", full_audit=True
            )
        )
        self.assertTrue(DEMO.permissions_safe(self.cursor(), role="control"))


if __name__ == "__main__":
    unittest.main()
