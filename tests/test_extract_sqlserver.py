"""Check the read-only SQL bridge with recorded queries and temporary files."""

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "source_extract", ROOT / "scripts/extract-sqlserver.py"
)
EXTRACTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACTOR)
HELPER = EXTRACTOR.source_io()
CONTRACTS = HELPER.contracts()
EXAMPLE = json.loads(
    (ROOT / "infra/source/sqlserver-tables.example.json").read_text()
)


class SourceCursor:
    """Return the requested synthetic source rows, recording every query."""

    def __init__(self, data):
        self.data = data
        self.statements = []
        self.current = []
        self.offset = 0

    def execute(self, statement):
        self.statements.append(statement)
        table = re.search(r"FROM \[[^]]+\]\.\[([^]]+)\]", statement)[1]
        columns = next(
            item["columns"] for item in CONTRACTS if item["table"] == table
        )
        self.current = [
            tuple(row[column] for column in columns)
            for row in reversed(self.data[table])
        ]
        self.offset = 0

    def fetchmany(self, size):
        start = self.offset
        stop = start + size
        result = self.current[start:stop]
        self.offset += len(result)
        return result


class SourceConnection:
    """Provide a recording cursor without any commit or mutation interface."""

    def __init__(self, data):
        self.recorder = SourceCursor(data)
        self.closed = False

    def cursor(self):
        return self.recorder

    def close(self):
        self.closed = True


class SourceExtractionTests(unittest.TestCase):
    """Check SELECT-only extraction and bounded landing publication."""

    def settings(self):
        return {
            "SOURCE_SQLSERVER_HOST": "source.example.invalid",
            "SOURCE_SQLSERVER_PORT": "1433",
            "SOURCE_SQLSERVER_DATABASE": "FactorySource",
            "SOURCE_SQLSERVER_USER": "source_reader",
            "SOURCE_SQLSERVER_PASSWORD": "synthetic-test-password",
            "SOURCE_SQLSERVER_CA_FILE": "system",
            "SOURCE_SQLSERVER_TIMEZONE": "UTC",
        }

    def configuration(self, directory):
        directory = Path(directory)
        settings = directory / "site.env"
        settings.write_text(
            "\n".join(
                key + "=" + value for key, value in self.settings().items()
            )
            + "\n"
        )
        settings.chmod(0o600)
        mapping = directory / "tables.json"
        mapping.write_text(json.dumps(EXAMPLE))
        mapping.chmod(0o600)
        return settings, mapping

    def test_select_only_with_explicit_columns_and_tls(self):
        data = HELPER.build_data(scenario="clean")
        connection = SourceConnection(data)
        driver = mock.Mock()
        driver.connect.return_value = connection
        example = json.loads(json.dumps(EXAMPLE))
        mapping = {item["target_table"]: item for item in example["tables"]}
        columns = mapping["erp_products"]["columns"]
        columns["ITEM_CODE"] = columns.pop("product_id")
        with mock.patch.dict(sys.modules, {"pymssql": driver}):
            actual = EXTRACTOR.extract_tables(
                self.settings(), mapping, CONTRACTS, 100000
            )
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.recorder.statements), 6)
        self.assertIn("[ITEM_CODE]", connection.recorder.statements[0])
        for statement in connection.recorder.statements:
            self.assertTrue(statement.startswith("SELECT TOP (100001) "))
            self.assertNotIn("*", statement)
            self.assertNotIn(";", statement)
        for source in CONTRACTS:
            columns = source["columns"]
            expected = sorted(
                data[source["table"]],
                key=lambda row: tuple(row[column] for column in columns),
            )
            self.assertEqual(actual[source["table"]], expected)
        arguments = driver.connect.call_args.kwargs
        self.assertTrue(arguments["read_only"])
        self.assertTrue(arguments["autocommit"])
        self.assertEqual(arguments["encryption"], "require")

    def test_scalar_datetime_and_decimal_contract(self):
        zone = ZoneInfo("Asia/Tokyo")
        self.assertEqual(
            EXTRACTOR.canonical_value(datetime(2026, 1, 5, 9), zone),
            "2026-01-05T00:00:00Z",
        )
        self.assertEqual(
            EXTRACTOR.canonical_value(
                datetime(2026, 1, 5, tzinfo=timezone.utc), zone
            ),
            "2026-01-05T00:00:00Z",
        )
        self.assertEqual(
            EXTRACTOR.canonical_value(Decimal("12.50"), zone), "12.50"
        )
        self.assertEqual(
            EXTRACTOR.canonical_value(date(2026, 1, 5), zone), "2026-01-05"
        )
        self.assertEqual(EXTRACTOR.canonical_value(None, zone), "")
        with self.assertRaises(ValueError):
            EXTRACTOR.canonical_value(b"unsupported-binary", zone)

    def test_row_limit_publishes_nothing(self):
        connection = SourceConnection(HELPER.build_data())
        driver = mock.Mock()
        driver.connect.return_value = connection
        with tempfile.TemporaryDirectory() as directory:
            settings, mapping = self.configuration(directory)
            output = Path(directory) / "unpublished"
            with mock.patch.dict(sys.modules, {"pymssql": driver}):
                with self.assertRaises(ValueError):
                    EXTRACTOR.main(
                        [
                            "--env-file",
                            str(settings),
                            "--table-map",
                            str(mapping),
                            "--output",
                            str(output),
                            "--max-rows",
                            "42",
                        ]
                    )
            self.assertEqual(len(connection.recorder.statements), 5)
            self.assertFalse(output.exists())

    def test_mapping_rejects_sql_expressions_and_incomplete_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            _, mapping = self.configuration(directory)
            value = json.loads(mapping.read_text())
            value["tables"][0]["source_schema"] = "dbo]; DROP TABLE x"
            mapping.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                EXTRACTOR.read_mapping(mapping, CONTRACTS)
            value = json.loads(json.dumps(EXAMPLE))
            value["tables"][0]["columns"].pop("product_id")
            mapping.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                EXTRACTOR.read_mapping(mapping, CONTRACTS)

    def test_completed_files_and_overwrite_refusal(self):
        connection = SourceConnection(HELPER.build_data())
        driver = mock.Mock()
        driver.connect.return_value = connection
        with tempfile.TemporaryDirectory() as directory:
            settings, mapping = self.configuration(directory)
            output = Path(directory) / "landing"
            arguments = [
                "--env-file",
                str(settings),
                "--table-map",
                str(mapping),
                "--output",
                str(output),
            ]
            with mock.patch.dict(sys.modules, {"pymssql": driver}):
                EXTRACTOR.main(arguments)
            manifest = json.loads((output / "source-extract.json").read_text())
            self.assertEqual(len(manifest["sources"]), 6)
            for source in manifest["sources"]:
                path = output / source["path"]
                self.assertEqual(
                    path.with_name(path.name + ".ready").read_text().strip(),
                    source["sha256"],
                )
            with mock.patch.dict(sys.modules, {"pymssql": driver}):
                driver.connect.reset_mock()
                with self.assertRaises(ValueError):
                    EXTRACTOR.main(arguments)
                driver.connect.assert_not_called()

    def test_certificate_exception_is_loopback_only(self):
        with self.assertRaises(ValueError):
            with EXTRACTOR.tls_configuration(self.settings(), True):
                self.fail("An external source cannot bypass trust checks.")


if __name__ == "__main__":
    unittest.main()
