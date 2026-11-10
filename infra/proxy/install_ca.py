#!/usr/bin/env python3
"""Add public CA certificates to build-image trust without dropping roots.

This command writes the OS certificate directory and bundle, and imports
hash-named aliases into the existing Java truststore when keytool is present.
Missing or empty optional input is skipped. Nonempty input must be a regular,
non-symlink PEM file of at most one MiB, with at most 64 certificate blocks.
Private keys are rejected. Added trust remains in the resulting image.
"""

import argparse
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
from hashlib import sha256
from pathlib import Path

CERTIFICATES = re.compile(
    rb"-----BEGIN CERTIFICATE-----\s+.+?\s+-----END CERTIFICATE-----",
    re.DOTALL,
)
BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
CERT_DIR = Path("/usr/local/share/ca-certificates")


def certificates(content, strict=False):
    """Validate X.509 PEM blocks; build input may contain certificates only."""
    if b"PRIVATE KEY" in content:
        raise ValueError("Private keys are never accepted as CA input.")
    matches = list(CERTIFICATES.finditer(content))
    if not matches or (strict and len(matches) > 64):
        raise ValueError("A bounded PEM certificate chain is required.")
    if strict and CERTIFICATES.sub(b"", content).strip():
        raise ValueError("CA input must contain PEM certificates only.")
    result = []
    for match in matches:
        pem = match.group() + b"\n"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=pem.decode("ascii"))
        der = ssl.PEM_cert_to_DER_cert(pem.decode("ascii"))
        result.append((sha256(der).hexdigest(), pem, der))
    return result


def atomic_write(path, content, permissions=0o644):
    """Publish public certificate bytes without exposing partial bundles."""
    descriptor, temporary = tempfile.mkstemp(prefix=".ca-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, permissions)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def java_store():
    """Find the installed JRE's existing truststore without replacing it."""
    tool = shutil.which("keytool")
    if tool is None:
        return None
    homes = []
    if os.environ.get("JAVA_HOME"):
        homes.append(Path(os.environ["JAVA_HOME"]))
    homes.append(Path(tool).resolve().parents[1])
    for home in homes:
        for relative in ["lib/security/cacerts", "jre/lib/security/cacerts"]:
            store = home / relative
            if store.is_file():
                return tool, store
    raise ValueError("Existing Java truststore was not found.")


def install_java(entries, files):
    """Import hash-named aliases, retaining every pre-existing JRE entry."""
    target = java_store()
    if target is None:
        return 0
    tool, store = target
    count = 0
    for digest, _, der in entries:
        alias = "lakehouse-corporate-" + digest
        arguments = [
            tool,
            "-keystore",
            str(store),
            "-storepass",
            "changeit",
            "-alias",
            alias,
        ]
        existing = subprocess.run(
            arguments + ["-exportcert"], capture_output=True, check=False
        )
        if existing.returncode == 0:
            if existing.stdout != der:
                raise ValueError("A conflicting Java CA alias exists.")
            continue
        result = subprocess.run(
            arguments
            + ["-importcert", "-noprompt", "-file", str(files[digest])],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise ValueError("Java CA import failed; existing trust is kept.")
        count += 1
    return count


def install(ca_file, *, cert_dir=CERT_DIR, bundle=BUNDLE, java=True):
    """Install a bounded public chain, preserving OS and optional JRE roots.

    Return status and certificate counts only. Missing/empty input is a
    no-op. Installing requires permission to write the selected certificate
    directory, OS bundle and, when enabled, the existing Java truststore.
    """
    source = Path(ca_file)
    if not source.exists() or source == Path("/dev/null"):
        return {"status": "skipped", "certificates": 0, "java_certificates": 0}
    if (
        not source.is_file()
        or source.is_symlink()
        or source.stat().st_size > 1024 * 1024
    ):
        raise ValueError("CA input must be a bounded regular public PEM file.")
    content = source.read_bytes()
    if not content.strip():
        return {"status": "skipped", "certificates": 0, "java_certificates": 0}
    entries = certificates(content, strict=True)
    # Save every original root before the OS utility regenerates its bundle.
    target = Path(bundle).resolve()
    original = target.read_bytes()
    roots = certificates(original)
    directory = Path(cert_dir)
    directory.mkdir(parents=True, exist_ok=True)
    files = {}
    for digest, pem, _ in entries:
        path = directory / ("lakehouse-corporate-" + digest + ".crt")
        if path.is_symlink():
            raise ValueError("CA certificate paths must not use symlinks.")
        if path.exists() and path.read_bytes() != pem:
            raise ValueError("A conflicting CA certificate file exists.")
        if not path.exists():
            atomic_write(path, pem)
        files[digest] = path
    update = shutil.which("update-ca-certificates")
    if update is not None:
        result = subprocess.run([update], capture_output=True, check=False)
        if result.returncode:
            raise ValueError("OS CA update failed.")
    current = target.read_bytes()
    known = {item[0] for item in certificates(current)}
    merged = current.rstrip() + b"\n"
    for digest, pem, _ in roots + entries:
        if digest not in known:
            merged += pem
            known.add(digest)
    atomic_write(target, merged, stat.S_IMODE(target.stat().st_mode))
    imported = install_java(entries, files) if java else 0
    return {
        "status": "installed",
        "certificates": len(entries),
        "java_certificates": imported,
    }


def main():
    """Run during image build; log counts only, never names or PEM bytes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ca-file",
        type=Path,
        default=Path("/run/secrets/corporate_ca"),
        help=(
            "build時に追加する公開CA PEM。既定: /run/secrets/corporate_ca。"
            "未存在/空ならskip。OS/JREの信頼先を更新するため書込権限が必要"
        ),
    )
    args = parser.parse_args()
    try:
        result = install(args.ca_file)
    except (OSError, ValueError, ssl.SSLError, UnicodeError):
        raise SystemExit(
            "CA setup failed; check public PEM/base trust."
        ) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
