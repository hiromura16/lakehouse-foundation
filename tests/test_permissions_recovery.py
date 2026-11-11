"""Source/AST recovery contracts only; no SQL Server execution is implied."""

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
DIRECTORY = ROOT / "infra/sqlserver"
PREFLIGHT = DIRECTORY / "diagnostics/003_permissions_preflight.sql"
BINARY = "Latin1_General_100_BIN2"


def parse(sql):
    """Use the repository's pinned T-SQL parser, never a DB connection."""
    result = Linter(dialect="tsql").parse_string(sql)
    if result.violations:
        raise AssertionError("Public T-SQL could not be parsed.")
    return result.tree


def direct_comparisons(tree):
    """Inspect actual expression operands rather than comments/string bodies.

    These relevant predicates have simple column operands. Split at boolean
    operators and inspect the left/right AST around their comparison node.
    This deliberately does not claim a complete T-SQL semantic type checker.
    """
    for expression in tree.recursive_crawl("expression"):
        code = [segment for segment in expression.segments if segment.is_code]
        boundaries = (
            [-1]
            + [
                index
                for index, item in enumerate(code)
                if item.is_type("binary_operator")
                and item.raw.upper() in {"AND", "OR"}
            ]
            + [len(code)]
        )
        for start, end in zip(boundaries, boundaries[1:]):
            first = start + 1
            clause = code[first:end]
            for index, item in enumerate(clause):
                if item.is_type("comparison_operator"):
                    following = index + 1
                    yield clause[:index], clause[following:]


def operands_are_binary(left, right):
    """Require the same explicit chosen collation on BOTH operand ASTs."""
    return all(
        any(s.is_type("collation_reference") and s.raw == BINARY for s in side)
        for side in (left, right)
    )


def referenced_columns(segments):
    """Return only direct column references from one simple operand."""
    return [s.raw for s in segments if s.is_type("column_reference")]


class PermissionCollationTests(unittest.TestCase):
    """Guard the reported mixed-column comparison and identity boundaries."""

    def test_reported_comparisons_have_bilateral_binary_operands(self):
        tree = parse((DIRECTORY / "003_demo_permissions.sql").read_text())
        required = {
            ("a.role_name", "r.name"),
            ("a.permission_name", "p.permission_name"),
            ("actual.name", "requested.user_name"),
        }
        found = set()
        for left, right in direct_comparisons(tree):
            names = (referenced_columns(left), referenced_columns(right))
            if len(names[0]) == len(names[1]) == 1:
                pair = (names[0][0], names[1][0])
                if pair in required:
                    self.assertTrue(operands_are_binary(left, right), pair)
                    found.add(pair)
        self.assertEqual(found, required)

    def test_checker_does_not_accept_one_sided_or_unrelated_collation(self):
        for predicate, expected in (
            ("a.role_name=r.name", False),
            ("a.role_name COLLATE " + BINARY + "=r.name", False),
            (
                "a.role_name COLLATE " + BINARY + "=r.name COLLATE " + BINARY,
                True,
            ),
        ):
            with self.subTest(predicate=predicate):
                tree = parse(
                    "SELECT 1 FROM sys.database_principals r WHERE "
                    + predicate
                )
                left, right = next(direct_comparisons(tree))
                self.assertEqual(operands_are_binary(left, right), expected)

    def test_diagnostic_preserves_canonical_definition_and_grant_allowlist(
        self,
    ):
        install = (DIRECTORY / "003_demo_permissions.sql").read_text()
        diagnostic = PREFLIGHT.read_text()
        canonical = (
            (ROOT / "scripts/sqlserver_safety.sql").read_text().rstrip("\n")
        )
        for sql in (install, diagnostic):
            literal = re.search(
                r"DECLARE @SafetyDefinition nvarchar\(max\) = "
                r"N'((?:[^']|'')*)';",
                sql,
            )
            self.assertIsNotNone(literal)
            self.assertEqual(literal.group(1).replace("''", "'"), canonical)
            self.assertIn("DATALENGTH(m.[definition])", sql)
            self.assertIn("m.execute_as_principal_id", sql)
            self.assertIn("m.uses_ansi_nulls", sql)
            self.assertIn("m.uses_quoted_identifier", sql)
            self.assertNotIn("CREATE OR ALTER", sql)
        grants = r"INSERT INTO @AllowedGrants VALUES(.*?);"
        self.assertEqual(
            re.search(grants, install, re.S).group(1),
            re.search(grants, diagnostic, re.S).group(1),
        )

    def test_outer_transaction_refusal_precedes_abort_settings(self):
        """Reject using RAISERROR/RETURN, never THROW under inherited ON."""
        for path in (DIRECTORY / "003_demo_permissions.sql", PREFLIGHT):
            with self.subTest(path=path.name):
                sql = path.read_text()
                before = sql[: sql.index("SET IMPLICIT_TRANSACTIONS OFF;")]
                self.assertIn("IF @@TRANCOUNT<>0", before)
                self.assertIn("RAISERROR(", before)
                self.assertIn("RETURN;", before)
                code = "\n".join(
                    line
                    for line in before.splitlines()
                    if not line.lstrip().startswith("--")
                )
                self.assertNotRegex(code, r"\bTHROW\b|SET XACT_ABORT")
                parse(sql)

    def test_catch_observes_failure_without_automatic_checker_repair(self):
        sql = (DIRECTORY / "003_demo_permissions.sql").read_text()
        tree = parse(sql)
        self.assertLess(sql.index("IF @@TRANCOUNT"), sql.index("BEGIN TRY"))
        self.assertIn("checker_existed_before", sql)
        self.assertIn("checker_created_this_attempt", sql)
        self.assertIn("checker_present_after_failure", sql)
        self.assertIn("IF XACT_STATE()<>0 ROLLBACK TRANSACTION", sql)
        self.assertIn("diagnostic_unavailable", sql)
        self.assertTrue(sql.rstrip().endswith("THROW;\nEND CATCH;"))
        self.assertFalse(
            list(tree.recursive_crawl("drop_procedure_statement"))
        )
        self.assertNotRegex(sql, r"(?i)ALTER\s+PROCEDURE|ERROR_MESSAGE\s*\(")

    def test_readonly_preflight_checks_ownership_and_outputs_no_definition(
        self,
    ):
        sql = PREFLIGHT.read_text()
        tree = parse(sql)
        for kind in (
            "create_procedure_statement",
            "create_role_statement",
            "grant_statement",
            "alter_role_statement",
            "drop_procedure_statement",
            "update_statement",
            "delete_statement",
        ):
            self.assertFalse(list(tree.recursive_crawl(kind)), kind)
        self.assertIn("@ActualId COLLATE " + BINARY, sql)
        self.assertIn("@OwnershipHash COLLATE " + BINARY, sql)
        self.assertIn("IS_MEMBER(N'db_owner')", sql)
        self.assertIn("mutations_performed", sql)
        self.assertIn("unexpected_permissions", sql)
        self.assertIn("exact_user_name", sql)
        self.assertIn("user_present", sql)
        self.assertIn("inherited_role_memberships", sql)
        # A comparison needs m.definition; the actual module body is never
        # projected as a result column. Check SELECT elements, not raw text.
        for element in tree.recursive_crawl("select_clause_element"):
            self.assertNotRegex(
                element.raw.strip(),
                r"^m\.\[?definition\]?$|"
                r"^@(?:ActualId|ActualHash|OwnershipHash)$",
            )

    def test_006_has_no_table_variable_or_set_comparison_to_patch(self):
        sql = (DIRECTORY / "006_demo_validation.sql").read_text()
        tree = parse(sql)
        self.assertNotRegex(sql, r"(?i)DECLARE\s+@\w+\s+TABLE")
        self.assertNotRegex(sql, r"(?i)\bEXCEPT\b|\bINTERSECT\b|\bMERGE\b")
        for left, right in direct_comparisons(tree):
            for name in referenced_columns(left) + referenced_columns(right):
                if name.endswith(
                    (".name", ".permission_name", ".capture_instance")
                ):
                    # 006's identifier filters use catalog/literal IN only.
                    self.assertFalse(
                        referenced_columns(left) and referenced_columns(right)
                    )
