"""完了印から原本を取り出す境界を、標準ライブラリだけで確認する。"""

import base64
import json
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


class ExternalBatchTests(unittest.TestCase):
    """外部アプリのバッチ完了印を、実ファイルと照合する。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.landing = Path(tmp.name)
        self.run = self.landing / "run-01" / "job-01"
        self.mark = self.run / "manifests" / "source.ready.json"
        self.mark.parent.mkdir(parents=True)
        self.fixture = Path(__file__).resolve().parents[1] / "samples"
        self.artifacts = []
        for relative in (
            "csv/erp_products.csv",
            "json/mes_events.jsonl",
            "images/inspection.png",
        ):
            path = self.run / relative
            path.parent.mkdir(parents=True)
            fixture = self.fixture / "landing-contract" / relative
            if path.suffix == ".png":
                data = base64.b64decode(
                    fixture.with_suffix(".png.b64").read_bytes()
                )
            else:
                data = fixture.read_bytes()
            path.write_bytes(data)
            self.artifacts.append(
                {
                    "path": relative,
                    "bytes": len(data),
                    "sha256": sha256(data).hexdigest(),
                }
            )

    def publish(self):
        """外部producerと同じ版の完成印を最後に置く。"""
        self.mark.write_text(
            json.dumps(
                {
                    "schema_version": "source.file-manifest.v2",
                    "record_count": 2,
                    "artifacts": self.artifacts,
                }
            )
        )

    def test_csv_json_and_image_from_one_nested_run(self):
        self.assertEqual(list(ready_files(self.landing)), [])
        self.publish()
        files = list(ready_files(self.landing))
        self.assertEqual(
            [item.relative for item in files],
            ["run-01/job-01/" + item["path"] for item in self.artifacts],
        )
        self.assertEqual(
            [item.digest for item in files],
            [item["sha256"] for item in self.artifacts],
        )

    def test_invalid_last_payload_never_yields_any_of_batch(self):
        self.artifacts[-1]["sha256"] = "0" * 64
        self.publish()
        with self.assertRaisesRegex(ValueError, "checksum"):
            next(ready_files(self.landing))

    def test_unavailable_payload_never_yields_any_of_batch(self):
        (self.run / self.artifacts[-1]["path"]).unlink()
        self.publish()
        with self.assertRaisesRegex(ValueError, "missing or unreadable"):
            next(ready_files(self.landing))

    def test_validation_rejects_nonrelative_payload_paths(self):
        self.artifacts[-1]["path"] = "../inspection.png"
        self.publish()
        with self.assertRaisesRegex(ValueError, "artifact path"):
            next(ready_files(self.landing))

    def test_validation_rejects_symlinked_batch_subdirectory(self):
        folder = self.run / "images"
        moved = self.landing / "no-marker"
        folder.rename(moved)
        folder.symlink_to(moved, target_is_directory=True)
        self.publish()
        with self.assertRaisesRegex(ValueError, "missing or unreadable"):
            next(ready_files(self.landing))

    def test_validation_rejects_conflicting_artifact_entries(self):
        self.artifacts.append(self.artifacts[0])
        self.publish()
        with self.assertRaisesRegex(ValueError, "artifact metadata"):
            next(ready_files(self.landing))

    def test_rejection_does_not_block_next_valid_batch(self):
        self.publish()
        malformed = self.mark.with_name("a-invalid.ready.json")
        malformed.write_text("{incomplete")
        rejected = []
        observed = list(
            ready_files(
                self.landing, on_rejected_batch=lambda *v: rejected.append(v)
            )
        )
        self.assertEqual(len(observed), 3)
        marker_id = sha256(
            malformed.relative_to(self.landing).as_posix().encode()
        ).hexdigest()[:16]
        self.assertEqual(rejected, [(marker_id, "manifest_invalid")])
        self.assertEqual(malformed.read_text(), "{incomplete")

    def test_generic_decoder_value_error_does_not_block_next_batch(self):
        self.publish()
        malformed = self.mark.with_name("a-oversized-number.ready.json")
        # CPythonの整数変換上限でJSONDecodeErrorではないValueErrorになる。
        malformed.write_text('{"record_count":' + "9" * 5000 + "}")
        rejected = []
        files = list(
            ready_files(
                self.landing, on_rejected_batch=lambda *v: rejected.append(v)
            )
        )
        self.assertEqual(len(files), 3)
        self.assertEqual(
            [reason for _, reason in rejected], ["manifest_invalid"]
        )
