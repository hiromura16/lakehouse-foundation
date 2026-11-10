"""Protect report export boundaries without rendering or service access."""

import importlib.util
import json
import locale
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts/report_export.py"
SPEC = importlib.util.spec_from_file_location("report_export_subject", MODULE)
EXPORT = importlib.util.module_from_spec(SPEC)
with mock.patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
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


class PortableMarkdownTests(unittest.TestCase):
    """Protect editable examples while adapting links in exported prose."""

    def test_plain_and_angle_links_are_portable(self):
        content = (
            "[local](./EVIDENCE-MATRIX.md#status)\n"
            "[guide](<../DOCUMENTATION-MANIFEST.md>)\n"
            "[spaced](<../example guide.md#section>)\n"
            "[web](<https://example.invalid/a b>) [anchor](#local)\n"
        )
        result = EXPORT.portable_markdown(
            content, EXPORT.REPORTS / "README.md"
        )
        public = (
            "https://github.com/hiromura16/lakehouse-foundation/blob/main/"
        )
        self.assertEqual(
            result,
            "[local](EVIDENCE-MATRIX.md#status)\n"
            + "[guide](<"
            + public
            + "docs/DOCUMENTATION-MANIFEST.md>)\n"
            + "[spaced](<"
            + public
            + "docs/example guide.md#section>)\n"
            + "[web](<https://example.invalid/a b>) [anchor](#local)\n",
        )

    def test_backtick_tilde_and_unclosed_fences_are_unchanged(self):
        # A shorter or different fence inside an example cannot end it.
        examples = [
            "```markdown\n[example](../../README.md)\n```\n",
            "~~~~markdown\r\n[example](<../../README.md>)\r\n"
            "```\r\n~~~\r\n~~~~~\r\n",
            "   ````markdown\n[example](../../README.md)\n   ```\n   ````\n",
            "~~~markdown\n[example](../../../outside.md)\n",
        ]
        for example in examples:
            with self.subTest(example=example):
                self.assertEqual(
                    EXPORT.portable_markdown(
                        example, EXPORT.REPORTS / "README.md"
                    ),
                    example,
                )

    def test_prose_after_a_closing_fence_is_rewritten(self):
        example = "~~~\n[example](../../README.md)\n~~~\n"
        result = EXPORT.portable_markdown(
            example + "[prose](./CAPTIONS.md)\n",
            EXPORT.REPORTS / "README.md",
        )
        self.assertEqual(result, example + "[prose](CAPTIONS.md)\n")


class ReportPreflightTests(unittest.TestCase):
    """Avoid redundant logo reads and parsing after export preflight."""

    def test_logo_allowlist_is_built_once_and_still_rejects_unknown_data(self):
        with mock.patch.object(
            sys, "path", [str(ROOT / "scripts"), *sys.path]
        ):
            import render_architecture

            EXPORT.allowed_logo_payloads.cache_clear()
            self.addCleanup(EXPORT.allowed_logo_payloads.cache_clear)
            with mock.patch.object(
                render_architecture, "logo", wraps=render_architecture.logo
            ) as logo:
                EXPORT.validate_report_svg('<svg width="1920"/>')
                EXPORT.validate_report_svg('<svg width="2400"/>')
                self.assertEqual(logo.call_count, 7)
                self.assertIsInstance(
                    EXPORT.allowed_logo_payloads(), frozenset
                )
                with self.assertRaises(ValueError):
                    EXPORT.validate_report_svg(
                        '<svg><image href="data:image/svg+xml;base64,AA"/>'
                        "</svg>"
                    )
                self.assertEqual(logo.call_count, 7)

    def test_pdf_uses_prevalidated_width_without_parsing_again(self):
        renderer, pdf = mock.MagicMock(), mock.MagicMock()
        renderer.svg2pdf.return_value = b"rendered-pdf-placeholder"
        original = pdf.PdfReader.return_value.pages[0]
        original.mediabox.width = 600
        original.mediabox.height = 400
        writer = pdf.PdfWriter.return_value
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                EXPORT,
                "validate_report_svg",
                side_effect=AssertionError("Unexpected second validation"),
            ):
                for width, paper in (
                    (1920, EXPORT.A4_LANDSCAPE_POINTS),
                    (2400, EXPORT.A3_LANDSCAPE_POINTS),
                ):
                    with self.subTest(width=width):
                        EXPORT.write_pdf(
                            "prevalidated SVG",
                            Path(directory) / "figure.pdf",
                            "Example",
                            "D:20261110000000Z",
                            renderer,
                            pdf,
                            svg_width_px=width,
                        )
                        writer.add_blank_page.assert_called_with(*paper)

    def test_bad_svg_width_does_not_create_an_export_directory(self):
        # Optional renderers are stubs: malformed input must stop before use.
        modules = {
            name: mock.MagicMock()
            for name in ("cairosvg", "pypdf", "PIL", "pptx")
        }
        data = {"figures": [{"id": "01-example"}]}
        key = EXPORT.REPORTS / "figures/01-example.svg"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new-export"
            with (
                mock.patch.dict(sys.modules, modules),
                mock.patch.object(EXPORT, "require_japanese_font"),
                mock.patch.object(
                    EXPORT, "allowed_logo_payloads", return_value=frozenset()
                ),
            ):
                for width in (None, "invalid", "nan", "-1"):
                    attribute = f' width="{width}"' if width else ""
                    with self.subTest(width=width):
                        with self.assertRaisesRegex(ValueError, "width"):
                            EXPORT.export_bundle(
                                data, {key: "<svg" + attribute + "/>"}, target
                            )
                        self.assertFalse(target.exists())


class ReportEncodingTests(unittest.TestCase):
    """Exercise every text export path with Japanese content and ASCII I/O."""

    def test_export_text_paths_are_utf8(self):
        if os.environ.get("REPORT_EXPECT_NON_UTF8") == "1":
            self.assertNotIn("utf", locale.getencoding().lower())
        data = {
            "reviewed_on": "2026-11-10",
            "reviewed_source_commit": "a" * 40,
            "figures": [
                {
                    "id": "01-example",
                    "title": "検証用の図",
                    "mermaid": "flowchart LR\n  A[検証]\n",
                }
            ],
        }
        svg = '<svg width="1920"><text>検証</text></svg>'
        reads, writes = Path.read_text, Path.write_text
        missing_encoding = []

        def read_text(path, *args, **kwargs):
            """Simulate an ASCII default even on a UTF-8 developer host."""
            if not args and "encoding" not in kwargs:
                missing_encoding.append(path.name)
                kwargs["encoding"] = "ascii"
            return reads(path, *args, **kwargs)

        def write_text(path, value, *args, **kwargs):
            """Fail on Japanese output if a writer omits its encoding."""
            if not args and "encoding" not in kwargs:
                missing_encoding.append(path.name)
                kwargs["encoding"] = "ascii"
            return writes(path, value, *args, **kwargs)

        modules = {
            name: mock.MagicMock() for name in ("cairosvg", "pypdf", "PIL")
        }
        modules["cairosvg"].svg2png.return_value = b"placeholder"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "export"
            target.mkdir()
            reports = Path(directory) / "source"
            reports.mkdir()
            for kind in ("svg", "png", "pdf", "mermaid"):
                (target / kind).mkdir()
            for name in (
                "README.md",
                "POC-REPORT-TEMPLATE.md",
                "EVALUATION-PLAN.md",
                "EVIDENCE-MATRIX.md",
                "PRESENTATION-OUTLINE.md",
                "DECISION-RECORD-TEMPLATE.md",
                "IMPLEMENTATION-INVENTORY.md",
                "CAPTIONS.md",
                "SCREENSHOT-GUIDE.md",
                "EDITING-GUIDE.md",
                "figure-index.csv",
                "figures.json",
                "requirements-export.txt",
                "requirements-export.lock",
            ):
                (reports / name).write_bytes(
                    "公開の検証素材\n".encode("utf-8")
                )
            (target / "evidence.txt").write_bytes(b"evidence")
            with (
                mock.patch.dict(sys.modules, modules),
                mock.patch.object(EXPORT, "REPORTS", reports),
                mock.patch.object(EXPORT, "write_pdf"),
                mock.patch.object(Path, "read_text", read_text),
                mock.patch.object(Path, "write_text", write_text),
            ):
                EXPORT.render_figure_exports(
                    data,
                    {"01-example": svg},
                    {"01-example": 1920},
                    target,
                    "D:20261110000000Z",
                )
                EXPORT.copy_portable_documents(
                    data, {reports / "gallery.html": "<p>公開図</p>"}, target
                )
                EXPORT.write_export_manifest(data, target)
            self.assertEqual(missing_encoding, [])
            self.assertEqual(
                (target / "svg/01-example.svg").read_bytes(),
                svg.encode("utf-8"),
            )
            manifest = json.loads(
                (target / "export-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["figure_count"], 1)
            self.assertEqual(
                (target / "README.md").read_text(encoding="utf-8"),
                "公開の検証素材\n",
            )

    def test_text_exports_run_with_a_real_non_utf8_default(self):
        environment = {
            **os.environ,
            "LC_ALL": "C",
            "PYTHONUTF8": "0",
            "PYTHONCOERCECLOCALE": "0",
            "PYTHONIOENCODING": "ascii",
            "REPORT_EXPECT_NON_UTF8": "1",
        }
        # Only the worker case runs in the child; this cannot recurse.
        result = subprocess.run(
            [
                sys.executable,
                "-X",
                "utf8=0",
                str(Path(__file__)),
                "ReportEncodingTests.test_export_text_paths_are_utf8",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(
            result.returncode,
            0,
            result.stderr.decode("ascii", errors="replace"),
        )


if __name__ == "__main__":
    unittest.main()
