"""Check the optional lab using synthetic responses, never a live service."""

import base64
import http.client
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "openobserve_lab_subject", ROOT / "scripts/openobserve_lab.py"
)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)


def settings():
    """Return synthetic secrets that are unrelated to any deployed service."""
    return {
        "OPENOBSERVE_PORT": "5080",
        "OPENOBSERVE_ROOT_USER_EMAIL": "operator@example.invalid",
        "OPENOBSERVE_ROOT_USER_PASSWORD": "synthetic-bootstrap-" + "x" * 40,
    }


class Response(io.BytesIO):
    """Provide a bounded JSON response with an explicit HTTP status."""

    status = 200

    def __init__(self, body):
        super().__init__(json.dumps(body).encode())


class Requests:
    """Capture requests while returning controlled in-memory responses."""

    def __init__(self, body):
        self.body = body
        self.requests = []

    def open(self, request, timeout):
        """Record the route and body without performing network I/O."""
        self.requests.append(request)
        return Response(self.body)


class LabConfigurationTests(unittest.TestCase):
    """Keep the optional target separate from ordinary monitoring and data."""

    def test_disabled_plan_needs_no_secret_and_contacts_nothing(self):
        with mock.patch.object(LAB.urllib.request, "build_opener") as opener:
            self.assertEqual(
                LAB.plan({"COMPOSE_PROFILES": "monitoring"}),
                {"enabled": False, "network_requests": 0},
            )
            self.assertFalse(LAB.plan({})["enabled"])
            opener.assert_not_called()

    def test_profile_and_resources_remain_optional_and_isolated(self):
        config = yaml.safe_load(
            (ROOT / "infra/openobserve/compose.yaml").read_text()
        )
        self.assertEqual(set(config["services"]), {"openobserve"})
        service = config["services"]["openobserve"]
        self.assertEqual(service["profiles"], ["openobserve-lab"])
        self.assertNotIn("depends_on", service)
        self.assertTrue(service["read_only"])
        self.assertEqual(service["cap_drop"], ["ALL"])
        self.assertEqual(service["volumes"], ["openobserve-data:/data"])
        self.assertTrue(service["ports"][0].startswith("127.0.0.1:"))
        self.assertNotIn("privileged", service)
        self.assertNotIn("pid", service)
        self.assertNotIn(
            "OPENOBSERVE_INGEST_TOKEN_FILE", service["environment"]
        )
        self.assertEqual(
            service["environment"]["ZO_COMPACT_DATA_RETENTION_DAYS"], "7"
        )
        self.assertEqual(service["environment"]["ZO_TELEMETRY"], "false")
        self.assertEqual(
            service["environment"]["ZO_PROMETHEUS_ENABLED"], "false"
        )
        self.assertIn(
            "DATA_VOLUME_PREFIX", config["volumes"]["openobserve-data"]["name"]
        )

    def test_vendor_artifacts_are_stable_pinned_and_probe_static(self):
        lock = json.loads(
            (ROOT / "infra/openobserve/images.lock.json").read_text()
        )
        self.assertEqual(lock["openobserve"]["version"], "1.1.2")
        dockerfile = (ROOT / "infra/openobserve/Dockerfile").read_text()
        self.assertIn(lock["openobserve"]["pinned"], dockerfile)
        self.assertIn(lock["health_probe"]["digest"], dockerfile)
        self.assertEqual(
            set(lock["health_probe"]["platforms"]), {"amd64", "arm64"}
        )
        for artifact in lock["health_probe"]["platforms"].values():
            self.assertFalse(artifact["elf_has_interpreter"])
        self.assertNotIn("apt-get", dockerfile)
        self.assertNotIn("cargo build", dockerfile)
        self.assertFalse(lock["validation"]["image_build_or_run"])


class LabApiTests(unittest.TestCase):
    """Check opt-in, authentication and bounded aggregate output."""

    def test_active_operations_without_consent_make_no_request(self):
        transport = Requests({"status": "ok"})
        for operation in (
            lambda: LAB.health({}, opener=transport),
            lambda: LAB.counts({}, opener=transport),
            lambda: LAB.create_token({}, "/unused", opener=transport),
        ):
            with self.assertRaises(LAB.LabError):
                operation()
        self.assertEqual(transport.requests, [])

    def test_health_uses_no_credentials_and_only_fixed_loopback(self):
        transport = Requests({"status": "ok"})
        self.assertTrue(LAB.health({}, allow_lab=True, opener=transport)["ok"])
        request = transport.requests[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:5080/healthz")
        self.assertNotIn("Authorization", request.headers)
        transport = Requests({"status": "not ok"})
        with self.assertRaises(LAB.LabError):
            LAB.health({}, allow_lab=True, opener=transport)

    def test_proxy_is_bypassed_and_redirects_are_refused(self):
        transport = Requests({"status": "ok"})
        with mock.patch.object(
            LAB.urllib.request, "build_opener", return_value=transport
        ) as factory:
            LAB.health({}, allow_lab=True)
        self.assertEqual(factory.call_args.args[0].proxies, {})
        self.assertIsInstance(factory.call_args.args[1], LAB.NoRedirect)
        with self.assertRaises(LAB.LabError):
            LAB.NoRedirect().redirect_request(
                None, None, 302, "redirect", {}, "https://example.invalid/"
            )

    def test_no_arbitrary_endpoint_or_invalid_port(self):
        for port in ("0", "65536", "5080/path", "http://example.invalid"):
            transport = Requests({"status": "ok"})
            with self.subTest(port=port), self.assertRaises(LAB.LabError):
                LAB.health(
                    {"OPENOBSERVE_PORT": port},
                    allow_lab=True,
                    opener=transport,
                )
            self.assertEqual(transport.requests, [])

    def test_count_query_is_fixed_bounded_and_does_not_return_messages(self):
        transport = Requests(
            {
                "hits": [
                    {
                        "total_records": 9,
                        "distinct_events": 7,
                        "message": "synthetic-private-log-not-for-output",
                    }
                ]
            }
        )
        with mock.patch.object(
            LAB.time, "time_ns", return_value=1000000000000
        ):
            result = LAB.counts(
                settings(), allow_lab=True, window_seconds=30, opener=transport
            )
        self.assertEqual(result["duplicate_excess"], 2)
        self.assertNotIn("message", json.dumps(result))
        query = json.loads(transport.requests[0].data)["query"]
        self.assertEqual(query["end_time"] - query["start_time"], 30000000)
        self.assertEqual(query["size"], 1)
        self.assertIn('FROM "lakehouse_logs"', query["sql"])
        self.assertTrue(
            transport.requests[0].full_url.endswith(LAB.SEARCH_PATH)
        )
        header = transport.requests[0].headers["Authorization"]
        user, password = (
            base64.b64decode(header.split()[1]).decode().split(":")
        )
        self.assertEqual(user, settings()["OPENOBSERVE_ROOT_USER_EMAIL"])
        self.assertEqual(
            password, settings()["OPENOBSERVE_ROOT_USER_PASSWORD"]
        )

    def test_invalid_count_response_never_becomes_fake_zero(self):
        for body in (
            {"hits": []},
            {"hits": [{"total_records": True, "distinct_events": 0}]},
            {"hits": [{"total_records": 1, "distinct_events": 2}]},
        ):
            with self.subTest(body=body), self.assertRaises(LAB.LabError):
                LAB.counts(settings(), allow_lab=True, opener=Requests(body))

    def test_server_error_text_is_never_exposed(self):
        secret = settings()["OPENOBSERVE_ROOT_USER_PASSWORD"]
        transport = mock.Mock()
        transport.open.side_effect = urllib.error.HTTPError(
            "http://127.0.0.1:5080/", 403, secret, {}, None
        )
        with self.assertRaises(LAB.LabError) as caught:
            LAB.counts(settings(), allow_lab=True, opener=transport)
        self.assertNotIn(secret, str(caught.exception))

    def test_oversized_response_is_rejected(self):
        transport = mock.Mock()
        response = io.BytesIO(b"x" * (LAB.MAX_RESPONSE + 1))
        response.status = 200
        transport.open.return_value = response
        with self.assertRaises(LAB.LabError):
            LAB.health({}, allow_lab=True, opener=transport)

    def test_trickled_partial_response_hits_one_total_deadline(self):
        clock = [0]
        reads = []
        response = Response({"status": "ok"})

        def trickle(maximum):
            """Deliver bytes before each socket timeout, but too slowly."""
            reads.append(maximum)
            clock[0] += 8
            return b"private-error-body"

        response.read1 = trickle
        transport = mock.Mock()
        transport.open.return_value = response
        with mock.patch.object(
            LAB.time, "monotonic", side_effect=lambda: clock[0]
        ):
            with self.assertRaisesRegex(LAB.LabError, "deadline") as caught:
                LAB.health({}, allow_lab=True, opener=transport)
        self.assertEqual(len(reads), 2)
        self.assertNotIn("private-error-body", str(caught.exception))
        self.assertEqual(transport.open.call_args.kwargs["timeout"], 3)

    def test_read1_cap_never_requests_more_than_limit_plus_one(self):
        response = Response({"padding": "x" * 64})
        reader = response.read1
        sizes = []

        def bounded(maximum):
            """Capture the bounded read1 request independently of read."""
            sizes.append(maximum)
            return reader(maximum)

        response.read1 = bounded
        response.read = mock.Mock(side_effect=AssertionError("no read"))
        transport = mock.Mock()
        transport.open.return_value = response
        with mock.patch.object(LAB, "MAX_RESPONSE", 16):
            with self.assertRaisesRegex(LAB.LabError, "safety limit"):
                LAB.health({}, allow_lab=True, opener=transport)
        self.assertEqual(sizes, [17])
        response.read.assert_not_called()

    def test_socket_read_timeout_is_limited_by_remaining_deadline(self):
        clock = [0]
        response = Response({"status": "ok"})
        connection = mock.Mock()
        response.fp = SimpleNamespace(raw=SimpleNamespace(_sock=connection))
        reader = response.read1

        def nearly_expired(maximum):
            """Consume most of the shared deadline during the first read."""
            clock[0] = 14
            return reader(maximum)

        response.read1 = nearly_expired
        transport = mock.Mock()
        transport.open.return_value = response
        with mock.patch.object(
            LAB.time, "monotonic", side_effect=lambda: clock[0]
        ):
            LAB.health({}, allow_lab=True, opener=transport)
        self.assertEqual(
            connection.settimeout.call_args_list, [mock.call(3), mock.call(1)]
        )

    def test_partial_http_failure_never_exposes_payload(self):
        response = Response({})
        response.read1 = mock.Mock(
            side_effect=http.client.IncompleteRead(b"private-error-body")
        )
        transport = mock.Mock()
        transport.open.return_value = response
        with self.assertRaises(LAB.LabError) as caught:
            LAB.health({}, allow_lab=True, opener=transport)
        self.assertNotIn("private-error-body", str(caught.exception))


class LabTokenTests(unittest.TestCase):
    """Create tokens explicitly without replacing files or printing secrets."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="openobserve-unit-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.output = self.directory / "ingest.json"
        self.token = "o2oi_" + "S" * 32
        self.transport = Requests(
            {
                "data": {
                    "name": LAB.TOKEN_NAME,
                    "token": self.token,
                    "enabled": True,
                }
            }
        )

    def test_named_token_is_saved_private_without_updating_site_settings(self):
        values = settings()
        original = dict(values)
        result = LAB.create_token(
            values,
            self.output,
            allow_lab=True,
            allow_write=True,
            opener=self.transport,
        )
        self.assertEqual(
            json.loads(self.output.read_text()), {"token": self.token}
        )
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.token, json.dumps(result))
        self.assertEqual(values, original)
        body = json.loads(self.transport.requests[0].data)
        self.assertEqual(body["name"], LAB.TOKEN_NAME)
        self.assertFalse(body["splunk_token"])

    def test_existing_output_or_symlink_prevents_admin_request(self):
        self.output.write_text("keep-existing-file")
        for output in (self.output, self.directory / "link.json"):
            if output != self.output:
                output.symlink_to(self.output)
            with self.assertRaises(LAB.LabError):
                LAB.create_token(
                    settings(),
                    output,
                    allow_lab=True,
                    allow_write=True,
                    opener=self.transport,
                )
        self.assertEqual(self.output.read_text(), "keep-existing-file")
        self.assertEqual(self.transport.requests, [])

    def test_bad_token_response_does_not_leave_an_empty_secret_file(self):
        for data in (
            None,
            {},
            {
                "name": LAB.TOKEN_NAME,
                "enabled": True,
                "token": "synthetic-wrong-token-type",
            },
        ):
            with self.subTest(data=data), self.assertRaises(LAB.LabError):
                LAB.create_token(
                    settings(),
                    self.output,
                    allow_lab=True,
                    allow_write=True,
                    opener=Requests({"data": data}),
                )
            self.assertFalse(self.output.exists())

    def test_root_secret_cannot_be_reused_for_ingestion(self):
        values = settings()
        values["OPENOBSERVE_ROOT_USER_PASSWORD"] = self.token
        self.output.write_text(json.dumps({"token": self.token}))
        self.output.chmod(0o600)
        values["OPENOBSERVE_INGEST_TOKEN_FILE"] = str(self.output)
        transport = Requests({"hits": []})
        with self.assertRaises(LAB.LabError):
            LAB.counts(values, allow_lab=True, opener=transport)
        self.assertEqual(transport.requests, [])

    def test_public_readable_token_file_is_rejected(self):
        self.output.write_text(json.dumps({"token": self.token}))
        self.output.chmod(0o644)
        values = settings()
        values["OPENOBSERVE_INGEST_TOKEN_FILE"] = str(self.output)
        with self.assertRaises(LAB.LabError):
            LAB.counts(values, allow_lab=True, opener=self.transport)
        self.assertEqual(self.transport.requests, [])

    def test_output_ancestor_link_is_rejected_before_token_creation(self):
        alias = self.directory / "linked-directory"
        alias.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(LAB.LabError):
            LAB.create_token(
                settings(),
                alias / self.output.name,
                allow_lab=True,
                allow_write=True,
                opener=self.transport,
            )
        self.assertEqual(self.transport.requests, [])

    def test_failed_creation_never_unlinks_a_replaced_output(self):
        replacement = self.directory / "replacement.json"
        replacement.write_text("keep-replacement")
        transport = mock.Mock()

        def replace_output(*args, **kwargs):
            """Simulate another same-UID process replacing reserved output."""
            replacement.replace(self.output)
            return Response({"data": {}})

        transport.open.side_effect = replace_output
        with self.assertRaises(LAB.LabError):
            LAB.create_token(
                settings(),
                self.output,
                allow_lab=True,
                allow_write=True,
                opener=transport,
            )
        self.assertEqual(self.output.read_text(), "keep-replacement")


class LabPrivateConfigTests(unittest.TestCase):
    """Validate files offline, including links and replacement races."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lab-private-unit-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "ingestion.json"
        self.token = "o2oi_" + "T" * 32
        self.path.write_text(json.dumps({"token": self.token}))
        self.path.chmod(0o600)
        self.values = settings()
        self.values.update(
            COMPOSE_PROFILES="monitoring,openobserve-lab",
            OPENOBSERVE_INGEST_TOKEN_FILE=str(self.path),
        )

    def rejected(self, values=None):
        """Require safe errors without filenames, credentials or JSON text."""
        with self.assertRaises(LAB.LabError) as caught:
            LAB.validate_config(self.values if values is None else values)
        message = str(caught.exception)
        for private in (
            str(self.path),
            self.token,
            self.values["OPENOBSERVE_ROOT_USER_PASSWORD"],
            "private-error-body",
        ):
            self.assertNotIn(private, message)

    def test_lab_off_never_reads_credentials_or_paths(self):
        values = dict(self.values, COMPOSE_PROFILES="monitoring")
        values["OPENOBSERVE_ROOT_USER_PASSWORD"] = ""
        with mock.patch.object(LAB.os, "open") as opened:
            LAB.validate_config(values)
            LAB.validate_config({})
        opened.assert_not_called()

    def test_lab_on_validates_offline_and_bootstrap_is_explicit(self):
        with mock.patch.object(LAB.urllib.request, "build_opener") as opener:
            self.assertIsNone(LAB.validate_config(self.values))
            without_file = dict(self.values, OPENOBSERVE_INGEST_TOKEN_FILE="")
            self.rejected(without_file)
            LAB.validate_config(without_file, require_token=False)
        opener.assert_not_called()

    def test_bootstrap_shape_is_validated_before_file_access(self):
        for key, value in (
            ("OPENOBSERVE_ROOT_USER_EMAIL", "invalid-email"),
            ("OPENOBSERVE_ROOT_USER_PASSWORD", "too-short"),
            ("OPENOBSERVE_ROOT_USER_PASSWORD", "x" * 32 + "\n"),
        ):
            with self.subTest(key=key, length=len(value)):
                self.rejected(dict(self.values, **{key: value}))

    def test_ancestor_and_leaf_links_are_rejected(self):
        alias = self.directory / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        leaf = self.directory / "leaf.json"
        leaf.symlink_to(self.path)
        for path in (alias / self.path.name, leaf):
            with self.subTest(kind=path.name):
                values = dict(self.values, OPENOBSERVE_INGEST_TOKEN_FILE=path)
                self.rejected(values)

    def test_relative_parent_traversal_and_checkout_paths_are_rejected(self):
        for path in (
            Path("ingestion.json"),
            self.directory / ".." / self.directory.name / self.path.name,
        ):
            self.rejected(
                dict(self.values, OPENOBSERVE_INGEST_TOKEN_FILE=path)
            )
        with mock.patch.object(LAB, "ROOT", self.directory):
            self.rejected()

    def test_exact_private_mode_and_current_owner_are_required(self):
        for mode in (0o400, 0o640, 0o644, 0o700, 0o1600):
            self.path.chmod(mode)
            with self.subTest(mode=oct(mode)):
                self.rejected()
        self.path.chmod(0o600)
        wrong_uid = os.getuid() + 1
        with mock.patch.object(LAB.os, "getuid", return_value=wrong_uid):
            self.rejected()

    def test_selected_runtime_uid_controls_owner_validation(self):
        actual_uid = os.getuid()
        values = dict(self.values, WORKER_UID=str(actual_uid))
        with mock.patch.object(LAB.os, "getuid", return_value=actual_uid + 1):
            LAB.validate_config(values)
        for uid in ("0", "-1", "invalid", 123, str(actual_uid + 1)):
            with self.subTest(uid=uid):
                self.rejected(dict(self.values, WORKER_UID=uid))

    def test_directory_fifo_and_oversized_file_are_rejected_without_read(self):
        folder = self.directory / "folder"
        folder.mkdir(mode=0o700)
        fifo = self.directory / "fifo"
        os.mkfifo(fifo, mode=0o600)
        oversized = self.directory / "oversized.json"
        oversized.write_bytes(b"x" * (LAB.MAX_TOKEN_FILE + 1))
        oversized.chmod(0o600)
        for path in (folder, fifo, oversized):
            with self.subTest(kind=path.name):
                with mock.patch.object(LAB.os, "read") as reader:
                    self.rejected(
                        dict(self.values, OPENOBSERVE_INGEST_TOKEN_FILE=path)
                    )
                reader.assert_not_called()

    def test_content_shape_root_reuse_and_json_errors_stay_private(self):
        for content in (
            "private-error-body",
            "[]",
            json.dumps({"token": "wrong-format"}),
            json.dumps({"token": self.token, "extra": "private-error-body"}),
            '{"token":"duplicate","token":"' + self.token + '"}',
        ):
            self.path.write_text(content)
            self.rejected()
        self.path.write_text(json.dumps({"token": self.token}))
        self.rejected(
            dict(self.values, OPENOBSERVE_ROOT_USER_PASSWORD=self.token)
        )

    def test_leaf_replacement_is_detected_using_pinned_identity(self):
        reader = os.read

        def replace_after_read(descriptor, size):
            """Replace the directory entry while the original fd is open."""
            block = reader(descriptor, size)
            if block:
                replacement = self.directory / "replacement.json"
                replacement.write_text(json.dumps({"token": self.token}))
                replacement.chmod(0o600)
                replacement.replace(self.path)
            return block

        with mock.patch.object(LAB.os, "read", side_effect=replace_after_read):
            self.rejected()

    def test_growth_during_read_is_bounded_and_rejected(self):
        reader = os.read
        sizes = []

        def grow_before_read(descriptor, size):
            """Simulate concurrent growth after the original fstat."""
            if not sizes:
                with self.path.open("ab") as stream:
                    stream.write(b"x" * 8192)
            sizes.append(size)
            return reader(descriptor, size)

        with mock.patch.object(LAB.os, "read", side_effect=grow_before_read):
            self.rejected()
        self.assertEqual(sizes, [LAB.MAX_TOKEN_FILE + 1])


class LabEntrypointTests(unittest.TestCase):
    """Execute the real shell guards with stubs, never the vendor binary."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="openobserve-shell-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state = self.directory / "state"
        self.state.mkdir(mode=0o700)
        self.probe = self.directory / "busybox-stub"
        self.probe.write_text(
            "#!/bin/sh\n"
            'case "$1 $2" in\n'
            '  "id -u"|"id -g") printf "10001\\n";;\n'
            '  "stat -c") stat -c "10001:10001:%a" "$4";;\n'
            '  "wget -q") test -z "${HTTP_PROXY:-}${http_proxy:-}"; '
            'printf "probe-called\\n";;\n'
            '  *) exec "$@";;\n'
            "esac\n"
        )
        self.probe.chmod(0o700)
        self.service = self.directory / "service-stub"
        self.service.write_text('#!/bin/sh\nprintf "service-started\\n"\n')
        self.service.chmod(0o700)

    def run_wrapper(self, mode=0o700, credentials=True):
        """Run a temporary wrapper copy against owned temporary state."""
        self.state.chmod(mode)
        text = (ROOT / "infra/openobserve/entrypoint.sh").read_text()
        text = text.replace("/bin/busybox", str(self.probe))
        text = text.replace("/data", str(self.state))
        text = text.replace("exec /openobserve", "exec " + str(self.service))
        script = self.directory / "entrypoint.sh"
        script.write_text(text)
        environment = dict(os.environ)
        environment.pop("ZO_ROOT_USER_EMAIL", None)
        environment.pop("ZO_ROOT_USER_PASSWORD", None)
        if credentials:
            environment.update(
                ZO_ROOT_USER_EMAIL="operator@example.invalid",
                ZO_ROOT_USER_PASSWORD="synthetic-not-for-output",
            )
        return subprocess.run(
            ["sh", str(script)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_prepared_state_starts_but_bad_mode_is_never_repaired(self):
        self.assertEqual(self.run_wrapper().returncode, 0)
        result = self.run_wrapper(mode=0o755)
        self.assertEqual(result.returncode, 78)
        self.assertNotIn("service-started", result.stdout)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o755)

    def test_missing_bootstrap_credentials_fail_without_secret_output(self):
        result = self.run_wrapper(credentials=False)
        self.assertEqual(result.returncode, 78)
        self.assertNotIn("service-started", result.stdout)
        result = self.run_wrapper()
        self.assertNotIn(
            "synthetic-not-for-output", result.stdout + result.stderr
        )

    def test_probe_discards_proxy_values_and_does_no_real_network_io(self):
        text = (ROOT / "infra/openobserve/health.sh").read_text()
        script = self.directory / "health.sh"
        script.write_text(text.replace("/bin/busybox", str(self.probe)))
        environment = dict(os.environ)
        environment.update(
            HTTP_PROXY="synthetic-proxy", http_proxy="synthetic"
        )
        result = subprocess.run(
            ["sh", str(script)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("probe-called", result.stdout)


if __name__ == "__main__":
    unittest.main()
