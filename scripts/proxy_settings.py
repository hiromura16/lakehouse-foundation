#!/usr/bin/env python3
"""Validate private proxy settings and prepare a Docker systemd drop-in."""

import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def fail(message):
    """Report validation failures without printing configuration values."""
    raise SystemExit("ERROR: " + message)


def check_no_symlink(path):
    """Reject redirected configuration paths before reading or writing."""
    for item in (path, *path.parents):
        if item.is_symlink():
            fail("Symlinks are not accepted in configuration paths.")


def atomic_write(path, content, permissions):
    """Publish one managed file with root ownership and private modes."""
    path.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".proxy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(content)
        os.chmod(tmp_name, permissions)
        os.chown(tmp_name, 0, 0)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def main():
    """Initialize, plan, or install files without restarting Docker."""
    mode, source_name, repo_name = sys.argv[1:]
    source = Path(source_name)
    repo = Path(repo_name)
    marker = "# managed-by=lakehouse-foundation-proxy"
    target_env = Path("/etc/lakehouse-foundation/proxy.env")
    target_dropin = Path(
        "/etc/systemd/system/docker.service.d/"
        "90-lakehouse-foundation-proxy.conf"
    )

    # Real proxy credentials belong outside the public source checkout.
    check_no_symlink(source)
    if source.resolve().is_relative_to(repo.resolve()):
        fail("Keep the real proxy file outside the repository.")
    if mode == "init":
        if source.exists():
            fail("Output already exists; it was not changed.")
        source.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if source.parent.stat().st_mode & 0o077:
            fail("Parent directory must be private (chmod 700).")
        with source.open("x", encoding="utf-8") as output:
            output.write((repo / "infra/proxy/proxy.env.example").read_text())
        source.chmod(0o600)
        print("Created private template. Edit it locally, then run --plan.")
        raise SystemExit(0)
    if not source.is_file():
        fail("Input file does not exist.")
    # Ownership and permissions must protect authentication URLs.
    info = source.stat()
    if info.st_mode & 0o077:
        fail("Input must be private: chmod 600 <file>.")
    if info.st_uid not in {
        os.geteuid(),
        int(os.environ.get("SUDO_UID", os.geteuid())),
    }:
        fail("Input must belong to the invoking user or root.")
    # Parse literal env assignments, never shell expressions or expansions.
    allowed = {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    values = {}
    for number, line in enumerate(
        source.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in allowed or key in values:
            fail(
                f"Invalid or duplicate setting at line {number}; "
                "values are not displayed."
            )
        if any(ord(c) <= 32 or ord(c) >= 127 or c in "\\\"'`$" for c in value):
            fail(f"Use literal ASCII URL-encoded values at line {number}.")
        if key.upper() in {"HTTP_PROXY", "HTTPS_PROXY"} and value:
            try:
                url = urlsplit(value)
                port = url.port
            except ValueError:
                fail(f"Invalid proxy URL at line {number}.")
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or port is None
                or url.query
                or url.fragment
                or url.path not in {"", "/"}
            ):
                fail(
                    "Use http(s)://[encoded-user:encoded-password@]host:port "
                    f"at line {number}."
                )
        values[key] = value
    if set(values) != allowed:
        fail(
            "All six upper/lower-case variables must be present, "
            "even if blank."
        )
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        if values[key] != values[key.lower()]:
            fail(f"{key} and its lowercase form must match.")
    if not values["NO_PROXY"]:
        fail("NO_PROXY must list internal service names and addresses.")
    if mode == "plan":
        print("Validated six settings; secret values were not displayed.")
        print(
            "Plan: install /etc/lakehouse-foundation/proxy.env "
            "(root:root 0600)."
        )
        print(
            "Plan: install 90-lakehouse-foundation-proxy.conf "
            "(root:root 0644)."
        )
        print(
            "Docker remains running with its current configuration "
            "until an explicit restart."
        )
        raise SystemExit(0)
    if os.geteuid() != 0:
        fail("--apply requires sudo/root.")
    # Daemon configuration takes precedence over environment variables.
    # Require reconciliation instead of installing an ignored setup.
    daemon_json = Path("/etc/docker/daemon.json")
    if daemon_json.exists():
        import json

        try:
            daemon = json.loads(daemon_json.read_text())
        except (ValueError, PermissionError):
            fail(
                "Cannot inspect existing daemon.json; "
                "review it before applying."
            )
        if daemon.get("proxies"):
            fail(
                "daemon.json already defines proxies. "
                "Reconcile it manually first."
            )
    # Overwrite only files carrying this tool's ownership marker.
    for path in (target_env, target_dropin):
        check_no_symlink(path)
        if path.exists() and not path.read_text().startswith(marker + "\n"):
            fail("An unmanaged destination exists; it was not changed.")

    # Write files only; operators choose a Docker restart window.
    body = (
        marker
        + "\n"
        + "".join(key + "=" + values[key] + "\n" for key in sorted(values))
    )
    atomic_write(target_env, body, 0o600)
    atomic_write(
        target_dropin,
        (repo / "infra/proxy/docker-proxy.conf.example").read_text(),
        0o644,
    )
    print("Installed managed proxy files. Docker has NOT been restarted.")
    print(
        "During a maintenance window: sudo systemctl daemon-reload; "
        "sudo systemctl restart docker"
    )
    print(
        "To disable later, use blank HTTP(S)_PROXY values and reapply/restart."
    )


if __name__ == "__main__":
    main()
