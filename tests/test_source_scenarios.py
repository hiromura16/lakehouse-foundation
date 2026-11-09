"""Check anomaly effects and backward compatibility without downstream I/O."""

import contextlib
import copy
import csv
import importlib.util
import io
import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "anomaly_generator_subject", ROOT / "scripts/generate-demo.py"
)
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)
SCENARIOS = GENERATOR.SCENARIOS


class ScenarioTests(unittest.TestCase):
    """Exercise real row/file faults and the unchanged published fixture."""

    def setUp(self):
        self.data = GENERATOR.build_data(scenario="clean")

    def test_catalog_has_concrete_effects_and_compatible_presets(self):
        entries = SCENARIOS.catalog()
        self.assertEqual(len(entries), 26)
        self.assertEqual(len({item["id"] for item in entries}), 26)
        for item in entries:
            self.assertTrue(item["description"])
            self.assertTrue(item["expected_effect"])
            self.assertTrue(item["targets"])
            self.assertIs(type(item["requires_file_consent"]), bool)
        for preset in SCENARIOS.presets():
            SCENARIOS.validate_selection(
                preset["anomalies"], images=True, allow_file_faults=True
            )

    def test_every_row_scenario_changes_a_copy_reproducibly(self):
        original = copy.deepcopy(self.data)
        for identifier, *_ in SCENARIOS.ROW_SCENARIOS:
            with self.subTest(identifier=identifier):
                first, changes = SCENARIOS.apply_rows(
                    self.data, [identifier], 100, seed=42, batch=1
                )
                second, _ = SCENARIOS.apply_rows(
                    self.data, [identifier], 100, seed=42, batch=1
                )
                self.assertEqual(first, second)
                self.assertNotEqual(first, original)
                self.assertGreater(changes[0]["affected"], 0)
                self.assertEqual(self.data, original)

    def test_rate_sorted_selection_and_batch_seed_are_deterministic(self):
        selected = ["invalid_number", "broken_reference"]
        first, changes = SCENARIOS.apply_rows(self.data, selected, 10, 42, 1)
        reverse, _ = SCENARIOS.apply_rows(
            self.data, list(reversed(selected)), 10, 42, 1
        )
        changed_seed, _ = SCENARIOS.apply_rows(self.data, selected, 10, 43, 1)
        changed_batch, _ = SCENARIOS.apply_rows(self.data, selected, 10, 42, 2)
        self.assertEqual(first, reverse)
        self.assertNotEqual(first, changed_seed)
        self.assertNotEqual(first, changed_batch)
        self.assertEqual([item["affected"] for item in changes], [4, 4])

    def test_revision_and_missing_shipment_have_precise_effects(self):
        changed, _ = SCENARIOS.apply_rows(
            self.data, ["conflicting_revision"], 10
        )
        self.assertEqual(len(changed["mes_events"]), 44)
        originals = {row["event_id"]: row for row in self.data["mes_events"]}
        for revision in changed["mes_events"][40:]:
            original = originals[revision["event_id"]]
            self.assertGreater(revision["updated_at"], original["updated_at"])
            self.assertEqual(
                int(revision["good_qty"]), int(original["good_qty"]) + 10
            )
        invalid, _ = SCENARIOS.apply_rows(self.data, ["invalid_latest"], 10)
        self.assertTrue(
            all(
                row["good_qty"] == "not-a-number"
                for row in invalid["mes_events"][40:]
            )
        )
        missing, changes = SCENARIOS.apply_rows(
            self.data, ["missing_shipment"], 10
        )
        self.assertEqual(changes[0]["affected"], 4)
        counts = {}
        for row in missing["erp_shipments"]:
            key = row["sales_order_id"]
            counts[key] = counts.get(key, 0) + 1
        self.assertEqual(len(counts), 18)
        self.assertEqual(set(counts.values()), {2})

    def test_file_faults_change_the_intended_contract_only(self):
        original = GENERATOR.encoded_sources(self.data)
        original["images/inspection.png"] = SCENARIOS.synthetic_png(42)
        for identifier, _, path, *_ in SCENARIOS.FILE_SCENARIOS:
            with self.subTest(identifier=identifier):
                result, markers, faults = SCENARIOS.apply_files(
                    original, [identifier]
                )
                self.assertEqual(faults[0]["path"], path)
                for other in original.keys() - {path}:
                    self.assertEqual(result[other], original[other])
                ready = SCENARIOS.ready_payload(
                    result[path], markers.get(path)
                )
                if identifier == "missing_ready":
                    self.assertIsNone(ready)
                elif identifier == "wrong_checksum":
                    self.assertNotEqual(
                        ready.decode().strip(),
                        sha256(result[path]).hexdigest(),
                    )
                else:
                    self.assertEqual(
                        ready.decode().strip(),
                        sha256(result[path]).hexdigest(),
                    )
                if identifier in {"extra_column", "missing_column"}:
                    columns = csv.DictReader(
                        io.StringIO(result[path].decode())
                    ).fieldnames
                    expected = next(
                        source["columns"]
                        for source in GENERATOR.contracts()
                        if source["path"] == path
                    )
                    self.assertNotEqual(set(columns), set(expected))
                elif identifier == "truncated_json":
                    with self.assertRaises(json.JSONDecodeError):
                        for line in result[path].decode().splitlines():
                            json.loads(line)
                elif identifier == "invalid_encoding":
                    with self.assertRaises(UnicodeDecodeError):
                        result[path].decode()
                elif identifier == "corrupt_image":
                    self.assertFalse(result[path].startswith(b"\x89PNG"))
        self.assertEqual(
            original["images/inspection.png"], SCENARIOS.synthetic_png(42)
        )

    def test_invalid_and_unconsented_combinations_are_rejected(self):
        invalid = [
            {"anomalies": ["unknown"]},
            {"anomalies": ["duplicate_row", "duplicate_row"]},
            {"anomalies": [], "anomaly_rate": 0},
            {"anomalies": [], "anomaly_rate": True},
            {"anomalies": ["missing_ready"]},
            {
                "anomalies": ["missing_ready"],
                "destination": "sqlserver",
                "allow_file_faults": True,
            },
            {"anomalies": ["corrupt_image"], "allow_file_faults": True},
            {
                "anomalies": ["extra_column", "missing_column"],
                "allow_file_faults": True,
            },
            {
                "anomalies": ["missing_ready", "wrong_checksum"],
                "allow_file_faults": True,
            },
            {
                "anomalies": ["truncated_json", "invalid_encoding"],
                "allow_file_faults": True,
            },
            {"anomalies": ["zero_production", "overproduction"]},
        ]
        for options in invalid:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    SCENARIOS.validate_selection(**options)

    def test_cli_lists_without_output_and_records_intentional_faults(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            GENERATOR.main(["--list-scenarios"])
        self.assertEqual(len(json.loads(stream.getvalue())["scenarios"]), 26)
        with tempfile.TemporaryDirectory() as directory:
            args = [
                "--output",
                directory,
                "--anomaly",
                "wrong_checksum",
                "--allow-file-faults",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                GENERATOR.main(args)
                GENERATOR.main(
                    [
                        "--output",
                        directory,
                        "--force",
                        "--anomaly",
                        "missing_ready",
                        "--allow-file-faults",
                    ]
                )
            output = Path(directory)
            self.assertFalse((output / "csv/erp_products.csv.ready").exists())
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertTrue(manifest["diagnostics"]["intentional_file_faults"])
            self.assertEqual(
                manifest["diagnostics"]["anomalies"], ["missing_ready"]
            )

    def test_published_default_edge_payload_hashes_stay_unchanged(self):
        expected = {
            "csv/erp_products.csv": (
                "fce9ac37df64656ee16575c0f336d80a"
                "128f1cdbdbae369cfe22f0ebb0ab954e"
            ),
            "csv/erp_work_orders.csv": (
                "6388331c5dbc5da4cd1c2e393ac1a25b"
                "65f892738902049278eccb69e53ff4b1"
            ),
            "csv/erp_sales_orders.csv": (
                "74754cbc58ebc226c92c86ec411f28da"
                "852ce2665e106aaf9508dbf18b31958a"
            ),
            "csv/erp_shipments.csv": (
                "d27f27b6fef83c40909e631f34cdee34"
                "d7842c0b4ace00838c74380ec09a5ecf"
            ),
            "json/mes_events.jsonl": (
                "fd0eb482f07ef9ed3d81680a456d25a1"
                "fa9859f205b924c227583a79f4dafa5e"
            ),
            "json/mes_downtime.jsonl": (
                "2554e54ca02f178b485279bb05a4c96d"
                "38011ca5ec6392eba6328a4f48eb7d42"
            ),
        }
        actual = GENERATOR.encoded_sources(GENERATOR.build_data())
        self.assertEqual(
            {
                path: sha256(payload).hexdigest()
                for path, payload in actual.items()
            },
            expected,
        )


if __name__ == "__main__":
    unittest.main()
