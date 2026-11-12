"""Exercise registry failure boundaries without network or daemon access."""

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cdc_images as images  # noqa: E402

TOP = "sha256:" + "a" * 64
CHILD = "sha256:" + "b" * 64
OTHER = "sha256:" + "c" * 64
REFERENCE = "example.invalid/public/image:1.0@" + TOP
MANIFEST = {"digest": CHILD, "mediaType": next(iter(images.MANIFEST_TYPES))}
INDEX = {
    "digest": TOP,
    "mediaType": next(iter(images.INDEX_TYPES)),
    "manifests": [
        {"digest": OTHER, "platform": {"os": "unknown"}},
        {
            "digest": CHILD,
            "platform": {
                "os": "linux",
                "architecture": "amd64",
            },
        },
    ],
}
CONFIG = {"os": "linux", "architecture": "amd64"}


class SourceTests(unittest.TestCase):
    """Keep generated references in sync and reject unmanaged inputs."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in (
            "infra/cdc/images.lock.json",
            "infra/cdc/compose.yaml",
            "infra/cdc/Dockerfile",
            "infra/lakehouse/images.lock.json",
            "infra/uv/images.lock.json",
        ):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(images.ROOT / name, target)

    def test_all_current_pins_match_compose_and_build_inputs(self):
        pins = images.load_images(self.root)
        images.compose_images(self.root, pins)
        self.assertEqual(set(pins), {*images.SERVICES, "python", "uv"})

    def test_generate_changes_only_the_image_and_is_idempotent(self):
        pins = images.load_images(self.root)
        original = (self.root / images.COMPOSE).read_text()
        pins["debezium"] = REFERENCE
        with self.assertRaisesRegex(images.ImageError, "COMPOSE_LOCK"):
            images.compose_images(self.root, pins)
        images.compose_images(self.root, pins, write=True)
        updated = (self.root / images.COMPOSE).read_text()
        before = images.load_images(self.root)["debezium"]
        self.assertEqual(updated, original.replace(before, REFERENCE))
        images.compose_images(self.root, pins, write=True)
        self.assertEqual((self.root / images.COMPOSE).read_text(), updated)

    def test_new_external_service_and_missing_lock_are_rejected(self):
        pins = images.load_images(self.root)
        path = self.root / images.COMPOSE
        path.write_text(
            path.read_text().replace(
                "services:\n",
                "services:\n  unmanaged:\n    image: " + REFERENCE + "\n",
            )
        )
        with self.assertRaisesRegex(images.ImageError, "COVERAGE"):
            images.compose_images(self.root, pins)
        path = self.root / "infra/cdc/images.lock.json"
        path.write_text("{}")
        with self.assertRaisesRegex(images.ImageError, "COVERAGE"):
            images.load_images(self.root)

    def test_new_build_input_is_not_silently_omitted(self):
        path = self.root / "infra/cdc/Dockerfile"
        original = path.read_text()
        for instruction in (
            "COPY --from=" + REFERENCE + " /x /x",
            "COPY --chown=0 --from=" + REFERENCE + " /x /x",
            "from " + REFERENCE + " AS additional",
            "COPY --chown=0 \\\n                --from="
            + REFERENCE
            + " /x /x",
        ):
            with self.subTest(instruction=instruction):
                path.write_text(original + "\n" + instruction + "\n")
                with self.assertRaisesRegex(
                    images.ImageError, "DOCKERFILE_LOCK"
                ):
                    images.load_images(self.root)

    def test_valid_build_input_case_and_flag_order_are_supported(self):
        pins = images.load_images(self.root)
        source = "from --platform=linux/amd64 " + pins["python"] + "\n"
        source += "copy --chown=0 --from=" + pins["uv"] + " /uv /bin/uv\n"
        (self.root / "infra/cdc/Dockerfile").write_text(source)
        self.assertEqual(images.load_images(self.root), pins)

    def test_unmanaged_local_tag_and_replaced_builder_are_rejected(self):
        pins = images.load_images(self.root)
        path = self.root / images.COMPOSE
        original = path.read_text()
        path.write_text(
            original.replace(
                "services:\n",
                "services:\n  unmanaged:\n"
                "    image: example.invalid/unmanaged:local\n",
            )
        )
        with self.assertRaisesRegex(images.ImageError, "COVERAGE"):
            images.compose_images(self.root, pins)
        path.write_text(
            original.replace(
                "${COMPOSE_PROJECT_NAME:-lakehouse}-cdc:local",
                REFERENCE,
            )
        )
        with self.assertRaisesRegex(images.ImageError, "LOCAL_IMAGE_CONTRACT"):
            images.compose_images(self.root, pins)

    def test_tag_only_digest_only_interpolated_and_malformed_pins_fail(self):
        for reference in (
            "apache/kafka:4.4.0",
            "apache/kafka@" + TOP,
            None,
            "${IMAGE}:1.0@" + TOP,
            "example.invalid/x:1.0@sha256:wrong",
        ):
            with self.subTest(reference=reference):
                with self.assertRaises(images.ImageError):
                    images.parse_pin(reference)

    def test_default_cli_is_offline_and_does_not_write(self):
        with (
            mock.patch.object(images, "ROOT", self.root),
            mock.patch.object(images, "docker") as docker,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(images.main([]), 0)
        docker.assert_not_called()


class CommandTests(unittest.TestCase):
    """Absence is explicit; transient failures retry and still fail closed."""

    def test_failure_classification_and_retry_policy(self):
        cases = {
            "ERROR: repo@sha256:abc: not found": "MANIFEST_MISSING",
            "manifest unknown": "MANIFEST_MISSING",
            "no matching manifest for linux/amd64": "PLATFORM_MISSING",
            "context deadline exceeded": "REGISTRY_TRANSIENT",
            "net/http: TLS handshake timeout": "REGISTRY_TRANSIENT",
            "429 Too Many Requests": "REGISTRY_TRANSIENT",
            "unexpected status 500": "REGISTRY_TRANSIENT",
            "502 Bad Gateway": "REGISTRY_TRANSIENT",
            "503 Service Unavailable": "REGISTRY_TRANSIENT",
            "504 Gateway Timeout": "REGISTRY_TRANSIENT",
            "Interrupted": "COMMAND_INTERRUPTED",
            "context canceled": "COMMAND_INTERRUPTED",
            "permission denied on docker.sock": "DOCKER_ACCESS_DENIED",
            "401 unauthorized": "REGISTRY_AUTH_FAILED",
            "unknown synthetic failure": "REGISTRY_COMMAND_FAILED",
            "timeout fetching manifest: not found": "REGISTRY_TRANSIENT",
        }
        for message, kind in cases.items():
            with self.subTest(message=message):
                result = subprocess.CompletedProcess([], 1, "", message)
                with (
                    mock.patch.object(
                        images.subprocess, "run", return_value=result
                    ) as run,
                    mock.patch.object(images.time, "sleep") as sleep,
                ):
                    with self.assertRaisesRegex(images.ImageError, kind):
                        images.docker(["buildx", "imagetools"])
                    transient = kind == "REGISTRY_TRANSIENT"
                    self.assertEqual(run.call_count, 3 if transient else 1)
                    self.assertEqual(sleep.call_count, 2 if transient else 0)

    def test_timeout_can_recover_and_signal_never_means_absence(self):
        with (
            mock.patch.object(
                images.subprocess,
                "run",
                side_effect=[
                    subprocess.TimeoutExpired("docker", 60),
                    subprocess.CompletedProcess([], 0, "ok", ""),
                ],
            ) as run,
            mock.patch.object(images.time, "sleep"),
        ):
            self.assertEqual(images.docker(["buildx"]), "ok")
            self.assertEqual(run.call_count, 2)
        self.assertEqual(
            images.failure_kind("not found", -15), "COMMAND_INTERRUPTED"
        )

    def test_timeout_exhaustion_and_invalid_json_fail(self):
        with (
            mock.patch.object(
                images.subprocess,
                "run",
                side_effect=(subprocess.TimeoutExpired("docker", 60)),
            ) as run,
            mock.patch.object(images.time, "sleep"),
        ):
            with self.assertRaisesRegex(images.ImageError, "TRANSIENT"):
                images.docker(["buildx"])
            self.assertEqual(run.call_count, 3)
        for value in ("[]", "null", "not-json"):
            with mock.patch.object(images, "docker", return_value=value):
                with self.assertRaisesRegex(images.ImageError, "RESPONSE"):
                    images.docker_json([])


class RegistryTests(unittest.TestCase):
    """Prove immutable index and platform content, independently of tag."""

    def test_index_and_amd64_config_and_separate_digest_reads(self):
        with mock.patch.object(
            images,
            "inspect",
            side_effect=[
                INDEX,
                INDEX,
                MANIFEST,
                CONFIG,
            ],
        ) as inspect:
            result = images.validate_registry(REFERENCE)
        self.assertEqual(result["pinned_digest"], TOP)
        self.assertEqual(result["platform_digest"], CHILD)
        self.assertEqual(result["pull"], "not_requested")
        self.assertEqual(
            inspect.call_args_list,
            [
                mock.call("example.invalid/public/image:1.0"),
                mock.call("example.invalid/public/image@" + TOP),
                mock.call("example.invalid/public/image@" + CHILD),
                mock.call("example.invalid/public/image@" + CHILD, "Image"),
            ],
        )

    def test_existing_tag_does_not_hide_missing_digest_or_child(self):
        for prefix in ([INDEX], [INDEX, INDEX]):
            with mock.patch.object(
                images,
                "inspect",
                side_effect=[
                    *prefix,
                    images.ImageError("MANIFEST_MISSING"),
                ],
            ):
                with self.assertRaisesRegex(
                    images.ImageError, "MANIFEST_MISSING"
                ):
                    images.validate_registry(REFERENCE)

    def test_mutable_tag_pin_mismatch_platform_and_child_errors(self):
        cases = [
            ([{**INDEX, "digest": OTHER}, INDEX], "TAG_DIGEST"),
            ([INDEX, {**INDEX, "digest": OTHER}], "PIN_DIGEST"),
            ([INDEX, {**INDEX, "manifests": []}], "PLATFORM_MISSING"),
            (
                [INDEX, {**INDEX, "manifests": [INDEX["manifests"][1]] * 2}],
                "AMBIGUOUS",
            ),
            ([INDEX, INDEX, {**MANIFEST, "digest": OTHER}], "PLATFORM_DIGEST"),
            (
                [INDEX, INDEX, {**MANIFEST, "mediaType": "unknown"}],
                "MANIFEST_INVALID",
            ),
            (
                [INDEX, INDEX, MANIFEST, {**CONFIG, "architecture": "arm64"}],
                "CONFIG_MISMATCH",
            ),
            ([INDEX, {**INDEX, "mediaType": "unknown"}], "MEDIA_TYPE"),
        ]
        for responses, error in cases:
            with self.subTest(error=error):
                with mock.patch.object(
                    images, "inspect", side_effect=responses
                ):
                    with self.assertRaisesRegex(images.ImageError, error):
                        images.validate_registry(REFERENCE)

    def test_single_manifest_requires_amd64_config_too(self):
        manifest = {**MANIFEST, "digest": TOP}
        with mock.patch.object(
            images,
            "inspect",
            side_effect=[
                manifest,
                manifest,
                CONFIG,
            ],
        ):
            self.assertEqual(
                images.validate_registry(REFERENCE)["platform_digest"], TOP
            )

    def test_platform_pin_must_belong_to_the_current_tag_and_match_config(
        self,
    ):
        manifest = {**MANIFEST, "digest": TOP}
        tag = {
            **INDEX,
            "digest": OTHER,
            "manifests": [
                {"digest": TOP, "platform": CONFIG},
            ],
        }
        with mock.patch.object(
            images,
            "inspect",
            side_effect=[
                tag,
                manifest,
                CONFIG,
            ],
        ):
            result = images.validate_registry(REFERENCE)
        self.assertFalse(result["tag_matches_pin"])
        self.assertTrue(result["tag_contains_platform_pin"])
        self.assertEqual(result["pinned_digest"], TOP)
        self.assertEqual(result["platform_digest"], TOP)
        with mock.patch.object(
            images,
            "inspect",
            side_effect=[
                {**tag, "manifests": []},
                manifest,
            ],
        ):
            with self.assertRaisesRegex(images.ImageError, "TAG_DIGEST"):
                images.validate_registry(REFERENCE)

    def test_smoke_checks_actual_platform_and_full_repo_digest(self):
        reference = "apache/kafka:4.4.0@" + TOP
        image = {
            "Os": "linux",
            "Architecture": "amd64",
            "RepoDigests": [
                "docker.io/apache/kafka@" + TOP,
            ],
        }
        evidence = {"image": reference}
        with (
            mock.patch.object(images, "docker") as docker,
            mock.patch.object(images, "docker_json", return_value=image),
        ):
            images.pull_smoke(evidence)
            docker.assert_called_once_with(
                [
                    "pull",
                    "--platform",
                    "linux/amd64",
                    reference,
                ],
                timeout=300,
            )
        self.assertEqual(evidence["pull"], "passed")
        for change, error in (
            ({"Architecture": "arm64"}, "PLATFORM_MISMATCH"),
            ({"RepoDigests": []}, "REPO_DIGEST_MISMATCH"),
            ({"RepoDigests": ["other/image@" + TOP]}, "REPO_DIGEST_MISMATCH"),
            (
                {"RepoDigests": ["apache/kafka@" + CHILD]},
                "REPO_DIGEST_MISMATCH",
            ),
        ):
            with (
                mock.patch.object(images, "docker"),
                mock.patch.object(
                    images, "docker_json", return_value={**image, **change}
                ),
            ):
                with self.assertRaisesRegex(images.ImageError, error):
                    images.pull_smoke({"image": reference})

    def test_registry_failure_is_nonzero_and_keeps_sanitized_evidence(self):
        output = io.StringIO()
        with (
            mock.patch.object(
                images,
                "validate_registry",
                side_effect=(images.ImageError("REGISTRY_TRANSIENT")),
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(images.main(["--registry"]), 1)
        self.assertEqual(
            json.loads(output.getvalue())["error"], "REGISTRY_TRANSIENT"
        )


if __name__ == "__main__":
    unittest.main()
