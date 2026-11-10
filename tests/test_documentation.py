"""Exercise document discovery, generated boundaries and local navigation.

All mutations use temporary Git fixtures. The public source documents and any
runtime configuration remain untouched by these tests.
"""

import contextlib
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "documentation", ROOT / "scripts/documentation.py"
)
DOCUMENTATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOCUMENTATION)


def entry(path, title):
    """Return one valid editorial record with explicit public relationships."""
    return {
        "path": path,
        "title": title,
        "purpose": "Explain the fixture's role.",
        "audience": ["Reader"],
        "when_to_use": "Before checking the fixture.",
        "category": "Guides",
        "prerequisites": [],
        "next": [],
        "status": "active",
    }


class DocumentationTests(unittest.TestCase):
    """Build isolated repositories so discovery follows actual Git rules."""

    def setUp(self):
        """Prepare two documents and an independently generated manifest."""
        temporary = tempfile.TemporaryDirectory(prefix="documentation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "docs").mkdir()
        self.write("README.md", "# Start\n\nKeep this body.\n")
        self.write("docs/guide.md", "# Guide\n\n[Start](../README.md)\n")
        self.index = {
            "README.md": entry("README.md", "Start"),
            "docs/guide.md": entry("docs/guide.md", "Guide"),
            DOCUMENTATION.MANIFEST: entry(
                DOCUMENTATION.MANIFEST, "Document manifest"
            ),
        }
        self.index["README.md"]["next"] = ["docs/guide.md"]
        self.index["docs/guide.md"]["prerequisites"] = ["README.md"]
        self.save_registry()
        self.write(
            DOCUMENTATION.MANIFEST,
            DOCUMENTATION.render_manifest(self.index),
        )
        subprocess.run(
            ["git", "init", "-q", str(self.root)],
            check=True,
            capture_output=True,
        )

    def write(self, path, text):
        """Write only a named fixture file, creating its parent as needed."""
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")

    def save_registry(self, documents=None):
        """Serialize a fixture registry; allow malformed records for tests."""
        self.write(
            DOCUMENTATION.REGISTRY,
            json.dumps(
                {
                    "schema_version": 1,
                    "documents": list(self.index.values())
                    if documents is None
                    else documents,
                }
            ),
        )

    def generate(self):
        """Generate the fixture's purpose blocks before an acceptance check."""
        DOCUMENTATION.update_documents(self.root, self.index)

    def test_generation_preserves_body_and_is_idempotent(self):
        """Insertion and later role updates leave all outside text intact."""
        original = (
            '<p id="banner">Banner</p>\n\n# Start\n\n'
            "Keep **formatting**, 日本語, and trailing spaces.  \n\n"
            "```markdown\n[Example](missing.md)\n```\n"
        )
        output = DOCUMENTATION.with_purpose(original, "README.md", self.index)
        block = DOCUMENTATION.purpose_block("README.md", self.index)
        self.assertEqual(output.replace("\n" + block + "\n", ""), original)
        self.assertEqual(
            DOCUMENTATION.with_purpose(output, "README.md", self.index),
            output,
        )
        changed = copy.deepcopy(self.index)
        changed["README.md"]["purpose"] = "A revised specific role."
        revised = DOCUMENTATION.with_purpose(output, "README.md", changed)
        self.assertEqual(
            revised.split(DOCUMENTATION.START)[0],
            output.split(DOCUMENTATION.START)[0],
        )
        self.assertEqual(
            revised.split(DOCUMENTATION.END)[1],
            output.split(DOCUMENTATION.END)[1],
        )
        self.assertIn("A revised specific role.", revised)

    def test_purpose_uses_relative_prerequisite_and_next_links(self):
        """The common API links correctly from nested generated documents."""
        with mock.patch.object(
            DOCUMENTATION,
            "load_index",
            return_value=self.index,
        ):
            block = DOCUMENTATION.purpose_block("docs/guide.md")
        self.assertIn("[Start](<../README.md>)", block)
        self.assertIn("(<DOCUMENTATION-MANIFEST.md>)", block)
        self.assertTrue(block.startswith(DOCUMENTATION.START))
        self.assertTrue(block.endswith(DOCUMENTATION.END))

    def test_existing_markers_must_be_unique_and_follow_first_h1(self):
        """Damaged, duplicate and displaced blocks are never overwritten."""
        block = DOCUMENTATION.purpose_block("README.md", self.index)
        malformed = [
            "# Start\n" + DOCUMENTATION.START,
            "# Start\n" + block + "\n" + block,
            block + "\n# Start\n",
            "# Start\n\nActual body.\n" + block,
            "# Start\n" + DOCUMENTATION.END + DOCUMENTATION.START,
        ]
        for text in malformed:
            with self.subTest(text=text[:25]), self.assertRaises(ValueError):
                DOCUMENTATION.with_purpose(text, "README.md", self.index)

    def test_discovery_respects_ignore_and_is_independent_of_cwd(self):
        """Include untracked MD while excluding ignored local notes."""
        self.write(".gitignore", "ignored/\n")
        self.write("ignored/private.md", "# Local only\n")
        self.write("docs/untracked note.md", "# New note\n")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "README.md"],
            check=True,
        )
        previous = Path.cwd()
        try:
            os.chdir(self.root.parent)
            paths = DOCUMENTATION.markdown_paths(self.root)
        finally:
            os.chdir(previous)
        self.assertIn("README.md", paths)
        self.assertEqual(paths.count("README.md"), 1)
        self.assertIn("docs/untracked note.md", paths)
        self.assertNotIn("ignored/private.md", paths)

    def test_empty_checkout_discovers_no_phantom_document(self):
        """Empty Git output is an empty list rather than an empty path."""
        with tempfile.TemporaryDirectory(prefix="empty-docs-") as value:
            directory = Path(value)
            subprocess.run(
                ["git", "init", "-q", str(directory)],
                check=True,
                capture_output=True,
            )
            self.assertEqual(DOCUMENTATION.markdown_paths(directory), [])

    def test_first_h1_ignores_fenced_example_titles(self):
        """Example headings cannot move the purpose block into a code fence."""
        text = "```markdown\n# Example\n```\n\n# Start\n\nBody.\n"
        result = DOCUMENTATION.with_purpose(text, "README.md", self.index)
        self.assertIn(
            "# Start\n\n" + DOCUMENTATION.START,
            result,
        )
        self.assertEqual(DOCUMENTATION.first_heading(text)[0], "Start")

    def test_coverage_detects_missing_and_unregistered_markdown(self):
        """Both directions of a stale registry produce actionable errors."""
        self.generate()
        self.write("docs/new.md", "# New\n")
        (self.root / "docs/guide.md").unlink()
        errors = DOCUMENTATION.check_documents(self.root, self.index)
        self.assertIn("Unregistered Markdown: docs/new.md", errors)
        self.assertIn(
            "Registered Markdown is missing: docs/guide.md",
            errors,
        )

    def test_generation_preflights_all_records_before_writing(self):
        """A later invalid file must not leave an earlier file regenerated."""
        before = (self.root / "README.md").read_bytes()
        self.write("docs/guide.md", "# Wrong title\n")
        with self.assertRaisesRegex(ValueError, "title differs"):
            DOCUMENTATION.update_documents(self.root, self.index)
        self.assertEqual((self.root / "README.md").read_bytes(), before)

    def test_registry_rejects_duplicate_paths_and_unregistered_relations(self):
        """Require one entry per document and registered navigation paths."""
        records = list(self.index.values())
        self.save_registry(records + [records[0]])
        with self.assertRaisesRegex(ValueError, "Duplicate document"):
            DOCUMENTATION.load_index(self.root)
        changed = copy.deepcopy(records)
        changed[0]["next"] = ["docs/not-registered.md"]
        self.save_registry(changed)
        with self.assertRaisesRegex(ValueError, "unregistered next"):
            DOCUMENTATION.load_index(self.root)

    def test_registry_rejects_invalid_paths_status_and_missing_fields(self):
        """Malformed metadata cannot generate unintended public file edits."""
        for field, value in (
            ("path", "../outside.md"),
            ("path", "/absolute.md"),
            ("status", "deployed"),
            ("audience", []),
            ("title", ""),
        ):
            changed = copy.deepcopy(list(self.index.values()))
            changed[0][field] = value
            self.save_registry(changed)
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    DOCUMENTATION.load_index(self.root)
        changed = copy.deepcopy(list(self.index.values()))
        del changed[0]["purpose"]
        self.save_registry(changed)
        with self.assertRaisesRegex(ValueError, "missing fields"):
            DOCUMENTATION.load_index(self.root)

    def test_registry_root_must_be_an_object(self):
        """Reject array, null and scalar JSON as ordinary input errors."""
        for value in ([], None, "text", 1):
            self.write(DOCUMENTATION.REGISTRY, json.dumps(value))
            with self.subTest(value=value), self.assertRaises(ValueError):
                DOCUMENTATION.load_index(self.root)

    def test_check_detects_stale_block_title_and_manifest_without_writes(self):
        """Metadata drift and hand-edited generated content fail read-only."""
        self.generate()
        self.write(
            "README.md",
            (self.root / "README.md")
            .read_text()
            .replace(
                "# Start",
                "# Renamed",
                1,
            ),
        )
        self.index["docs/guide.md"]["purpose"] = "Updated role."
        self.write(DOCUMENTATION.MANIFEST, "# Document manifest\n")
        before = {name: (self.root / name).read_bytes() for name in self.index}
        errors = DOCUMENTATION.check_documents(self.root, self.index)
        self.assertTrue(any("title differs" in error for error in errors))
        self.assertTrue(any("missing or stale" in error for error in errors))
        self.assertTrue(any("generated manifest" in error for error in errors))
        self.assertEqual(
            before,
            {name: (self.root / name).read_bytes() for name in self.index},
        )

    def test_gfm_duplicate_headings_and_unicode_anchors(self):
        """Duplicate and explicit IDs work alongside punctuation and CJK."""
        self.write(
            "docs/guide.md",
            (
                "# Guide\n## Same heading\n## Same heading\n"
                "## Same heading-1\n## 日本語：見出し！\n"
                '<a id="explicit"></a>\n'
            ),
        )
        self.write(
            "README.md",
            (
                "# Start\n[First](docs/guide.md#same-heading)\n"
                "[Second](docs/guide.md#same-heading-1)\n"
                "[Collision](docs/guide.md#same-heading-1-1)\n"
                "[日本語](docs/guide.md#%E6%97%A5%E6%9C%AC%E8%AA%9E"
                "%E8%A6%8B%E5%87%BA%E3%81%97)\n"
                "[Explicit](docs/guide.md#explicit)\n"
            ),
        )
        self.assertEqual(
            DOCUMENTATION.check_links(
                self.root,
                ["README.md"],
            ),
            [],
        )
        with (self.root / "README.md").open("a") as stream:
            stream.write("[Bad](docs/guide.md#absent)\n")
        self.assertIn(
            "local anchor not found.",
            DOCUMENTATION.check_links(
                self.root,
                ["README.md"],
            )[0],
        )

    def test_html_references_images_and_fenced_examples(self):
        """Check HTML and images while ignoring fenced examples."""
        self.write("docs/picture.svg", '<svg id="diagram"></svg>')
        self.write(
            "README.md",
            (
                '# Start\n<a href="docs/guide.md#guide">Guide</a>\n'
                '<img src="docs/picture.svg#diagram">\n'
                "![Image](docs/picture.svg)\n"
                "```markdown\n[Ignore](missing.md)\n"
                '<img src="also-missing.svg">\n```\n'
                '~~~html\n<a href="missing.html">No</a>\n~~~\n'
            ),
        )
        self.assertEqual(
            DOCUMENTATION.check_links(
                self.root,
                ["README.md"],
            ),
            [],
        )
        self.write("README.md", '# Start\n<img src="missing.svg">\n')
        self.assertEqual(
            DOCUMENTATION.check_links(
                self.root,
                ["README.md"],
            ),
            ["README.md:2: local link target not found."],
        )

    def test_reference_links_spaces_and_balanced_parentheses(self):
        """Common Markdown destinations keep spaces and nested parentheses."""
        self.write("docs/space name.md", "# Space\n")
        self.write("docs/file(1).md", "# Number\n")
        self.write(
            "README.md",
            (
                "# Start\n[Space](<docs/space name.md>)\n"
                "[Number](docs/file(1).md)\n[Ref][guide]\n"
                "[Root relative](/docs/guide.md#guide)\n"
                "[guide]: docs/guide.md#guide\n"
            ),
        )
        self.assertEqual(
            DOCUMENTATION.check_links(
                self.root,
                ["README.md"],
            ),
            [],
        )

    def test_heading_formatting_and_identifier_underscores(self):
        """GitHub heading text removes emphasis while preserving code IDs."""
        self.assertEqual(
            DOCUMENTATION.heading_slug(
                "This'll be a _Helpful_ Section About the Greek Letter Θ!"
            ),
            "thisll-be-a-helpful-section-about-the-greek-letter-θ",
        )
        self.assertEqual(
            DOCUMENTATION.heading_slug("Use `a_b` and __bold__"),
            "use-a_b-and-bold",
        )

    def test_local_escape_and_symlink_targets_are_rejected(self):
        """Traversal and symlink escapes fail even when their target exists."""
        with tempfile.TemporaryDirectory(prefix="document-outside-") as value:
            outside = Path(value) / "outside.md"
            outside.write_text("# Outside\n")
            (self.root / "docs/linked.md").symlink_to(outside)
            self.write(
                "README.md",
                (
                    "# Start\n[Traversal](../outside.md)\n"
                    "[Encoded](%2e%2e/outside.md)\n"
                    "[Symlink](docs/linked.md#outside)\n"
                ),
            )
            errors = DOCUMENTATION.check_links(self.root, ["README.md"])
            self.assertEqual(len(errors), 3)
            self.assertTrue(
                all("escapes repository" in error for error in errors)
            )

    def test_directory_anchor_and_document_symlinks_cannot_escape(self):
        """A directory's README and a source MD obey the same root boundary."""
        with tempfile.TemporaryDirectory(prefix="outside-anchor-") as value:
            outside = Path(value) / "outside.md"
            outside.write_text("# Outside\n")
            (self.root / "docs/subdir").mkdir()
            (self.root / "docs/subdir/README.md").symlink_to(outside)
            (self.root / "docs/source.md").symlink_to(outside)
            self.write("README.md", "# Start\n[Dir](docs/subdir/#outside)\n")
            errors = DOCUMENTATION.check_links(
                self.root,
                ["README.md", "docs/source.md"],
            )
            self.assertEqual(len(errors), 2)
            self.assertTrue(
                all("escapes repository" in error for error in errors)
            )

    def test_cli_from_other_cwd_generates_then_checks_without_mutation(self):
        """The real entry point succeeds from outside the target checkout."""
        command = [
            sys.executable,
            str(ROOT / "scripts/documentation.py"),
            "--root",
            str(self.root),
        ]
        generated = subprocess.run(
            command,
            cwd=self.root.parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(generated.returncode, 0, generated.stdout)
        before = {
            name: (self.root / name).stat().st_mtime_ns for name in self.index
        }
        checked = subprocess.run(
            command + ["--check"],
            cwd=self.root.parent,
            capture_output=True,
            text=True,
        )
        self.assertEqual(checked.returncode, 0, checked.stdout)
        self.assertIn("3 Markdown files", checked.stdout)
        self.assertEqual(
            before,
            {
                name: (self.root / name).stat().st_mtime_ns
                for name in self.index
            },
        )

    def test_cli_invalid_registry_does_not_traceback_or_reveal_root(self):
        """Invalid input yields a concise public diagnostic with no OS path."""
        self.write(DOCUMENTATION.REGISTRY, "{invalid}")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = DOCUMENTATION.main(["--root", str(self.root), "--check"])
        self.assertEqual(result, 1)
        self.assertNotIn("Traceback", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_generated_manifest_has_complete_category_and_reader_views(self):
        """The generated index contains actual links and editorial rules."""
        rendered = DOCUMENTATION.render_manifest(self.index)
        self.assertIn("### Guides", rendered)
        self.assertIn("### Reader", rendered)
        self.assertIn("[Guide](<guide.md>)", rendered)
        self.assertIn("scripts/documentation.py --check", rendered)
        self.generate()
        self.assertEqual(
            DOCUMENTATION.check_documents(self.root, self.index),
            [],
        )


if __name__ == "__main__":
    unittest.main()
