"""Audit CDC comparison syntax without claiming SQL Server execution.

SQLFluff supplies the parsed T-SQL structure, including both sides of each
comparison and each EXCEPT projection. These source contracts cannot reproduce
SQL Server collation coercion, transaction behavior, Agent jobs or CDC calls;
the separately gated synthetic integration fixture must exercise those.
"""

import importlib.util
import re
import unittest
from pathlib import Path

# Skip only an absent optional package; keep broken installs as errors.
try:
    from sqlfluff.core import Linter
except ModuleNotFoundError as error:
    if (
        error.name != "sqlfluff"
        or importlib.util.find_spec("sqlfluff") is not None
    ):
        raise
    raise unittest.SkipTest(
        "Optional tests need sqlfluff. In a uv virtual environment, run "
        "uv pip sync --require-hashes requirements-dev.lock."
    ) from error

ROOT = Path(__file__).resolve().parents[1]
BIN2 = "Latin1_General_100_BIN2"
CDC_FILES = (
    "004_demo_cdc_enable.sql",
    "005_demo_cdc_disable.sql",
)
STRING_COLUMNS = {
    "capture_instance",
    "column_name",
    "source_schema",
    "source_table",
    "type_name",
    "name",
    "role_name",
    "index_name",
    "primary_index",
    "primary_column",
    "permission_name",
}
STRING_PARAMETERS = {
    "@Capture",
    "@ConfirmDisableCdc",
    "@ActualId",
    "@ActualHash",
    "@InstallationId",
    "@OwnershipHash",
    "@ExpectedDatabase",
}
STRING_FUNCTIONS = (
    "OBJECT_NAME(",
    "OBJECT_SCHEMA_NAME(",
    "TYPE_NAME(",
    "DB_NAME(",
    "UPPER(",
)


def parse_sql(sql):
    """Parse public SQL only and fail on parser errors rather than skipping."""
    parsed = Linter(dialect="tsql").parse_string(sql)
    if parsed.violations:
        raise AssertionError("The public T-SQL contract did not parse.")
    return parsed.tree


def significant(segments):
    """Exclude trivia while retaining identifier/keyword/operand boundaries."""
    return [segment for segment in segments if segment.is_code]


def string_operand(segment):
    """Classify only reviewed identifier/permission operands, never enums."""
    if segment.is_type("column_reference"):
        name = segment.raw.rsplit(".", 1)[-1].strip("[]")
        return name in STRING_COLUMNS
    if segment.raw in STRING_PARAMETERS:
        return True
    return segment.raw.upper().startswith(STRING_FUNCTIONS)


def operand(parts, index, direction):
    """Read the adjacent operand and its explicit parsed COLLATE modifier."""
    if direction == -1:
        previous = index - 1
        collated = parts[previous].is_type("collation_reference")
        if collated:
            if previous < 2 or parts[previous - 1].raw.upper() != "COLLATE":
                raise AssertionError("Unexpected parsed collation structure.")
            return parts[previous - 2], parts[previous].raw
        return parts[previous], None
    following = index + 1
    if following >= len(parts):
        raise AssertionError("Missing parsed comparison operand.")
    if following + 2 < len(parts) and (
        parts[following + 1].raw.upper() == "COLLATE"
        and parts[following + 2].is_type("collation_reference")
    ):
        return parts[following], parts[following + 2].raw
    return parts[following], None


def comparison_findings(tree):
    """Audit both parsed operands, including guarded dynamic SELECTs."""
    checked, findings = 0, []
    for expression in tree.recursive_crawl("expression"):
        parts = significant(expression.segments)
        for index, segment in enumerate(parts):
            if not segment.is_type("comparison_operator"):
                continue
            left, left_collation = operand(parts, index, -1)
            right, right_collation = operand(parts, index, 1)
            if not (string_operand(left) or string_operand(right)):
                continue
            checked += 1
            if left_collation != BIN2 or right_collation != BIN2:
                findings.append((left.raw, segment.raw, right.raw))
    return checked, findings


def embedded_selects(tree):
    """Decode T-SQL string escaping, not arbitrary whitespace/comment edits."""
    for statement in tree.recursive_crawl("execute_script_statement"):
        references = [
            ref.raw for ref in statement.recursive_crawl("object_reference")
        ]
        if "sys.sp_executesql" not in references:
            continue
        for segment in statement.raw_segments:
            if not segment.is_type("quoted_literal"):
                continue
            raw = segment.raw.removeprefix("N")
            if not (raw.startswith("'") and raw.endswith("'")):
                continue
            decoded = raw[1:-1].replace("''", "'")
            if decoded.lstrip().upper().startswith("SELECT"):
                yield decoded


class CdcCollationTests(unittest.TestCase):
    """Keep mixed-source comparisons binary and diagnostics non-destructive."""

    @classmethod
    def setUpClass(cls):
        cls.sources = {
            name: (ROOT / "infra/sqlserver" / name).read_text()
            for name in CDC_FILES
        }
        cls.trees = {name: parse_sql(sql) for name, sql in cls.sources.items()}

    def test_caller_transaction_is_refused_before_any_session_change(self):
        """RAISERROR plus RETURN avoids THROW's XACT_ABORT participation."""
        for name, tree in self.trees.items():
            with self.subTest(name=name):
                batch = next(tree.recursive_crawl("batch"))
                first = next(
                    segment
                    for segment in batch.segments
                    if segment.is_type("statement")
                )
                condition = next(first.recursive_crawl("if_clause"))
                expression = next(condition.recursive_crawl("expression"))
                self.assertEqual(expression.raw, "@@TRANCOUNT <> 0")
                self.assertEqual(
                    len(list(first.recursive_crawl("raiserror_statement"))), 1
                )
                self.assertEqual(
                    len(list(first.recursive_crawl("return_segment"))), 1
                )
                self.assertLess(
                    first.raw.index("RAISERROR("), first.raw.index("RETURN;")
                )
                self.assertNotIn("THROW", first.raw)
                for forbidden in (
                    "set_statement",
                    "transaction_statement",
                    "execute_script_statement",
                    "select_statement",
                    "insert_statement",
                    "update_statement",
                ):
                    self.assertEqual(
                        list(first.recursive_crawl(forbidden)), []
                    )
                sql = self.sources[name]
                guard_start = sql.index("IF @@TRANCOUNT <> 0")
                guard_end = guard_start + len(first.raw)
                self.assertLess(guard_end, sql.index("SET XACT_ABORT ON"))
                self.assertLess(
                    guard_end, sql.index("SET IMPLICIT_TRANSACTIONS")
                )
                self.assertLess(guard_end, sql.index("THROW 51000"))
                self.assertNotIn("THROW 51100", sql)

    def test_every_reviewed_string_comparison_has_two_binary_operands(self):
        """Check AST operands, not the mere presence of a COLLATE word."""
        for name, tree in self.trees.items():
            with self.subTest(name=name):
                count, findings = comparison_findings(tree)
                self.assertGreaterEqual(count, 20)
                self.assertEqual(findings, [])
                nested = list(embedded_selects(tree))
                self.assertGreaterEqual(len(nested), 2)
                for statement in nested:
                    _, findings = comparison_findings(parse_sql(statement))
                    self.assertEqual(findings, [])

    def test_ast_audit_rejects_either_missing_collation_and_case_insensitive(
        self,
    ):
        """A comment or unrelated collation cannot satisfy either operand."""
        valid = (
            "SELECT 1 FROM cdc.change_tables ct JOIN @Targets t ON "
            "ct.capture_instance COLLATE " + BIN2 + " = "
            "t.capture_instance COLLATE " + BIN2 + ";"
        )
        self.assertEqual(comparison_findings(parse_sql(valid)), (1, []))
        mutants = (
            valid.replace(
                "ct.capture_instance COLLATE " + BIN2, "ct.capture_instance"
            ),
            valid.replace(
                "t.capture_instance COLLATE " + BIN2, "t.capture_instance"
            ),
            valid.replace(BIN2, "Latin1_General_CI_AS"),
        )
        for mutation in mutants:
            with self.subTest(mutation=mutation[:35]):
                self.assertTrue(comparison_findings(parse_sql(mutation))[1])

    def test_both_except_directions_collate_each_text_projection(self):
        """Every selected name has its own parsed collation modifier."""
        for name, tree in self.trees.items():
            with self.subTest(name=name):
                count = 0
                for compound in tree.recursive_crawl("set_expression"):
                    if "EXCEPT" not in compound.raw.upper():
                        continue
                    for element in compound.recursive_crawl(
                        "select_clause_element"
                    ):
                        columns = list(
                            element.recursive_crawl("column_reference")
                        )
                        if not columns or not string_operand(columns[0]):
                            continue
                        collations = list(
                            element.recursive_crawl("collation_reference")
                        )
                        self.assertEqual([c.raw for c in collations], [BIN2])
                        count += 1
                self.assertGreaterEqual(count, 8)

    def test_null_drop_pending_requires_an_existing_tracked_source(self):
        """Reject orphans/pending drops before any grant or CDC procedure."""
        for name, sql in self.sources.items():
            with self.subTest(name=name):
                self.assertNotIn("COALESCE(ct.has_drop_pending,1)", sql)
                start = sql.index("LEFT JOIN sys.tables src")
                end = sql.index("THROW 51130", start)
                guard = sql[start:end]
                self.assertIn("src.object_id=ct.source_object_id", guard)
                self.assertIn("src.object_id IS NULL", guard)
                self.assertIn("src.is_tracked_by_cdc<>1", guard)
                self.assertIn("ct.has_drop_pending IS NOT NULL", guard)
                self.assertIn("ct.has_drop_pending<>0", guard)
                self.assertLess(end, sql.index("EXEC @Result=sys.sp_cdc_"))

    def test_cdc_exec_named_arguments_are_plain_bindings_not_collated(self):
        """COLLATE belongs to comparisons, never EXEC parameter assignments."""
        expected = {
            "@source_schema": "@Schema",
            "@source_name": "@Table",
            "@capture_instance": "@Capture",
        }
        for name, tree in self.trees.items():
            with self.subTest(name=name):
                calls = [
                    call
                    for call in tree.recursive_crawl(
                        "execute_script_statement"
                    )
                    if "EXEC @Result=sys.sp_cdc_" in call.raw
                ]
                self.assertEqual(len(calls), 1)
                call = calls[0]
                self.assertNotIn("COLLATE", call.raw)
                parts = significant(call.segments)
                bindings = {}
                for index, segment in enumerate(parts[:-2]):
                    if (
                        segment.is_type("parameter")
                        and parts[index + 1].is_type("comparison_operator")
                        and parts[index + 2].is_type("expression")
                    ):
                        bindings[segment.raw] = parts[index + 2].raw
                exact = dict(expected)
                if name.startswith("004"):
                    exact.update(
                        {
                            "@role_name": "N'lh_demo_cdc_reader'",
                            "@supports_net_changes": "0",
                            "@captured_column_list": "@CapturedColumns",
                            "@index_name": "@PrimaryIndex",
                        }
                    )
                self.assertEqual(bindings, exact)

    def test_alias_discovery_precedes_exact_join_and_first_mutation(self):
        """Case aliases on another source must not disappear from preflight."""
        for name, sql in self.sources.items():
            with self.subTest(name=name):
                start = sql.index("-- Never add a second capture version")
                end = sql.index("THROW 51111", start)
                discovery = sql[start:end]
                self.assertIn(
                    "ct.source_object_id=t.source_object_id", discovery
                )
                self.assertIn("OR ct.object_id=OBJECT_ID(", discovery)
                self.assertIn(
                    "N'cdc.'+QUOTENAME(t.capture_instance+N'_CT')", discovery
                )
                self.assertIn("ct.capture_instance COLLATE " + BIN2, discovery)
                self.assertLess(end, sql.index("EXEC @Result=sys.sp_cdc_"))
                self.assertNotIn("DATABASE_DEFAULT", sql)

    def test_case_fold_is_binary_reject_only_discovery(self):
        """Stable folding finds ASCII aliases; exact identity still decides."""
        for name, sql in self.sources.items():
            with self.subTest(name=name):
                start = sql.index("-- Never add a second capture version")
                end = sql.index("\n", sql.index("THROW 51111", start))
                discovery = sql[start:end]
                tree = parse_sql(discovery)
                folded = [
                    function
                    for function in tree.recursive_crawl("function")
                    if function.raw.startswith("UPPER(")
                ]
                self.assertEqual(len(folded), 2)
                self.assertEqual(
                    [
                        next(function.recursive_crawl("column_reference")).raw
                        for function in folded
                    ],
                    ["ct.capture_instance", "t.capture_instance"],
                )
                for function in folded:
                    self.assertEqual(
                        [
                            collation.raw
                            for collation in function.recursive_crawl(
                                "collation_reference"
                            )
                        ],
                        [BIN2],
                    )
                checked, findings = comparison_findings(tree)
                self.assertEqual((checked, findings), (2, []))
                # Folding only broadens rejection. It cannot be used by any
                # existence/reuse query, count, cursor or mutation afterwards.
                self.assertNotIn("UPPER(", sql[:start] + sql[end:])
                self.assertRegex(
                    discovery,
                    r"WHERE ct\.capture_instance COLLATE "
                    + BIN2
                    + r"\s*<> t\.capture_instance COLLATE "
                    + BIN2,
                )
                self.assertLess(end, sql.index("EXEC @Result=sys.sp_cdc_"))
                self.assertIn(
                    "not general Unicode identifier equivalence", discovery
                )

    def test_shared_role_rejection_resolves_identity_without_authorizing_it(
        self,
    ):
        """Names can differ while native lookup resolves the same principal.

        Inspect the AST grouping: both rejection branches must remain inside
        parentheses before the exact fixed-capture exclusion. This is a
        source contract, not evidence of SQL Server's saved CDC spelling.
        """
        for name, sql in self.sources.items():
            with self.subTest(name=name):
                start = sql.index("IF EXISTS (", sql.index("THROW 51114"))
                end = sql.index("DECLARE @AllowedReadObjects", start)
                tree = parse_sql(sql[start:end])
                self.assertEqual(
                    len(list(tree.recursive_crawl("throw_statement"))), 1
                )
                self.assertIn("THROW 51115", tree.raw)
                guards = list(tree.recursive_crawl("if_clause"))
                self.assertEqual(len(guards), 1)
                guard = guards[0]
                where = next(guard.recursive_crawl("where_clause"))
                expression = next(where.recursive_crawl("expression"))
                outer = significant(expression.segments)
                self.assertEqual(
                    [part.raw.upper() for part in outer[1:4]],
                    ["AND", "NOT", "EXISTS"],
                )
                self.assertEqual(len(outer), 5)
                self.assertTrue(outer[0].is_type("bracketed"))
                identity = next(outer[0].recursive_crawl("expression"))
                tokens = significant(identity.segments)
                self.assertEqual(
                    [token.raw for token in tokens],
                    [
                        "ct.role_name",
                        "COLLATE",
                        BIN2,
                        "=",
                        "N'lh_demo_cdc_reader'",
                        "COLLATE",
                        BIN2,
                        "OR",
                        "DATABASE_PRINCIPAL_ID(ct.role_name)",
                        "=",
                        "DATABASE_PRINCIPAL_ID(N'lh_demo_cdc_reader')",
                    ],
                )
                exclusion = list(outer[4].recursive_crawl("table_reference"))
                self.assertEqual([ref.raw for ref in exclusion], ["@Targets"])
                self.assertEqual(comparison_findings(outer[4]), (1, []))
                self.assertEqual(
                    [
                        ref.raw
                        for ref in outer[4].recursive_crawl("column_reference")
                    ],
                    ["t.capture_instance", "ct.capture_instance"],
                )
                # The new native name resolution must occur only in this
                # rejection guard; existing authorization stays exact BIN2.
                self.assertEqual(
                    sql.count("DATABASE_PRINCIPAL_ID(ct.role_name)"), 1
                )
                self.assertLess(
                    sql.index("THROW 51115"),
                    sql.index("EXEC @Result=sys.sp_cdc_"),
                )
                self.assertIn(
                    "ct.role_name COLLATE "
                    + BIN2
                    + "\n          <> N'lh_demo_cdc_reader' COLLATE "
                    + BIN2,
                    sql,
                )

    def test_failure_output_compares_observed_presence_without_attribution(
        self,
    ):
        """Inspect current fixed state; never assume complete rollback."""
        for name, sql in self.sources.items():
            with self.subTest(name=name):
                self.assertLess(
                    sql.index("DECLARE @PreflightCapturePresence"),
                    sql.index("BEGIN TRY"),
                )
                self.assertLess(
                    sql.index("SET @CaptureSnapshotReady=1"),
                    sql.index("COMMIT TRANSACTION"),
                )
                self.assertLess(
                    sql.index("COMMIT TRANSACTION"),
                    sql.index("SET @CdcMutationAttempted=1"),
                )
                self.assertIn("AS preflight_presence", sql)
                self.assertIn("AS current_presence", sql)
                self.assertIn("AS observed_change", sql)
                self.assertIn("N'unknown'", sql)
                self.assertIn("N'became_present'", sql)
                self.assertIn("N'became_absent'", sql)
                self.assertIn("preserve_state_and_review_before_rerun", sql)
                self.assertIn(
                    "fixed_demo_cdc_failure_diagnostic_unavailable", sql
                )
                executable = re.sub(r"--[^\n]*", "", sql)
                self.assertNotIn("ERROR_MESSAGE()", executable)
                self.assertNotRegex(executable, r"(?i)\bDROP\b")
                self.assertRegex(sql, r"END CATCH;\s*IF CURSOR_STATUS")
                self.assertTrue(sql.rstrip().endswith("THROW;\nEND CATCH;"))

    def test_diagnostic_lookup_has_only_six_public_names(self):
        """Restrict dynamic diagnostics to public fixed capture names."""
        expected = {
            "lh_demo_erp_products_v1",
            "lh_demo_erp_work_orders_v1",
            "lh_demo_erp_sales_orders_v1",
            "lh_demo_erp_shipments_v1",
            "lh_demo_mes_events_v1",
            "lh_demo_mes_downtime_v1",
        }
        for name, tree in self.trees.items():
            with self.subTest(name=name):
                statements = [
                    s
                    for s in embedded_selects(tree)
                    if "fixed.capture_instance" in s
                ]
                self.assertEqual(len(statements), 1)
                statement = statements[0]
                values = set(re.findall(r"N'(lh_demo_[a-z_]+_v1)'", statement))
                self.assertEqual(values, expected)
                self.assertNotIn("$", statement)
                self.assertNotIn("sys.database_principals", statement)
                self.assertNotIn("GRANT", statement)
                self.assertNotIn("sp_cdc_", statement)


if __name__ == "__main__":
    unittest.main()
