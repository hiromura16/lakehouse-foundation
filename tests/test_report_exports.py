"""Protect report export boundaries without rendering or service access."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts/report_export.py"
SPEC = importlib.util.spec_from_file_location("report_export_subject", MODULE)
EXPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORT)


class ReportExportBoundaryTests(unittest.TestCase):
    """Refuse active figures, external resources and unsafe destinations."""

    def test_active_or_external_vectors_are_rejected(self):
        # No renderer should receive a URL, DTD or script from a report input.
        malicious = [
            '<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>',
            '<svg onload="run()"/>',
            '<svg><image href="https://example.invalid/image.svg"/></svg>',
            '<svg><image href="file:///private/credential"/></svg>',
            '<!DOCTYPE svg [<!ENTITY x "value">]><svg/>',
            '<svg><rect fill="url(https://example.invalid/pattern)"/></svg>',
        ]
        original = list(sys.path)
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            for value in malicious:
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        EXPORT.validate_report_svg(value)
        finally:
            sys.path[:] = original

    def test_existing_or_repository_destination_is_refused(self):
        with self.assertRaises(ValueError):
            EXPORT.external_destination(ROOT / "docs/reports/export")
        with self.assertRaises(ValueError):
            EXPORT.external_destination(Path("relative-export"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                EXPORT.external_destination(Path(directory))
            target = Path(directory) / "new-export"
            self.assertEqual(EXPORT.external_destination(target), target)
            self.assertFalse(target.exists())

    def test_symlink_parent_is_refused_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            (parent / "real").mkdir()
            (parent / "alias").symlink_to(parent / "real")
            with self.assertRaises(ValueError):
                EXPORT.external_destination(parent / "alias/export")
            self.assertFalse((parent / "real/export").exists())


if __name__ == "__main__":
    unittest.main()
