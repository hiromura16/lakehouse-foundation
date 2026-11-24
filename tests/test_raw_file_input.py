"""完了印から原本を取り出す境界を、標準ライブラリだけで確認する。"""

import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from scripts.raw_file_input import RawFile, ready_files


class RawFileTests(unittest.TestCase):
    """原本の順序・境界と、完了印の拒否を固定する。"""

    def test_sorted_lazy_reads_and_sha_check(self):
        """後続の不一致は先行ファイルを返した後ではじめて検出する。"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name, content in (("b.csv", b"later"), ("a.csv", b"earlier")):
                path = directory / name
                path.write_bytes(content)
                expected = (
                    sha256(content).hexdigest()
                    if name == "a.csv"
                    else ("0" * 64)
                )
                path.with_name(name + ".ready").write_text(expected + "\n")
            files = ready_files(directory)
            first = next(files)
            self.assertEqual(
                first,
                RawFile(
                    directory / "a.csv",
                    "a.csv",
                    b"earlier",
                    sha256(b"earlier").hexdigest(),
                ),
            )
            with self.assertRaisesRegex(
                ValueError, "does not match its completion marker"
            ):
                next(files)

    def test_no_marker_missing_source_and_symlinks_are_not_input(self):
        """従来対象外の着地ファイルや直接 symlink を引き続き無視する。"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            payload = directory / "never-ready.csv"
            payload.write_bytes(b"ignored")
            marker = directory / "missing.csv.ready"
            marker.write_text(sha256(b"ignored").hexdigest() + "\n")
            (directory / "linked.csv").symlink_to(payload)
            (directory / "linked.csv.ready").write_bytes(marker.read_bytes())
            (directory / "marker-link.csv").write_bytes(payload.read_bytes())
            (directory / "marker-link.csv.ready").symlink_to(marker)
            self.assertEqual(list(ready_files(directory)), [])
