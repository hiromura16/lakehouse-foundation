"""Verify CA bootstrap in temporary paths without changing host trust."""

import importlib.util
import shutil
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "corporate_ca_subject", ROOT / "infra/proxy/install_ca.py"
)
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class CertificateInstallTests(unittest.TestCase):
    """Retain original roots and reject private or malformed input."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("openssl") is None:
            raise unittest.SkipTest(
                "Public certificate fixture needs openssl."
            )
        cls.temporary = tempfile.TemporaryDirectory(prefix="ca-helper-test-")
        cls.directory = Path(cls.temporary.name)
        cls.certificates = []
        for name in ["base-root", "corporate-root"]:
            key = cls.directory / (name + ".key")
            certificate = cls.directory / (name + ".crt")
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(key),
                    "-out",
                    str(certificate),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=" + name,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            key.chmod(0o600)
            cls.certificates.append(certificate.read_bytes())

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="ca-install-output-")
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)
        self.bundle = self.output / "system-bundle.crt"
        self.bundle.write_bytes(self.certificates[0])
        self.source = self.output / "corporate.crt"
        self.source.write_bytes(self.certificates[1])
        self.cert_dir = self.output / "certificates"

    def install(self):
        return INSTALLER.install(
            self.source, bundle=self.bundle, cert_dir=self.cert_dir, java=False
        )

    def test_absent_and_empty_input_have_no_side_effects(self):
        missing = self.output / "missing.crt"
        result = INSTALLER.install(
            missing, bundle=self.bundle, cert_dir=self.cert_dir
        )
        self.assertEqual(result["status"], "skipped")
        self.source.write_bytes(b"")
        self.assertEqual(self.install()["status"], "skipped")
        self.assertFalse(self.cert_dir.exists())
        self.assertEqual(self.bundle.read_bytes(), self.certificates[0])

    def test_public_chain_preserves_base_roots_and_is_idempotent(self):
        with mock.patch.object(INSTALLER.shutil, "which", return_value=None):
            first = self.install()
            second = self.install()
        self.assertEqual(first["status"], "installed")
        self.assertEqual(second["certificates"], 1)
        self.assertEqual(
            len(INSTALLER.certificates(self.bundle.read_bytes())), 2
        )
        self.assertEqual(len(list(self.cert_dir.glob("*.crt"))), 1)
        self.assertIn(self.certificates[0].strip(), self.bundle.read_bytes())
        self.assertIn(self.certificates[1].strip(), self.bundle.read_bytes())

    def test_os_update_cannot_drop_roots_from_the_original_bundle(self):
        def rebuild(*_, **__):
            self.bundle.write_bytes(self.certificates[1])
            return subprocess.CompletedProcess([], 0)

        with (
            mock.patch.object(
                INSTALLER.shutil,
                "which",
                return_value="update-ca-certificates",
            ),
            mock.patch.object(
                INSTALLER.subprocess, "run", side_effect=rebuild
            ),
        ):
            self.install()
        self.assertEqual(
            len(INSTALLER.certificates(self.bundle.read_bytes())), 2
        )
        self.assertIn(self.certificates[0].strip(), self.bundle.read_bytes())

    def test_private_key_and_non_pem_inputs_are_rejected_before_writes(self):
        key = (self.directory / "corporate-root.key").read_bytes()
        for value in [key, self.certificates[1] + key, b"not-a-certificate"]:
            with self.subTest(kind=value[:10]):
                self.source.write_bytes(value)
                with self.assertRaises((ValueError, ssl.SSLError)):
                    self.install()
                self.assertFalse(self.cert_dir.exists())
                self.assertEqual(
                    self.bundle.read_bytes(), self.certificates[0]
                )

    def test_java_adds_only_hash_aliases_and_keeps_existing_entries(self):
        entries = INSTALLER.certificates(self.certificates[1], strict=True)
        digest, _, der = entries[0]
        files = {digest: self.source}
        store = self.output / "cacerts"
        with mock.patch.object(
            INSTALLER, "java_store", return_value=("keytool", store)
        ):
            with mock.patch.object(
                INSTALLER.subprocess,
                "run",
                side_effect=[
                    subprocess.CompletedProcess([], 1),
                    subprocess.CompletedProcess([], 0),
                ],
            ) as commands:
                self.assertEqual(INSTALLER.install_java(entries, files), 1)
            self.assertEqual(commands.call_count, 2)
            self.assertNotIn("-delete", str(commands.call_args_list))
            self.assertIn(
                "lakehouse-corporate-" + digest, commands.call_args.args[0]
            )
            with mock.patch.object(
                INSTALLER.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=der),
            ) as commands:
                self.assertEqual(INSTALLER.install_java(entries, files), 0)
                commands.assert_called_once()
