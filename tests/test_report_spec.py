"""Validate report specifications before generating or exporting any files."""

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MODULE = importlib.util.spec_from_file_location(
    "report_spec_subject", SCRIPTS / "render_report_assets.py"
)
REPORT = importlib.util.module_from_spec(MODULE)
with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    MODULE.loader.exec_module(REPORT)
BASELINE = json.loads(REPORT.SPEC.read_text(encoding="utf-8"))
# The service inventory deliberately has no routes. Add one valid test-only
# edge so required fields and malformed waypoint cases exercise the parser.
BASELINE["figures"][0]["edges"] = [
    {
        "source": BASELINE["figures"][0]["panels"][0]["key"],
        "target": BASELINE["figures"][0]["panels"][1]["key"],
        "label": "Test route",
        "kind": "data",
    }
]


class ReportSpecificationTests(unittest.TestCase):
    """Report the editable JSON path for missing, mistyped or invalid input."""

    def load(self, data):
        """Read a temporary specification through the production validator."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "figures.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with mock.patch.object(REPORT, "SPEC", path):
                return REPORT.load_spec()

    def test_every_required_field_has_an_actionable_error(self):
        locations = [
            (
                (),
                (
                    "schema_version",
                    "reviewed_on",
                    "reviewed_source_commit",
                    "figures",
                ),
            ),
            (
                ("figures", 0),
                (
                    "id",
                    "title",
                    "subtitle",
                    "caption",
                    "audience",
                    "width",
                    "height",
                    "panels",
                    "edges",
                    "notes",
                    "sources",
                    "mermaid",
                ),
            ),
            (
                ("figures", 0, "panels", 0),
                (
                    "key",
                    "x",
                    "y",
                    "width",
                    "height",
                    "tone",
                    "title",
                    "lines",
                    "tag",
                    "logo",
                ),
            ),
            (
                ("figures", 0, "edges", 0),
                ("source", "target", "label", "kind"),
            ),
        ]
        for route, fields in locations:
            for field in fields:
                data = copy.deepcopy(BASELINE)
                obj = data
                for key in route:
                    obj = obj[key]
                del obj[field]
                with self.subTest(route=route, field=field):
                    with self.assertRaisesRegex(
                        ValueError, rf"figures.json: .*\.{field}: Add"
                    ):
                        self.load(data)

    def test_types_and_numeric_boundaries_are_validated(self):
        cases = [
            (("schema_version",), True),
            (("reviewed_on",), None),
            (("reviewed_source_commit",), 123),
            (("figures",), {}),
            (("figures", 0), None),
            (("figures", 0, "width"), "2400"),
            (("figures", 0, "width"), True),
            (("figures", 0, "width"), float("inf")),
            (("figures", 0, "width"), 10**1000),
            (("figures", 0, "height"), 0),
            (("figures", 0, "notes"), "note"),
            (("figures", 0, "notes", 0), 0),
            (("figures", 0, "sources", 0), {}),
            (("figures", 0, "panels", 0), "panel"),
            (("figures", 0, "panels", 0, "x"), float("nan")),
            (("figures", 0, "panels", 0, "width"), -1),
            (("figures", 0, "panels", 0, "lines"), {}),
            (("figures", 0, "panels", 0, "lines", 0), None),
            (("figures", 0, "edges", 0), []),
            (("figures", 0, "edges", 0, "kind"), False),
            (("figures", 0, "edges", 0, "points"), None),
            (("figures", 0, "edges", 0, "points"), [[1, 2]]),
            (("figures", 0, "edges", 0, "points"), [[1, 2], [3]]),
            (("figures", 0, "edges", 0, "points"), [[1, 2], [3, "4"]]),
        ]
        for route, value in cases:
            data = copy.deepcopy(BASELINE)
            obj = data
            for key in route[:-1]:
                obj = obj[key]
            obj[route[-1]] = value
            with self.subTest(route=route, value=value):
                with self.assertRaisesRegex(ValueError, "figures.json: "):
                    self.load(data)
        for value in (None, [], "not an object"):
            with self.subTest(root=value):
                with self.assertRaisesRegex(ValueError, "JSON object"):
                    self.load(value)

    def test_calendar_date_and_commit_are_checked_before_generation(self):
        for value in ("2026-02-30", "20261110", "2026-1-1", "invalid"):
            data = copy.deepcopy(BASELINE)
            data["reviewed_on"] = value
            with self.subTest(date=value):
                with self.assertRaisesRegex(ValueError, "reviewed_on"):
                    self.load(data)
        data = copy.deepcopy(BASELINE)
        data["reviewed_source_commit"] = "main"
        with self.assertRaisesRegex(ValueError, "reviewed_source_commit"):
            self.load(data)
        data["reviewed_source_commit"] = "a" * 40
        data["reviewed_on"] = "2024-02-29"
        self.assertEqual(self.load(data)["reviewed_on"], "2024-02-29")

    def test_cross_references_and_style_names_are_validated(self):
        cases = [
            (("figures", 0, "id"), "invalid"),
            (("figures", 0, "sources", 0), "../outside"),
            (("figures", 0, "sources", 0), "missing-public-source"),
            (("figures", 0, "panels", 0, "key"), ""),
            (("figures", 0, "panels", 0, "x"), 0),
            (("figures", 0, "panels", 0, "tone"), "unknown"),
            (("figures", 0, "panels", 0, "logo"), "unknown"),
            (("figures", 0, "edges", 0, "source"), "unknown"),
            (("figures", 0, "edges", 0, "kind"), "unknown"),
        ]
        for route, value in cases:
            data = copy.deepcopy(BASELINE)
            obj = data
            for key in route[:-1]:
                obj = obj[key]
            obj[route[-1]] = value
            with self.subTest(route=route):
                with self.assertRaises(ValueError):
                    self.load(data)
        for duplicate in ("figures", "panels"):
            data = copy.deepcopy(BASELINE)
            items = (
                data["figures"]
                if duplicate == "figures"
                else (data["figures"][0]["panels"])
            )
            items.append(copy.deepcopy(items[0]))
            with self.subTest(duplicate=duplicate):
                with self.assertRaisesRegex(ValueError, "unique"):
                    self.load(data)

    def test_malformed_json_reports_the_line_and_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "figures.json"
            path.write_text('{"figures": [}', encoding="utf-8")
            with mock.patch.object(REPORT, "SPEC", path):
                with self.assertRaisesRegex(ValueError, "line 1, column"):
                    REPORT.load_spec()

    def test_cli_schema_errors_have_no_traceback_and_create_no_export(self):
        # A miniature checkout exercises the actual __main__ error handling.
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            scripts = checkout / "scripts"
            reports = checkout / "docs/reports"
            scripts.mkdir(parents=True)
            reports.mkdir(parents=True)
            # Include the renderer's public purpose API dependency too. Invalid
            # input must fail before that API reads the documentation registry.
            for filename in (
                "render_report_assets.py",
                "svg_primitives.py",
                "documentation.py",
            ):
                shutil.copyfile(SCRIPTS / filename, scripts / filename)
            target = Path(directory) / "new-export"
            invalid = [
                ({"schema_version": 1}, b"$.reviewed_on"),
                ({**BASELINE, "reviewed_on": "2026-02-30"}, b"$.reviewed_on"),
                ({**BASELINE, "figures": [None]}, b"$.figures[0]"),
            ]
            for data, expected_location in invalid:
                (reports / "figures.json").write_text(
                    json.dumps(data), encoding="utf-8"
                )
                result = subprocess.run(
                    [
                        sys.executable,
                        str(scripts / "render_report_assets.py"),
                        "--export-dir",
                        str(target),
                    ],
                    capture_output=True,
                    timeout=30,
                )
                with self.subTest(data_type=type(data).__name__):
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(b"ERROR:", result.stderr)
                    self.assertIn(expected_location, result.stderr)
                    self.assertNotIn(b"Traceback", result.stderr)
                    self.assertFalse(target.exists())

    def test_all_existing_svg_bytes_are_unchanged(self):
        with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            data = REPORT.load_spec()
            for figure in data["figures"]:
                with self.subTest(figure=figure["id"]):
                    path = REPORT.REPORTS / "figures" / (figure["id"] + ".svg")
                    self.assertEqual(
                        REPORT.render_svg(figure, data).encode("utf-8"),
                        path.read_bytes(),
                    )
            import render_architecture

            for filename, renderer in (
                ("architecture.svg", render_architecture.platform),
                ("ai-cloudflare.svg", render_architecture.hybrid),
            ):
                with self.subTest(figure=filename):
                    self.assertEqual(
                        (renderer() + "\n").encode("utf-8"),
                        (render_architecture.ASSETS / filename).read_bytes(),
                    )


if __name__ == "__main__":
    unittest.main()
