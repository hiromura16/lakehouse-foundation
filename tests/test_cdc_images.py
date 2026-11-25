"""Exercise registry failure boundaries without network or daemon access."""

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
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
RELEASE = "quay.io/debezium/connect:3.7.1.Final"
VERSION_ENV = "DEBEZIUM_VERSION=3.7.1.Final"
RELEASE_CONFIG = {**CONFIG, "config": {"Env": [VERSION_ENV, "OTHER=x"]}}
IMAGE_ID = "sha256:" + "d" * 64


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

    def test_all_current_references_match_compose_and_build_inputs(self):
        pins = images.load_images(self.root)
        images.compose_images(self.root, pins)
        self.assertEqual(set(pins), {*images.SERVICES, "python", "uv"})
        self.assertEqual(pins["debezium"], RELEASE)
        for name in ("kafka", "kafbat", "python", "uv"):
            images.parse_pin(pins[name])

    def test_duplicate_lock_keys_fail_even_if_last_pin_is_valid(self):
        """CDC自身と共有ビルド入力のトップ・入れ子重複を拒否する。"""
        for scope, service in (
            ("cdc", "debezium"),
            ("lakehouse", "python"),
            ("uv", "uv"),
        ):
            path = self.root / f"infra/{scope}/images.lock.json"
            original = path.read_text()
            top = f'"{service}":'
            properties = (
                ('"tag":', '"policy":')
                if service == "debezium"
                else ('"pinned":',)
            )
            for member in (top, *properties):
                insertion = member + " null, "
                with self.subTest(scope=scope, member=member):
                    invalid = original.replace(member, insertion + member, 1)
                    self.assertNotEqual(original, invalid)
                    path.write_text(invalid)
                    with self.assertRaisesRegex(
                        images.ImageError, "CDC_LOCK_DUPLICATE_KEY"
                    ):
                        images.load_images(self.root)
            path.write_text(original)

    def test_lock_rejects_non_json_constants(self):
        """値を利用しない追加項目でも非標準JSON定数は拒否する。"""
        path = self.root / "infra/cdc/images.lock.json"
        path.write_text(path.read_text().replace("{", '{"extra": NaN,', 1))
        with self.assertRaisesRegex(
            images.ImageError, "CDC_LOCK_INVALID_JSON"
        ):
            images.load_images(self.root)

    def test_generate_changes_only_the_image_and_is_idempotent(self):
        path = self.root / images.COMPOSE
        canonical = path.read_text()
        stale = canonical.replace(RELEASE, RELEASE + "@" + TOP)
        self.assertNotEqual(stale, canonical)
        path.write_text(stale)
        with (
            mock.patch.object(images, "ROOT", self.root),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(images.main([]), 1)
            self.assertEqual(path.read_text(), stale)
            self.assertEqual(images.main(["--write-compose"]), 0)
            self.assertEqual(path.read_text(), canonical)
            self.assertEqual(images.main(["--write-compose"]), 0)
            self.assertEqual(path.read_text(), canonical)
            self.assertEqual(images.main([]), 0)

    def test_only_exact_debezium_tag_and_policy_are_accepted(self):
        path = self.root / "infra/cdc/images.lock.json"
        correct = json.loads(path.read_text())
        for entry in (
            {"tag": RELEASE, "policy": "pinned"},
            {"tag": RELEASE},
            {"pinned": RELEASE + "@" + TOP},
            {"tag": RELEASE, "policy": "release-tag", "digest": TOP},
            {"tag": RELEASE + "@" + TOP, "policy": "release-tag"},
            {
                "tag": RELEASE.replace("quay.io", "example.invalid"),
                "policy": "release-tag",
            },
            *(
                {
                    "tag": RELEASE.rsplit(":", 1)[0] + ":" + tag,
                    "policy": "release-tag",
                }
                for tag in (
                    "latest",
                    "3",
                    "3.7",
                    "3.7.1",
                    "3.7.1.CR1",
                    "3.7.2.Final",
                    "3.7.1.Final-extra",
                )
            ),
        ):
            with self.subTest(entry=entry):
                path.write_text(json.dumps({**correct, "debezium": entry}))
                with self.assertRaisesRegex(
                    images.ImageError, "DEBEZIUM_RELEASE_TAG_REQUIRED"
                ):
                    images.load_images(self.root)

    def test_immutable_inputs_do_not_accept_the_mutable_policy_or_release(
        self,
    ):
        for scope, name in (
            ("cdc", "kafka"),
            ("cdc", "kafbat"),
            ("lakehouse", "python"),
            ("uv", "uv"),
        ):
            path = self.root / f"infra/{scope}/images.lock.json"
            original = path.read_text()
            locks = json.loads(original)
            for change in (
                {"tag": RELEASE, "policy": "release-tag"},
                {"pinned": RELEASE},
                {"pinned": RELEASE + "@" + TOP},
                {"pinned": locks[name]["pinned"].split("@")[0]},
            ):
                with self.subTest(scope=scope, name=name, change=change):
                    path.write_text(json.dumps({**locks, name: change}))
                    with self.assertRaises((images.ImageError, KeyError)):
                        images.load_images(self.root)
            path.write_text(original)

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

    def test_pin_mismatch_platform_and_child_errors(self):
        cases = [
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

    def test_platform_pin_membership_and_config(
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
                {**tag, "manifests": INDEX["manifests"]},
                manifest,
                CONFIG,
            ],
        ):
            drift = images.validate_registry(REFERENCE)
        self.assertEqual(drift["warnings"], ["REGISTRY_TAG_DIGEST_MISMATCH"])
        self.assertFalse(drift["tag_contains_platform_pin"])

    def test_tag_drift_warns_and_pull_keeps_approved_reference(self):
        with mock.patch.object(
            images,
            "inspect",
            side_effect=[{**INDEX, "digest": OTHER}, INDEX, MANIFEST, CONFIG],
        ) as inspect:
            result = images.validate_registry(REFERENCE)
        self.assertEqual(result["warnings"], ["REGISTRY_TAG_DIGEST_MISMATCH"])
        self.assertEqual(result["image"], REFERENCE)
        self.assertEqual(result["pinned_digest"], TOP)
        self.assertFalse(result["tag_matches_pin"])
        self.assertNotIn(
            mock.call("example.invalid/public/image@" + OTHER),
            inspect.call_args_list,
        )
        with (
            mock.patch.object(images, "docker") as docker,
            mock.patch.object(
                images,
                "docker_json",
                return_value={
                    "Os": "linux",
                    "Architecture": "amd64",
                    "RepoDigests": ["example.invalid/public/image@" + TOP],
                },
            ),
        ):
            images.pull_smoke(result)
        self.assertEqual(docker.call_args.args[0][-1], REFERENCE)
        self.assertEqual(result["pull"], "passed")

    def test_tag_drift_does_not_hide_fixed_content_failures(self):
        tag = {**INDEX, "digest": OTHER}
        cases = [
            ([tag, images.ImageError("MANIFEST_MISSING")], "MANIFEST_MISSING"),
            (
                [tag, INDEX, images.ImageError("MANIFEST_MISSING")],
                "MANIFEST_MISSING",
            ),
            (
                [tag, INDEX, MANIFEST, {**CONFIG, "architecture": "arm64"}],
                "CONFIG_MISMATCH",
            ),
            ([tag, {**INDEX, "digest": OTHER}], "PIN_DIGEST_MISMATCH"),
            ([tag, {**INDEX, "manifests": []}], "PLATFORM_MISSING"),
        ]
        for responses, error in cases:
            with self.subTest(error=error):
                with mock.patch.object(
                    images, "inspect", side_effect=responses
                ):
                    with self.assertRaisesRegex(images.ImageError, error):
                        images.validate_registry(REFERENCE)

    def test_tag_read_failures_and_invalid_digest_still_fail(self):
        for response, error in (
            (images.ImageError("REGISTRY_TRANSIENT"), "REGISTRY_TRANSIENT"),
            (images.ImageError("MANIFEST_MISSING"), "MANIFEST_MISSING"),
            ({**INDEX, "digest": "invalid"}, "TAG_DIGEST_INVALID"),
        ):
            with mock.patch.object(
                images, "inspect", side_effect=[response, INDEX]
            ):
                with self.assertRaisesRegex(images.ImageError, error):
                    images.validate_registry(REFERENCE)

    def test_cli_exposes_warning_and_still_requires_pull_success(self):
        for failure in (None, images.ImageError("PULL_REPO_DIGEST_MISMATCH")):
            output, errors = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(
                    images, "load_images", return_value={"debezium": REFERENCE}
                ),
                mock.patch.object(images, "compose_images"),
                mock.patch.object(
                    images,
                    "inspect",
                    side_effect=[
                        {**INDEX, "digest": OTHER},
                        INDEX,
                        MANIFEST,
                        CONFIG,
                    ],
                ),
                mock.patch.object(
                    images, "pull_smoke", side_effect=failure
                ) as pull,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                self.assertEqual(
                    images.main(["--pull-smoke"]), 1 if failure else 0
                )
            self.assertEqual(pull.call_args.args[0]["image"], REFERENCE)
            self.assertIn(
                "WARNING: debezium: REGISTRY_TAG_DIGEST_MISMATCH",
                errors.getvalue(),
            )
            report = json.loads(output.getvalue())
            self.assertEqual(
                report["images"]["debezium"]["warnings"],
                ["REGISTRY_TAG_DIGEST_MISMATCH"],
            )
            if failure:
                self.assertEqual(report["error"], "PULL_REPO_DIGEST_MISMATCH")

    def test_malformed_drift_descriptor_is_not_a_warning(self):
        for tag in (
            {**INDEX, "digest": OTHER, "mediaType": "invalid"},
            {**INDEX, "digest": OTHER, "manifests": []},
            {**INDEX, "digest": OTHER, "manifests": [{}]},
        ):
            with mock.patch.object(
                images, "inspect", side_effect=[tag, INDEX]
            ):
                with self.assertRaisesRegex(images.ImageError, "DESCRIPTOR"):
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

    def test_release_manifest_uses_exact_bytes_and_strict_config_descriptor(
        self,
    ):
        media = "application/vnd.oci.image.manifest.v1+json"
        descriptor = {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": IMAGE_ID,
            "size": 1234,
        }
        manifest = {
            "schemaVersion": 2,
            "mediaType": media,
            "config": descriptor,
        }
        body = json.dumps(manifest, indent=2).replace("\n", "\r\n").encode()
        digest = "sha256:" + sha256(body).hexdigest()
        ref = "quay.io/debezium/connect@" + digest
        with mock.patch.object(images, "docker", return_value=body) as cli:
            self.assertEqual(
                images.release_config_digest(ref, digest, media), IMAGE_ID
            )
            cli.assert_called_once_with(
                ["buildx", "imagetools", "inspect", ref, "--raw"], binary=True
            )
        with (
            mock.patch.object(images, "docker", return_value=body + b"\n"),
            self.assertRaisesRegex(images.ImageError, "BYTES_MISMATCH"),
        ):
            images.release_config_digest(ref, digest, media)
        for bad, error in (
            (
                body.replace(b'"size":', b'"size": 1, "size":', 1),
                "MANIFEST_INVALID",
            ),
            (
                body.replace(b'"size": 1234', b'"size": NaN', 1),
                "MANIFEST_INVALID",
            ),
            (
                body.replace(b"1234", b"true", 1),
                "CONFIG_DESCRIPTOR_INVALID",
            ),
            (
                body.replace(b"1234", b"0", 1),
                "CONFIG_DESCRIPTOR_INVALID",
            ),
            (
                body.replace(IMAGE_ID.encode(), b"not-a-digest", 1),
                "CONFIG_DESCRIPTOR_INVALID",
            ),
            (
                body.replace(b"oci.image.config", b"oci.image.index", 1),
                "CONFIG_DESCRIPTOR_INVALID",
            ),
            (b"{", "MANIFEST_INVALID"),
        ):
            with (
                self.subTest(error=error),
                mock.patch.object(images, "docker", return_value=bad),
                self.assertRaisesRegex(images.ImageError, error),
            ):
                observed = "sha256:" + sha256(bad).hexdigest()
                images.release_config_digest(ref, observed, media)

    def test_debezium_release_reads_fresh_digest_and_exact_metadata(self):
        for index_digest in (TOP, OTHER):
            index = {**INDEX, "digest": index_digest}
            with (
                mock.patch.object(
                    images,
                    "inspect",
                    side_effect=[index, index, MANIFEST, RELEASE_CONFIG],
                ) as inspect,
                mock.patch.object(
                    images, "release_config_digest", return_value=IMAGE_ID
                ) as manifest,
            ):
                result = images.validate_registry(RELEASE)
            manifest.assert_called_once_with(
                RELEASE.rsplit(":", 1)[0] + "@" + CHILD,
                CHILD,
                MANIFEST["mediaType"],
            )
            self.assertEqual(result["observed_amd64_config_digest"], IMAGE_ID)
            self.assertEqual(result["observed_index_digest"], index_digest)
            self.assertEqual(result["policy"], "release-tag")
            self.assertEqual(result["platform_digest"], CHILD)
            self.assertEqual(result["debezium_version"], "3.7.1.Final")
            self.assertNotIn("pinned_digest", result)
            repo = RELEASE.rsplit(":", 1)[0]
            self.assertEqual(
                inspect.call_args_list,
                [
                    mock.call(RELEASE),
                    mock.call(repo + "@" + index_digest),
                    mock.call(repo + "@" + CHILD),
                    mock.call(repo + "@" + CHILD, "Image"),
                ],
            )

    def test_debezium_rejects_invalid_release_evidence(self):
        cases = (
            ([{**INDEX, "digest": "invalid"}], "TAG_DIGEST_INVALID"),
            ([{**MANIFEST, "digest": TOP}], "RELEASE_INDEX_REQUIRED"),
            ([INDEX, {**INDEX, "digest": OTHER}], "RELEASE_INDEX_MISMATCH"),
            ([INDEX, {**INDEX, "manifests": [{}]}], "RELEASE_INDEX_INVALID"),
            (
                [INDEX, {**INDEX, "manifests": [INDEX["manifests"][0]]}],
                "PLATFORM_MISSING",
            ),
            (
                [INDEX, {**INDEX, "manifests": [INDEX["manifests"][1]] * 2}],
                "PLATFORM_AMBIGUOUS",
            ),
            (
                [INDEX, INDEX, {**MANIFEST, "digest": OTHER}],
                "PLATFORM_DIGEST_MISMATCH",
            ),
            (
                [INDEX, INDEX, {**MANIFEST, "mediaType": "not-a-manifest"}],
                "PLATFORM_MANIFEST_INVALID",
            ),
            (
                [INDEX, INDEX, MANIFEST, {**RELEASE_CONFIG, "os": "windows"}],
                "PLATFORM_CONFIG_MISMATCH",
            ),
            ([INDEX, INDEX, MANIFEST, CONFIG], "VERSION_INVALID"),
            (
                [
                    INDEX,
                    INDEX,
                    MANIFEST,
                    {
                        **CONFIG,
                        "config": {"Env": ["DEBEZIUM_VERSION=3.7.2.Final"]},
                    },
                ],
                "VERSION_MISMATCH",
            ),
            (
                [
                    INDEX,
                    INDEX,
                    MANIFEST,
                    {**CONFIG, "config": {"Env": [VERSION_ENV, VERSION_ENV]}},
                ],
                "VERSION_MISMATCH",
            ),
        )
        for responses, error in cases:
            with (
                self.subTest(error=error),
                mock.patch.object(images, "inspect", side_effect=responses),
                mock.patch.object(
                    images, "release_config_digest", return_value=IMAGE_ID
                ),
                self.assertRaisesRegex(images.ImageError, error),
            ):
                images.validate_registry(RELEASE)

    def test_release_cli_checks_explicit_amd64_and_current_repo_digest(self):
        repo = RELEASE.rsplit(":", 1)[0]
        classic = {
            "Os": "linux",
            "Architecture": "amd64",
            "Id": IMAGE_ID,
            "RepoDigests": [repo + "@" + TOP, repo + "@" + OTHER],
            "Config": {"Env": [VERSION_ENV, "PRIVATE_FIXTURE=do-not-print"]},
        }
        root = {
            **classic,
            "Id": TOP,
            "Descriptor": {"digest": TOP, "mediaType": INDEX["mediaType"]},
        }
        child = {
            **classic,
            "Id": CHILD,
            "Descriptor": {
                "digest": CHILD,
                "mediaType": MANIFEST["mediaType"],
                "platform": CONFIG,
            },
        }
        no_platform = {
            **child,
            "Descriptor": {
                "digest": CHILD,
                "mediaType": MANIFEST["mediaType"],
            },
        }
        receipt = "Digest: " + TOP
        for result, local, selected, final, error, kind in (
            (receipt, classic, classic, INDEX, None, "config"),
            (receipt, root, child, INDEX, None, "manifest"),
            (receipt, root, no_platform, INDEX, None, "manifest"),
            (
                "Digest: " + OTHER,
                root,
                child,
                INDEX,
                "PULL_RELEASE_DIGEST_CHANGED",
                None,
            ),
            ("", root, child, INDEX, "PULL_RELEASE_DIGEST_CHANGED", None),
            (
                receipt + "\nDigest: " + OTHER,
                root,
                child,
                INDEX,
                "PULL_RELEASE_DIGEST_CHANGED",
                None,
            ),
            (
                receipt,
                root,
                child,
                {**INDEX, "digest": OTHER},
                "PULL_RELEASE_DIGEST_CHANGED",
                None,
            ),
            (
                receipt,
                {**root, "Id": "private-looking-bad"},
                child,
                INDEX,
                "PULL_RELEASE_ROOT_ID_INVALID",
                None,
            ),
            (
                receipt,
                {**root, "Id": OTHER},
                child,
                INDEX,
                "PULL_RELEASE_ROOT_ID_MISMATCH",
                None,
            ),
            (
                receipt,
                {**root, "Descriptor": None},
                child,
                INDEX,
                "PULL_RELEASE_ROOT_ID_MISMATCH",
                None,
            ),
            (
                receipt,
                {**root, "Architecture": "arm64"},
                child,
                INDEX,
                "PULL_PLATFORM_MISMATCH",
                None,
            ),
            (
                receipt,
                {**root, "RepoDigests": [repo + "@" + CHILD]},
                child,
                INDEX,
                "PULL_REPO_DIGEST_MISMATCH",
                None,
            ),
            (
                receipt,
                root,
                root,
                INDEX,
                "PULL_RELEASE_AMD64_ID_MISMATCH",
                None,
            ),
            (
                receipt,
                root,
                {**child, "Id": OTHER},
                INDEX,
                "PULL_RELEASE_AMD64_ID_MISMATCH",
                None,
            ),
            (
                receipt,
                root,
                {**child, "Id": "private-looking-bad"},
                INDEX,
                "PULL_RELEASE_AMD64_ID_INVALID",
                None,
            ),
            (
                receipt,
                root,
                {**child, "Descriptor": None},
                INDEX,
                "PULL_RELEASE_AMD64_ID_MISMATCH",
                None,
            ),
            (
                receipt,
                root,
                {
                    **child,
                    "Descriptor": {
                        **child["Descriptor"],
                        "digest": OTHER,
                    },
                },
                INDEX,
                "PULL_RELEASE_AMD64_ID_MISMATCH",
                None,
            ),
            (
                receipt,
                root,
                {
                    **child,
                    "Descriptor": {
                        **child["Descriptor"],
                        "mediaType": INDEX["mediaType"],
                    },
                },
                INDEX,
                "PULL_RELEASE_AMD64_ID_MISMATCH",
                None,
            ),
            (
                receipt,
                root,
                {
                    **child,
                    "Descriptor": {
                        **child["Descriptor"],
                        "platform": {"architecture": "arm64"},
                    },
                },
                INDEX,
                "PULL_RELEASE_AMD64_DESCRIPTOR_INVALID",
                None,
            ),
            (
                receipt,
                classic,
                {**classic, "Descriptor": child["Descriptor"]},
                INDEX,
                "PULL_RELEASE_AMD64_ID_MISMATCH",
                None,
            ),
            (
                receipt,
                root,
                {**child, "Architecture": "arm64"},
                INDEX,
                "PULL_RELEASE_AMD64_PLATFORM_MISMATCH",
                None,
            ),
            (
                receipt,
                root,
                {**child, "Config": {"Env": [VERSION_ENV] * 2}},
                INDEX,
                "PULL_RELEASE_VERSION_MISMATCH",
                None,
            ),
            (
                receipt,
                root,
                {**child, "Config": {"Env": []}},
                INDEX,
                "PULL_RELEASE_VERSION_MISMATCH",
                None,
            ),
            (
                receipt,
                root,
                images.ImageError("REGISTRY_COMMAND_FAILED"),
                INDEX,
                "PULL_RELEASE_AMD64_INSPECT_UNAVAILABLE",
                None,
            ),
        ):
            output = io.StringIO()
            with (
                self.subTest(error=error, kind=kind),
                mock.patch.object(
                    images, "load_images", return_value={"debezium": RELEASE}
                ),
                mock.patch.object(images, "compose_images"),
                mock.patch.object(
                    images,
                    "inspect",
                    side_effect=[
                        INDEX,
                        INDEX,
                        MANIFEST,
                        RELEASE_CONFIG,
                        final,
                    ],
                ),
                mock.patch.object(
                    images, "release_config_digest", return_value=IMAGE_ID
                ),
                mock.patch.object(
                    images, "docker", return_value=result
                ) as cli,
                mock.patch.object(
                    images, "docker_json", side_effect=[local, selected]
                ) as inspect_local,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(images.main(["--pull-smoke"]), bool(error))
                cli.assert_called_once_with(
                    ["pull", "--platform", "linux/amd64", RELEASE], timeout=300
                )
            self.assertNotIn("private-looking-bad", output.getvalue())
            self.assertNotIn("PRIVATE_FIXTURE", output.getvalue())
            report = json.loads(output.getvalue())
            evidence = report["images"]["debezium"]
            if error:
                self.assertEqual(report["error"], error)
                self.assertEqual(report["failed_image"]["phase"], "pull")
                self.assertEqual(evidence["pull"], "not_requested")
                self.assertNotIn("repo_digest", evidence)
                self.assertNotIn(OTHER, output.getvalue())
                if error == "PULL_RELEASE_ROOT_ID_MISMATCH":
                    if local["Id"] == OTHER:
                        self.assertEqual(
                            evidence["local_root_image_id_state"],
                            "unrecognized",
                        )
            else:
                self.assertEqual(
                    inspect_local.call_args_list,
                    [
                        mock.call(
                            [
                                "image",
                                "inspect",
                                RELEASE,
                                "--format",
                                "{{json .}}",
                            ]
                        ),
                        mock.call(
                            [
                                "image",
                                "inspect",
                                "--platform",
                                "linux/amd64",
                                RELEASE,
                                "--format",
                                "{{json .}}",
                            ]
                        ),
                    ],
                )
                self.assertEqual(evidence["repo_digest"], repo + "@" + TOP)
                self.assertEqual(evidence["local_root_image_id"], local["Id"])
                self.assertEqual(
                    evidence["local_amd64_image_id"], selected["Id"]
                )
                self.assertEqual(evidence["local_amd64_identity_kind"], kind)
                self.assertEqual(
                    evidence["observed_amd64_config_digest"], IMAGE_ID
                )
                self.assertEqual(evidence["pull"], "passed")

    def test_pull_smoke_requires_same_process_observation_contract(self):
        with (
            mock.patch.object(images, "docker") as cli,
            self.assertRaisesRegex(images.ImageError, "OBSERVATION_REQUIRED"),
        ):
            images.pull_smoke({"image": RELEASE, "observed_index_digest": TOP})
        cli.assert_not_called()

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

    def test_missing_pin_identifies_public_image_without_stderr(self):
        output = io.StringIO()
        with (
            mock.patch.object(
                images,
                "load_images",
                return_value={"debezium": REFERENCE},
            ),
            mock.patch.object(images, "compose_images"),
            mock.patch.object(
                images,
                "validate_registry",
                side_effect=images.ImageError("MANIFEST_MISSING"),
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(images.main(["--registry"]), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["error"], "MANIFEST_MISSING")
        self.assertEqual(
            result["failed_image"],
            {"name": "debezium", "image": REFERENCE, "phase": "registry"},
        )
        self.assertEqual(result["status"], "failed")

    def test_pull_failure_is_labeled_without_weakening_registry_checks(self):
        output = io.StringIO()
        with (
            mock.patch.object(
                images,
                "load_images",
                return_value={"debezium": REFERENCE},
            ),
            mock.patch.object(images, "compose_images"),
            mock.patch.object(
                images,
                "validate_registry",
                return_value={"image": REFERENCE, "registry": "passed"},
            ),
            mock.patch.object(
                images,
                "pull_smoke",
                side_effect=images.ImageError("DOCKER_ACCESS_DENIED"),
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(images.main(["--pull-smoke"]), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["failed_image"]["phase"], "pull")
        self.assertEqual(result["error"], "DOCKER_ACCESS_DENIED")
        self.assertEqual(result["images"]["debezium"]["registry"], "passed")


if __name__ == "__main__":
    unittest.main()
