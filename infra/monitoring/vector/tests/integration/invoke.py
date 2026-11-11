#!/usr/bin/env python3
"""Plan by default; explicitly run a disposable official-binary trial.

This is an operator opt-in, never a deployment/CI prerequisite. It builds only
public context, then runs without network, host mounts or an admin socket,
ports, capabilities or root. No actual run is performed by source unit tests.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
DIRECTORY = Path(__file__).resolve().parent
IMAGE_PREFIX = "lakehouse-vector-isolated:"


def plan():
    """Make isolation and incomplete production acceptance machine-readable."""
    return {
        "schema_version": 1,
        "action": "plan",
        "requires_explicit_operator_run": True,
        "network": "none",
        "host_mounts": [],
        "published_ports": [],
        "docker_socket": False,
        "container_user": "10001:10001",
        "memory": "1g",
        "cpus": "1",
        "temporary_storage": "160m",
        "buffer_variant": "eight_event_memory_not_production_disk",
        "actual_openobserve": "not_run_http_stub_only",
        "production_disk_saturation": "not_run",
        "binary_layout_compatibility": "pending_runtime",
        "company_acceptance": "not_run",
    }


def commands(config, image, context, name):
    """Select a local CLI and confined disposable-container options."""
    if not re.fullmatch(r"lakehouse-vector-isolated:[0-9a-f]{16}", image):
        raise ValueError("ISOLATED_IMAGE_NAME_INVALID")
    if not re.fullmatch(r"lakehouse-vector-trial-[0-9a-f]{16}", name):
        raise ValueError("ISOLATED_CONTAINER_NAME_INVALID")
    docker = ["docker", "--config", str(config), "--context", "default"]
    return (
        docker
        + [
            "build",
            "--file",
            str(Path(context) / "Dockerfile"),
            "--tag",
            image,
            str(context),
        ],
        docker
        + [
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=160m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "10001:10001",
            "--pids-limit",
            "128",
            "--memory",
            "1g",
            "--cpus",
            "1",
            "--ipc",
            "none",
            image,
        ],
    )


def public_context(destination):
    """Copy a fixed public allowlist; never send the checkout to Docker."""
    selected = [
        "infra/monitoring/vector/common.yaml",
        "infra/monitoring/vector/loki.yaml",
        "infra/monitoring/vector/openobserve.yaml",
        "infra/monitoring/vector/tests/integration/probe.py",
        "infra/monitoring/loki/loki.yaml",
    ]
    selected += [
        str(path.relative_to(ROOT))
        for path in (ROOT / "infra/monitoring/vector/transforms").glob("*.vrl")
    ]
    for relative in selected:
        source = ROOT / relative
        if source.is_symlink() or any(
            parent.is_symlink() for parent in source.parents
        ):
            raise ValueError("PUBLIC_CONTEXT_SYMLINK")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        shutil.copyfile(source, target)
        target.chmod(0o644)
    shutil.copyfile(DIRECTORY / "Dockerfile", destination / "Dockerfile")


def run_trial():
    """Execute only after --run, hide process bodies and bound both steps."""
    # The private checkout itself never becomes the build context. The image
    # tag is isolated from application tags; no cache is deleted.
    identifier = hashlib.sha256(
        (DIRECTORY / "Dockerfile").read_bytes()
        + (DIRECTORY / "probe.py").read_bytes()
    ).hexdigest()[:16]
    with tempfile.TemporaryDirectory(prefix="vector-isolated-cli-") as scratch:
        config = Path(scratch) / "cli"
        config.mkdir(mode=0o700)
        context = Path(scratch) / "public"
        context.mkdir(mode=0o755)
        public_context(context)
        name = "lakehouse-vector-trial-" + secrets.token_hex(8)
        build, run = commands(config, IMAGE_PREFIX + identifier, context, name)
        result = subprocess.run(
            build,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath},
            timeout=300,
            check=False,
        )
        if result.returncode != 0:
            return {**plan(), "action": "run", "status": "build_failed"}
        try:
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(
                    run,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    env={"PATH": os.defpath},
                    timeout=300,
                    check=False,
                )
                output.seek(0)
                encoded = output.read(65537)
                if len(encoded) > 65536:
                    raise ValueError("ISOLATED_RESULT_LIMIT")
        finally:
            # Exact random disposable name only; never a service/app name.
            cleanup = subprocess.run(
                [
                    "docker",
                    "--config",
                    str(config),
                    "--context",
                    "default",
                    "rm",
                    "--force",
                    name,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={"PATH": os.defpath},
                timeout=15,
                check=False,
            )
            # --rm may already have removed a completed trial. A nonzero
            # cleanup is not silently accepted: absence must be established.
            if cleanup.returncode != 0:
                absent = subprocess.run(
                    [
                        "docker",
                        "--config",
                        str(config),
                        "--context",
                        "default",
                        "container",
                        "ls",
                        "--all",
                        "--filter",
                        "name=^/" + name + "$",
                        "--format",
                        "{{.Names}}",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env={"PATH": os.defpath},
                    timeout=15,
                    check=False,
                )
                if absent.returncode != 0 or absent.stdout.strip():
                    raise ValueError("ISOLATED_CONTAINER_CLEANUP_FAILED")
        try:
            summary = json.loads(encoded)
        except (UnicodeError, ValueError):
            raise ValueError("ISOLATED_RESULT_INVALID") from None
        if not isinstance(summary, dict) or summary.get("schema_version") != 1:
            raise ValueError("ISOLATED_RESULT_INVALID")
        summary["container_exit_code"] = result.returncode
        if result.returncode != 0:
            # A process may print its last record before failing. Both the
            # structured result and the actual exit status must prove success.
            summary["status"] = "container_failed"
        return summary


def main():
    """Avoid Docker even for inspection unless the operator selects --run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    options = parser.parse_args()
    try:
        result = run_trial() if options.run else plan()
    except (OSError, ValueError, subprocess.SubprocessError):
        result = {
            **plan(),
            "action": "run",
            "status": "failed_review_required",
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if not options.run or result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
