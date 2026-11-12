"""Offline AST boundaries; behavioral cases run only in SQL2022 CI."""

import importlib.util
import sys
import unittest
from pathlib import Path

try:
    import sqlfluff  # noqa: F401
except ModuleNotFoundError as error:
    if error.name != "sqlfluff":
        raise
    raise unittest.SkipTest("Install hashed requirements-dev.lock") from error

HERE = Path(__file__).resolve().parent / "integration/sqlserver_collation"
SPEC = importlib.util.spec_from_file_location(
    "cdc_health_contract", HERE / "health_contract.py"
)
CONTRACT = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(HERE))
try:
    SPEC.loader.exec_module(CONTRACT)
finally:
    sys.path.pop(0)


class HealthContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blocks = {
            name: CONTRACT.extract(
                (CONTRACT.ROOT / "infra/sqlserver" / name).read_text()
            )
            for name in CONTRACT.FILES
        }

    def test_only_relation_names_change(self):
        for name, (original, mapped) in self.blocks.items():
            with self.subTest(file=name):
                self.assertEqual(
                    original,
                    mapped.replace("#capture", "cdc.change_tables").replace(
                        "#objects", "sys.tables"
                    ),
                )
                self.assertTrue(original.startswith("IF EXISTS ("))
                self.assertTrue(original.endswith("review.', 1;"))

    def test_missing_duplicate_and_invalid_blocks_fail_closed(self):
        original = next(iter(self.blocks.values()))[0]
        invalid = [
            "SELECT 1;",
            "-- " + original.replace("\n", "\n-- "),
            original + "\n" + original,
            original.replace("IF EXISTS", "IF NOT EXISTS"),
            original.replace("LEFT JOIN", "INNER JOIN", 1),
            original.replace("THROW 51130", "THROW 51131"),
            original.replace("SELECT 1", "SELECT 1; DELETE sys.tables"),
            original.replace("ct.source_object_id", "ct.object_id", 1),
            original.replace("sys.tables src", "dbo.tables src"),
            original.replace("ct.has_drop_pending<>0", "ABS(1)=1"),
            original.replace("THROW 51130", "BEGIN PRINT 'bad'; THROW 51130")
            + " END;",
            "IF 1=1 BEGIN\n" + original + "\nEND;",
            original.replace("WHERE src", "WHERE ) src"),
        ]
        for index, sql in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(ValueError):
                CONTRACT.extract(sql)

    def test_behavioral_mutants_reach_sql_without_predicate_rewrite(self):
        for original, _ in self.blocks.values():
            variants = CONTRACT.mutants(original)
            self.assertEqual(len(variants), 7)
            for name, sql in variants.items():
                with self.subTest(variant=name):
                    extracted, mapped = CONTRACT.extract(sql)
                    self.assertEqual(extracted, sql)
                    self.assertIn(mapped, CONTRACT.matrix(mapped))

    def test_complete_matrix_and_real_left_join_witness(self):
        self.assertEqual(len(set(CONTRACT.CASES)), 24)
        _, mapped = next(iter(self.blocks.values()))
        sql = CONTRACT.matrix(mapped)
        self.assertEqual(sql.count(mapped), 24)
        self.assertEqual(sql.count("UPDATE #sentinel SET value=value+1;"), 24)
        self.assertIn("LEFT JOIN #objects src ON", sql)
        self.assertIn("IF 0=1 INSERT #objects VALUES (10,1);", sql)
        parsed = CONTRACT.Linter(dialect="tsql").parse_string(sql)
        self.assertEqual(parsed.violations, [])


if __name__ == "__main__":
    unittest.main()
