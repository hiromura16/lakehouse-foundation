"""Probe real local TLS/CONNECT boundaries without company infrastructure."""

import base64
import importlib.util
import json
import select
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "proxy_checks_subject", ROOT / "infra/demo/proxy_checks.py"
)
CHECKS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKS)


class OriginHandler(BaseHTTPRequestHandler):
    """Return fixed status only; never print headers or credentials."""

    def do_HEAD(self):
        self.send_response(302 if self.path == "/redirect" else 200)
        if self.path == "/redirect":
            self.send_header("Location", "https://outside.invalid/")
        self.end_headers()

    def log_message(self, *_):
        pass


class QuietHTTPServer(ThreadingHTTPServer):
    """Suppress expected TLS failures from negative trust tests."""

    def handle_error(self, *_):
        pass


class ConnectHandler(socketserver.StreamRequestHandler):
    """Accept authenticated CONNECT to the one local test origin only."""

    def handle(self):
        line = self.rfile.readline().decode().strip()
        headers = {}
        while True:
            header = self.rfile.readline().decode().strip()
            if not header:
                break
            key, _, value = header.partition(":")
            headers[key.lower()] = value.strip()
        self.server.calls.append(line)
        if headers.get("proxy-authorization") != self.server.authorization:
            self.wfile.write(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n"
            )
            return
        allowed = {
            f"CONNECT localhost:{self.server.origin_port} HTTP/1.1",
            "CONNECT connect-fixture.example.invalid:"
            f"{self.server.origin_port} HTTP/1.1",
        }
        if line not in allowed:
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        with socket.create_connection(
            ("127.0.0.1", self.server.origin_port), timeout=2
        ) as upstream:
            self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.wfile.flush()
            peers = [self.connection, upstream]
            while True:
                readable, _, _ = select.select(peers, [], [], 2)
                if not readable:
                    return
                for peer in readable:
                    data = peer.recv(65536)
                    if not data:
                        return
                    other = (
                        upstream
                        if peer is self.connection
                        else self.connection
                    )
                    other.sendall(data)


class ConnectServer(socketserver.ThreadingTCPServer):
    """Run a loopback-only, single-origin proxy with ephemeral credentials."""

    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, *_):
        pass


class ProxyDiagnosticsTests(unittest.TestCase):
    """Check routing, auth and CA failures using real TLS sockets."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("openssl") is None:
            raise unittest.SkipTest("Local TLS fixture needs openssl.")
        cls.temporary = tempfile.TemporaryDirectory(prefix="proxy-tls-test-")
        cls.directory = Path(cls.temporary.name)
        cls.key = cls.directory / "test.key"
        cls.cert = cls.directory / "test.crt"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(cls.key),
                "-out",
                str(cls.cert),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,"
                "DNS:connect-fixture.example.invalid",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.key.chmod(0o600)
        cls.origin = QuietHTTPServer(("127.0.0.1", 0), OriginHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, cls.key)
        cls.origin.socket = context.wrap_socket(
            cls.origin.socket, server_side=True
        )
        cls.origin_thread = threading.Thread(
            target=cls.origin.serve_forever, daemon=True
        )
        cls.origin_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.origin.shutdown()
        cls.origin.server_close()
        cls.origin_thread.join(timeout=2)
        cls.temporary.cleanup()

    def setUp(self):
        self.proxy = ConnectServer(("127.0.0.1", 0), ConnectHandler)
        self.proxy.origin_port = self.origin.server_port
        self.proxy.calls = []
        self.user = "synthetic-user"
        self.password = "synthetic-pass"
        encoded = base64.b64encode(
            (self.user + ":" + self.password).encode()
        ).decode()
        self.proxy.authorization = "Basic " + encoded
        self.proxy_thread = threading.Thread(
            target=self.proxy.serve_forever, daemon=True
        )
        self.proxy_thread.start()
        self.addCleanup(self.proxy_thread.join, 2)
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)
        self.values = {
            "HTTPS_PROXY": (
                f"http://{self.user}:{self.password}@127.0.0.1:"
                + str(self.proxy.server_address[1])
            ),
            "NO_PROXY": ",".join(CHECKS.INTERNAL_SERVICES),
            "SSL_CERT_FILE": str(self.cert),
        }

    def probe(self, values=None, path="/", proxy_fixture=False):
        # CONNECT still goes only to this loopback fixture. The reserved DNS
        # name represents an external target, so required localhost bypass
        # does not prevent testing authenticated proxy/TLS behavior.
        host = (
            "connect-fixture.example.invalid" if proxy_fixture else "localhost"
        )
        target = f"https://{host}:{self.origin.server_port}" + path
        with mock.patch.dict(CHECKS.TARGETS, {"pypi": target}):
            return CHECKS.run_checks(
                values or self.values, ["pypi"], timeout=2
            )

    def test_authenticated_proxy_and_ca_succeed_without_secret_output(self):
        report = self.probe(proxy_fixture=True)
        self.assertTrue(report["checks"][0]["ok"])
        self.assertTrue(report["checks"][0]["via_proxy"])
        self.assertEqual(len(self.proxy.calls), 1)
        serialized = json.dumps(report)
        for private in [self.user, self.password, str(self.cert)]:
            self.assertNotIn(private, serialized)

    def test_wrong_credentials_and_missing_ca_have_distinct_safe_codes(self):
        wrong = {
            **self.values,
            "HTTPS_PROXY": self.values["HTTPS_PROXY"].replace(
                self.password, "wrong"
            ),
        }
        self.assertEqual(
            self.probe(wrong, proxy_fixture=True)["checks"][0]["code"],
            "PROXY_AUTH_REQUIRED",
        )
        without_ca = {
            key: value
            for key, value in self.values.items()
            if key != "SSL_CERT_FILE"
        }
        self.assertEqual(
            self.probe(without_ca, proxy_fixture=True)["checks"][0]["code"],
            "TLS_VERIFY_FAILED",
        )

    def test_no_proxy_bypasses_auth_proxy_and_redirect_is_not_followed(self):
        direct = {**self.values, "NO_PROXY": "localhost"}
        result = self.probe(direct)["checks"][0]
        self.assertTrue(result["ok"])
        self.assertFalse(result["via_proxy"])
        self.assertEqual(self.proxy.calls, [])
        result = self.probe(direct, "/redirect")["checks"][0]
        self.assertEqual(result["code"], "REDIRECT_BLOCKED")
        self.assertFalse(result["ok"])

    def test_configuration_has_only_presence_and_fixed_internal_names(self):
        report = CHECKS.configuration_report(self.values)
        self.assertTrue(report["all_internal_bypassed"])
        self.assertFalse(report["variables"]["HTTPS_PROXY"]["case_matches"])
        self.assertNotIn(self.password, json.dumps(report))
        overridden = {**self.values, "https_proxy": ""}
        self.assertFalse(self.probe(overridden)["checks"][0]["via_proxy"])

    def test_saved_corporate_ca_adds_trust_without_replacing_system_roots(
        self,
    ):
        values = {
            key: value
            for key, value in self.values.items()
            if key != "SSL_CERT_FILE"
        }
        values["CORPORATE_CA_FILE"] = str(self.cert)
        self.assertTrue(self.probe(values)["checks"][0]["ok"])
        self.assertTrue(
            CHECKS.configuration_report(values)["custom_ca_configured"]
        )
        with mock.patch.object(
            CHECKS.ssl, "create_default_context"
        ) as factory:
            context = CHECKS._context(values)
            factory.assert_called_once_with()
            context.load_verify_locations.assert_called_once_with(
                cafile=str(self.cert)
            )

    def test_arbitrary_targets_and_unbounded_timeouts_are_rejected(self):
        for targets, timeout in [
            (["https://outside.invalid/"], 1),
            (["pypi", "pypi"], 1),
            (["pypi"], 11),
            (["pypi"], float("nan")),
        ]:
            with self.assertRaises(ValueError):
                CHECKS.run_checks(self.values, targets, timeout)
        self.assertEqual(self.proxy.calls, [])
