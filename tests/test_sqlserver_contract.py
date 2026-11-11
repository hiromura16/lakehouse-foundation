"""Static SQL contracts only: these tests never execute T-SQL on a server.

They keep public DDL aligned with the source adapter and prevent accidental
broad grants or legacy-object targeting. SQL Server compilation, effective
permissions, typed round trips and CDC capture remain separate live tests.
"""

import importlib.util
import re
import unittest
from pathlib import Path

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
    "006_demo_validation.sql",
)


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
        self.assertEqual(len(grants), 25)
        for permissions, target, role in grants:
            self.assertTrue(target.startswith("OBJECT::[lh_demo_"))
            self.assertTrue(
                set(permissions.split(", "))
                <= {"SELECT", "INSERT", "UPDATE", "DELETE", "VIEW DEFINITION"}
            )
            if "installation" in target:
                self.assertEqual(permissions, "SELECT")
            if role == "lh_demo_reader":
                self.assertEqual(permissions, "SELECT")
            if role == "lh_demo_control_writer":
                self.assertTrue(
                    "installation" in target or "batch_watermarks" in target
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


if __name__ == "__main__":
    unittest.main()
