#!/usr/bin/env python3
"""Plan or run official pinned validators in disposable isolated containers.

No service lifecycle operation, bind mount, Docker socket grant, host privilege
or network is supplied to a validation container. Only public checked-in
configuration is copied into its stopped filesystem. Running this command
requires authorized disposable Docker execution. Plan is the default.
"""

import argparse
import json
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "infra/monitoring"


def jobs():
    """Resolve validators from the same immutable locks as deployed images."""
    locks = json.loads((ROOT / "infra/lakehouse/images.lock.json").read_text())
    image = locks["prometheus"]["pinned"]
    return [
        (
            "prometheus-config",
            image,
            "/bin/promtool",
            ["check", "config", "/validation/prometheus.yml"],
        ),
        (
            "prometheus-rules",
            image,
            "/bin/promtool",
            ["check", "rules", "/validation/rules/foundation.yml"],
        ),
        (
            "prometheus-fixtures",
            image,
            "/bin/promtool",
            ["test", "rules", "/validation/rules/tests.yml"],
        ),
    ]


def execute(job):
    """Bound execution and hide log bodies; report failed cleanup."""
    name, image, entrypoint, arguments = job
    container = None
    outcome = {"validator": name, "status": "not_run"}
    paths = ("prometheus.yml", "rules/foundation.yml", "rules/tests.yml")
    for relative in paths:
        path = MONITORING / relative
        if path.is_symlink() or path.parent.is_symlink():
            return {"validator": name, "status": "blocked-public-symlink"}
    staging = None
    try:
        staging = tempfile.TemporaryDirectory(prefix="lh-public-validator-")
        public = Path(staging.name) / "validation"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        rules = public / "rules"
        rules.mkdir(mode=0o755)
        rules.chmod(0o755)
        for relative in paths:
            target = public / relative
            shutil.copyfile(MONITORING / relative, target)
            # Only fresh public scratch files, never operator host state.
            target.chmod(0o644)
        result = subprocess.run(
            [
                "docker",
                "create",
                "--name",
                "lh-validator-" + secrets.token_hex(8),
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--user",
                "65534:65534",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=32m",
                "--entrypoint",
                entrypoint,
                image,
                *arguments,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=180,
            check=False,
        )
        candidate = result.stdout.strip()
        if result.returncode or not re.fullmatch(rb"[0-9a-f]{64}", candidate):
            outcome["status"] = "container-create-failed"
            return outcome
        container = candidate.decode("ascii")
        copied = subprocess.run(
            ["docker", "cp", str(public), container + ":/validation"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if copied.returncode:
            outcome["status"] = "public-config-copy-failed"
            return outcome
        result = subprocess.run(
            ["docker", "start", "--attach", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
        outcome["status"] = "passed" if result.returncode == 0 else "failed"
        return outcome
    except (OSError, subprocess.TimeoutExpired):
        outcome["status"] = "unavailable-or-timeout"
        return outcome
    finally:
        if staging is not None:
            try:
                staging.cleanup()
            except OSError:
                outcome["status"] = "cleanup-failed"
        if container is not None:
            # Immutable ID identifies only the container created in this call.
            try:
                cleanup = subprocess.run(
                    ["docker", "rm", "--force", container],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                if cleanup.returncode:
                    outcome["status"] = "cleanup-failed"
            except (OSError, subprocess.TimeoutExpired):
                outcome["status"] = "cleanup-failed"


def main():
    """Default to a public plan; never confuse planned checks with passes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    selected = jobs()
    if not args.run:
        print(
            json.dumps(
                {
                    "status": "not_run",
                    "validators": [
                        {
                            "name": j[0],
                            "image": j[1],
                            "entrypoint": j[2],
                            "args": j[3],
                        }
                        for j in selected
                    ],
                }
            )
        )
        return
    results = [execute(job) for job in selected]
    print(json.dumps({"results": results}))
    if any(item["status"] != "passed" for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
