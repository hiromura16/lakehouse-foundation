"""Static SQL contracts only: these tests never execute T-SQL on a server.

They keep public DDL aligned with the source adapter and prevent accidental
broad grants or legacy-object targeting. SQL Server compilation, effective
permissions, typed round trips and CDC capture remain separate live tests.
"""

import importlib.util
import re
import unittest
from pathlib import Path

from sqlfluff.core import Linter

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "infra/sqlserver"
SPEC = importlib.util.spec_from_file_location(
    "fixed_sql_contract", ROOT / "scripts/sqlserver_demo.py"
)
DEMO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEMO)
FILES = (
    "001_demo_schemas.sql",
    "002_demo_tables.sql",
    "003_demo_permissions.sql",
    "004_demo_cdc_enable.sql",
    "005_demo_cdc_disable.sql",
    "006_demo_validation.sql",
)
CDC_FILES = ("004_demo_cdc_enable.sql", "005_demo_cdc_disable.sql")


def source(filename):
    """Read public SQL and exclude line comments from executable contracts."""
    return "\n".join(
        line
        for line in (DIRECTORY / filename).read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


def table_blocks():
    """Extract bounded CREATE TABLE blocks from the fixed public DDL."""
    return {
        schema + "." + table: body
        for schema, table, body in re.findall(
            r"CREATE TABLE \[([a-z_]+)\]\.\[([a-z_]+)\] \(\n(.*?)\n\);",
            source("002_demo_tables.sql"),
            re.DOTALL,
        )
    }


class FixedSqlContractTests(unittest.TestCase):
    """Check safe scopes and stable six-table typed definitions."""

    def test_fixed_ddl_parses_with_pinned_tsql_grammar(self):
        """Parse all fixed DDL without executing SQL or skipping guards."""
        linter = Linter(dialect="tsql")
        for filename in FILES:
            with self.subTest(filename=filename):
                parsed = linter.parse_string(
                    (DIRECTORY / filename).read_text(), fname=filename
                )
                self.assertEqual(parsed.violations, [])

    def test_each_script_guards_connected_database_and_ownership(self):
        for filename in FILES:
            with self.subTest(filename=filename):
                sql = source(filename)
                self.assertIn("DB_NAME()", sql)
                self.assertIn("$(TargetDatabase)", sql)
                self.assertIn("$(InstallationId)", sql)
                self.assertIn("$(OwnershipHash)", sql)
                self.assertIn("@ActualVersion <> 1", sql)
                self.assertIn("THROW 51003", sql)
                self.assertIn("Unmarked existing tables", sql)
                self.assertIn("tr.is_disabled=0", sql)
                self.assertIn("FROM sys.foreign_keys", sql)
                self.assertIn("THROW 51016", sql)
                self.assertIn("THROW 51017", sql)
                self.assertIn("SET XACT_ABORT ON", sql)
                self.assertIn("BEGIN TRANSACTION", sql)
                self.assertIn("COMMIT TRANSACTION", sql)
                self.assertNotRegex(sql, r"(?i)\bDROP\b|\bCREATE\s+DATABASE\b")
                self.assertNotRegex(
                    sql, r"(?i)\[(?:pocctl|legacy)\]|\[dbo\]\.\["
                )

    def test_unknown_objects_are_checked_without_a_partial_type_allowlist(
        self,
    ):
        """Include CLR functions, synonyms and sequences in DBA preflight."""
        for filename in FILES:
            with self.subTest(filename=filename):
                sql = source(filename)
                beginning = sql.index("FROM sys.objects o JOIN sys.schemas s")
                end = sql.index("THROW 51005", beginning)
                guard = sql[beginning:end]
                self.assertIn("o.is_ms_shipped=0", guard)
                self.assertIn("o.parent_object_id=0", guard)
                self.assertNotIn("o.type IN", guard)

    def test_null_marker_version_and_unowned_existing_role_are_refused(self):
        """Keep standalone SQL guards strict despite SQL NULL comparisons."""
        for filename in FILES:
            with self.subTest(filename=filename):
                self.assertIn("@ActualVersion IS NULL", source(filename))
        permissions = source("003_demo_permissions.sql")
        self.assertIn("r.owning_principal_id IS NULL", permissions)
        self.assertIn(
            "r.owning_principal_id<>DATABASE_PRINCIPAL_ID(N'dbo')",
            permissions,
        )
        self.assertLess(
            permissions.index("THROW 51012"), permissions.index("GRANT SELECT")
        )
        self.assertNotIn("ALTER AUTHORIZATION", permissions)

    def test_exact_tables_primary_keys_watermarks_and_originals(self):
        blocks = table_blocks()
        expected = {
            item["schema"] + "." + item["table"]
            for item in DEMO.TABLES.values()
        } | {
            "lh_demo_ctl.installation",
            "lh_demo_ctl.demo_runs",
            "lh_demo_ctl.batch_watermarks",
        }
        self.assertEqual(set(blocks), expected)
        for item in DEMO.TABLES.values():
            with self.subTest(table=item["table"]):
                body = blocks[item["schema"] + "." + item["table"]]
                self.assertIn("PRIMARY KEY ([" + item["pk"] + "])", body)
                self.assertIn(
                    "[updated_at] datetime2(7) NOT NULL "
                    "DEFAULT SYSUTCDATETIME()",
                    body,
                )
                self.assertIn(
                    "[created_at] datetime2(7) NOT NULL "
                    "DEFAULT SYSUTCDATETIME()",
                    body,
                )
                self.assertIn("[run_id] char(12) NOT NULL", body)
                self.assertIn("[original_values] nvarchar(max) NOT NULL", body)
                self.assertIn("ISJSON(original_values)=1", body)
                names = re.findall(r"^    \[([a-z_]+)\] ", body, re.MULTILINE)
                self.assertEqual(
                    set(names),
                    set(item["columns"])
                    | {"created_at", "run_id", "original_values"},
                )

    def test_decimal_unicode_null_and_existing_schema_verification(self):
        sql = source("002_demo_tables.sql")
        self.assertIn("[standard_cycle_seconds] decimal(18,3) NULL", sql)
        self.assertIn("[unit_price] decimal(18,2) NULL", sql)
        self.assertIn("[product_name] nvarchar(256) NULL", sql)
        self.assertIn("FROM @Expected EXCEPT SELECT * FROM @Actual", sql)
        self.assertIn("FROM @Actual EXCEPT SELECT * FROM @Expected", sql)
        for attribute in (
            "max_length",
            "precision_value",
            "scale_value",
            "nullable_value",
            "primary_key_value",
        ):
            self.assertIn(attribute + " ", sql)
        self.assertLess(
            sql.index("Existing fixed-table contract mismatch"),
            sql.index("INSERT INTO [lh_demo_ctl].[installation]"),
        )

    def test_checkpoint_and_run_control_contracts(self):
        blocks = table_blocks()
        watermark = blocks["lh_demo_ctl.batch_watermarks"]
        for name in (
            "source_table",
            "last_watermark",
            "last_primary_key",
            "snapshot_id",
            "updated_at",
        ):
            self.assertIn("[" + name + "]", watermark)
        self.assertIn("[last_watermark] datetime2(7) NULL", watermark)
        runs = blocks["lh_demo_ctl.demo_runs"]
        for name in (
            "run_id",
            "seed",
            "quality_mode",
            "record_count",
            "period_days",
            "target_mode",
            "started_at",
            "completed_at",
            "status",
            "error_summary",
        ):
            self.assertIn("[" + name + "]", runs)

    def test_permission_grants_are_object_only_and_principals_are_separate(
        self,
    ):
        sql = source("003_demo_permissions.sql")
        grants = re.findall(r"GRANT (.*?) ON (.*?) TO \[([a-z_]+)\];", sql)
        self.assertEqual(len(grants), 27)
        for permissions, target, role in grants:
            self.assertTrue(target.startswith("OBJECT::[lh_demo_"))
            self.assertTrue(
                set(permissions.split(", "))
                <= {
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "VIEW DEFINITION",
                    "EXECUTE",
                }
            )
            if "installation" in target:
                self.assertEqual(permissions, "SELECT")
            if role == "lh_demo_reader":
                self.assertEqual(permissions, "SELECT")
            if role == "lh_demo_control_writer":
                self.assertTrue(
                    "installation" in target
                    or "batch_watermarks" in target
                    or "check_write_safety_v1" in target
                )
        self.assertNotRegex(
            sql, r"(?i)\bCREATE\s+(LOGIN|USER)\b|\bREVOKE\b|\bPASSWORD\b"
        )
        self.assertNotRegex(
            sql, r"(?i)\bGRANT\s+(CONTROL|ALTER|CREATE)\b|\bdb_datawriter\b"
        )
        self.assertIn("three distinct existing database users", sql)
        self.assertIn("QUOTENAME(@Writer)", sql)
        self.assertIn("An existing role has unexpected permissions", sql)

    def test_privileged_checker_is_fixed_read_only_and_exactly_pinned(self):
        sql = (DIRECTORY / "003_demo_permissions.sql").read_text()
        literal = re.search(
            r"DECLARE @SafetyDefinition nvarchar\(max\) = N'((?:[^']|'')*)';",
            sql,
        )
        self.assertIsNotNone(literal)
        self.assertEqual(
            literal.group(1).replace("''", "'"), DEMO.SAFETY_DEFINITION
        )
        checker = DEMO.SAFETY_DEFINITION
        self.assertIn("WITH EXECUTE AS 'dbo'", checker)
        self.assertIn("USER_ID()=DATABASE_PRINCIPAL_ID(N'dbo')", checker)
        self.assertIn("fk.parent_object_id", checker)
        self.assertIn("fk.referenced_object_id", checker)
        self.assertIn("tr.is_disabled=0", checker)
        self.assertIn("))=9", checker)
        self.assertNotRegex(
            checker,
            r"(?i)\b(INSERT|UPDATE|DELETE|MERGE|ALTER|DROP|GRANT|REVOKE)\b",
        )
        self.assertNotIn("@", checker)
        self.assertNotIn("sp_executesql", checker)
        self.assertIn("DATALENGTH(m.[definition])", sql)
        self.assertIn("m.execute_as_principal_id", sql)
        self.assertIn("COALESCE(o.principal_id,s.principal_id)", sql)
        self.assertNotIn("CREATE OR ALTER", sql)
        for permissions, target, role in re.findall(
            r"GRANT (.*?) ON (.*?) TO \[([a-z_]+)\];",
            source("003_demo_permissions.sql"),
        ):
            if "EXECUTE" in permissions:
                self.assertEqual(
                    target, "OBJECT::[lh_demo_ctl].[check_write_safety_v1]"
                )
                self.assertNotEqual(role, "lh_demo_reader")

    def test_validation_does_not_print_ownership_or_mutate_data(self):
        sql = source("006_demo_validation.sql")
        marker = sql.index("IF OBJECT_ID(N'lh_demo_ctl.installation',N'U')")
        validation = sql[marker:]
        self.assertNotRegex(
            validation,
            r"(?i)\bINSERT\b|\bUPDATE\b|\bDELETE\b|\bGRANT\b|\bALTER\b",
        )
        self.assertNotIn("ownership_hash", validation)
        self.assertNotIn("original_values", validation)
        self.assertIn("OBJECT_ID(N'cdc.change_tables'", validation)

    def test_cdc_preserves_the_existing_metadata_safety_checker(self):
        """CDC setup accepts the installed checker without redefining it."""
        baseline = source("006_demo_validation.sql")
        marker = "AND s.name+N'.'+o.name NOT IN ("
        expected = baseline.split(marker, 1)[1].split(")", 1)[0]
        for filename in CDC_FILES:
            with self.subTest(filename=filename):
                sql = source(filename)
                actual = sql.split(marker, 1)[1].split(")", 1)[0]
                self.assertEqual(actual, expected)
                self.assertIn("lh_demo_ctl.check_write_safety_v1", actual)
                self.assertNotRegex(
                    sql, r"(?i)(?:ALTER|CREATE|DROP)\s+PROCEDURE"
                )

    def test_cdc_target_and_captured_columns_are_exactly_the_six_contracts(
        self,
    ):
        """Check SQL allowlists without executing a CDC procedure."""
        expected_targets = []
        expected_columns = []
        for ordinal, (contract, item) in enumerate(DEMO.TABLES.items(), 1):
            capture = DEMO.CAPTURE_INSTANCES[contract]
            columns = [
                *item["columns"],
                "created_at",
                "run_id",
                "original_values",
            ]
            expected_targets.append(
                (
                    str(ordinal),
                    item["schema"],
                    item["table"],
                    capture,
                    item["pk"],
                    ",".join("[" + name + "]" for name in columns),
                )
            )
            expected_columns.extend(
                (capture, str(index), name)
                for index, name in enumerate(columns, 1)
            )
        for filename in CDC_FILES:
            with self.subTest(filename=filename):
                sql = source(filename)
                targets = re.findall(
                    r"\((\d+),N'([^']+)',N'([^']+)',\s*"
                    r"N'([^']+)',N'([^']+)',\s*N'([^']+)'\)",
                    sql,
                )
                self.assertEqual(targets, expected_targets)
                columns = re.findall(
                    r"\(N'(lh_demo_[a-z_]+_v1)',(\d+),N'([^']+)'\)", sql
                )
                self.assertEqual(columns, expected_columns)
                self.assertIn("i.is_primary_key=1", sql)
                self.assertIn("ic.key_ordinal=1", sql)
                self.assertIn("ic.key_ordinal>0)<>1", sql)
                self.assertIn("c.is_computed=1", sql)
                self.assertIn("d.type='PS'", sql)

    def test_cdc_existing_identity_columns_and_types_fail_before_changes(self):
        """Check guards before any existing capture could be replaced."""
        for filename in CDC_FILES:
            with self.subTest(filename=filename):
                sql = source(filename)
                mutation = sql.index("EXEC @Result=sys.sp_cdc_")
                for error in (
                    "THROW 51108",
                    "THROW 51109",
                    "THROW 51110",
                    "THROW 51111",
                    "THROW 51112",
                    "THROW 51113",
                    "THROW 51114",
                    "THROW 51115",
                    "THROW 51116",
                    "THROW 51117",
                    "THROW 51118",
                    "THROW 51128",
                ):
                    self.assertLess(sql.index(error), mutation)
                for check in (
                    "ct.source_object_id<>t.source_object_id",
                    "ct.role_name IS NULL",
                    "COALESCE(ct.supports_net_changes,1)<>0",
                    "COALESCE(ct.has_drop_pending,1)<>0",
                    "ct.index_name IS NOT NULL",
                    "FROM @ExpectedColumns e JOIN cdc.change_tables ct",
                    "FROM @ActualColumns",
                    "src.column_id IS NULL",
                    "src.system_type_id<>changed.system_type_id",
                    "src.max_length<>changed.max_length",
                    "src.[precision]<>changed.[precision]",
                    "src.[scale]<>changed.[scale]",
                    "c.is_nullable<>e.nullable_value",
                    "TYPE_NAME(c.user_type_id)",
                ):
                    self.assertIn(check, sql)
                # Match both CDC scripts against the reviewed 002 type rows.
                type_pattern = r"^    \(N'lh_demo_(?:erp|mes)'[^\n]+"
                expected_types = re.findall(
                    type_pattern, source("002_demo_tables.sql"), re.MULTILINE
                )
                actual_types = re.findall(type_pattern, sql, re.MULTILINE)
                self.assertEqual(
                    [row.rstrip(",;") for row in actual_types],
                    [row.rstrip(",;") for row in expected_types],
                )

    def test_cdc_role_is_owned_narrow_read_only_and_never_changes_logins(self):
        """The adapter reader gains fixed CDC access, never write authority."""
        for filename in CDC_FILES:
            with self.subTest(filename=filename):
                sql = source(filename)
                self.assertIn("$(SourceReaderUser)", sql)
                self.assertIn("DATABASE_PRINCIPAL_ID(@Reader) IS NULL", sql)
                self.assertIn("r.type='R'", sql)
                self.assertIn(
                    "r.owning_principal_id=DATABASE_PRINCIPAL_ID(N'dbo')", sql
                )
                self.assertIn("m.member_principal_id", sql)
                self.assertRegex(
                    sql,
                    r"p\.permission_name\s+COLLATE\s+"
                    r"Latin1_General_100_BIN2\s*<>\s*N'SELECT'\s+"
                    r"COLLATE\s+Latin1_General_100_BIN2",
                )
                self.assertIn("p.class<>1 OR p.minor_id<>0", sql)
                self.assertIn("p.state<>'G'", sql)
                self.assertIn("@AllowedReadObjects", sql)
                self.assertNotRegex(
                    sql, r"(?i)\bCREATE\s+(LOGIN|USER)\b|\bPASSWORD\b"
                )
                self.assertNotRegex(
                    sql,
                    r"(?i)\bGRANT\s+(?:CONTROL|ALTER|INSERT|UPDATE|DELETE|"
                    r"EXECUTE|VIEW)\b|\bON\s+SCHEMA::|\bdb_datawriter\b",
                )
        enabled = source(CDC_FILES[0])
        self.assertIn("QUOTENAME(@Reader)", enabled)
        self.assertIn(
            "CREATE ROLE [lh_demo_cdc_reader] AUTHORIZATION [dbo]", enabled
        )
        self.assertEqual(
            set(
                re.findall(
                    r"GRANT SELECT ON OBJECT::\[cdc\]\.\[([^]]+)\]", enabled
                )
            ),
            {"change_tables", "captured_columns", "lsn_time_mapping"},
        )
        self.assertNotRegex(
            source(CDC_FILES[1]), r"(?i)\bGRANT\b|\bALTER\s+ROLE\b"
        )

    def test_cdc_jobs_are_preserved_and_partial_retry_is_explicit(self):
        """Check safety contracts without claiming live SQL behavior."""
        for filename in CDC_FILES:
            with self.subTest(filename=filename):
                sql = source(filename)
                raw = (DIRECTORY / filename).read_text()
                self.assertIn("@@TRANCOUNT <> 0", sql)
                self.assertIn("SET IMPLICIT_TRANSACTIONS OFF", sql)
                self.assertIn("sys.sp_getapplock", sql)
                self.assertIn("@LockOwner=N'Session'", sql)
                self.assertIn("sys.sp_releaseapplock", sql)
                self.assertIn("is_cdc_enabled=1", sql)
                self.assertGreaterEqual(
                    sql.count("independent.is_tracked_by_cdc=1"), 2
                )
                self.assertGreaterEqual(
                    sql.count("independent.object_id=ct.source_object_id"), 2
                )
                self.assertLess(
                    sql.index("COMMIT TRANSACTION"),
                    sql.index("EXEC @Result=sys.sp_cdc_"),
                )
                self.assertIn("Partial completion can remain", raw)
                self.assertIn("SQL Server 2022 compilation", raw)
                self.assertIn("actual CDC execution remain unverified", raw)
                self.assertNotRegex(
                    sql,
                    r"(?i)\bALTER\s+DATABASE\b|sp_cdc_(?:enable_db|disable_db|"
                    r"add_job|drop_job|change_job|start_job|stop_job)|"
                    r"\bUPDATE\s+(?:msdb|cdc)\.|\bsp_update_job\b",
                )
                self.assertIn(
                    "o.is_ms_shipped=0 AND o.parent_object_id=0", sql
                )
        enabled = source(CDC_FILES[0])
        # Full identity/column/type checks run again after new captures exist.
        self.assertEqual(enabled.count("THROW 51112"), 2)
        self.assertEqual(enabled.count("THROW 51113"), 2)
        self.assertEqual(enabled.count("THROW 51114"), 2)
        self.assertIn("@supports_net_changes=0", enabled)
        self.assertIn("@captured_column_list=@CapturedColumns", enabled)
        self.assertIn("@allow_partition_switch=0", enabled)

    def test_cdc_disable_requires_separate_exact_confirmation(self):
        """The destructive CDC-history operation has no broad/all option."""
        sql = source(CDC_FILES[1])
        self.assertIn("$(ConfirmDisableCdc)", sql)
        self.assertIn("DATALENGTH(@ConfirmDisableCdc)<>2", sql)
        self.assertIn(
            "@ConfirmDisableCdc COLLATE Latin1_General_100_BIN2<>N'1'", sql
        )
        self.assertLess(
            sql.index("THROW 51123"), sql.index("BEGIN TRANSACTION")
        )
        self.assertIn("@capture_instance=@Capture", sql)
        self.assertNotRegex(sql, r"(?i)@capture_instance\s*=\s*N?'all'")
        self.assertNotIn("sp_cdc_enable_table", sql)
        self.assertIn("THROW 51129", sql)
        self.assertNotRegex(sql, r"(?i)\bDELETE\s+FROM\s+\[lh_demo_")


if __name__ == "__main__":
    unittest.main()
