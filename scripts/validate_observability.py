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
    selected = [
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

    if "loki" in locks:
        selected.append(
            (
                "loki-config",
                locks["loki"]["pinned"],
                "/usr/bin/loki",
                [
                    "-config.file=/validation/loki/loki.yaml",
                    "-verify-config=true",
                ],
            )
        )
    if "vector" in locks:
        for suffix, arguments in (
            (
                "loki",
                ["validate", "--no-environment", "common.yaml", "loki.yaml"],
            ),
            (
                "fanout",
                [
                    "validate",
                    "--no-environment",
                    "common.yaml",
                    "loki.yaml",
                    "openobserve.yaml",
                ],
            ),
            (
                "fixtures",
                [
                    "test",
                    "common.yaml",
                    "loki.yaml",
                    "tests/native-tests.yaml",
                ],
            ),
        ):
            selected.append(
                (
                    "vector-" + suffix,
                    locks["vector"]["pinned"],
                    "/usr/bin/vector",
                    arguments,
                )
            )
    return selected


def execute(job, *, public_diagnostics=False):
    """Bound execution and hide log bodies; report failed cleanup."""
    name, image, entrypoint, arguments = job
    container = None
    outcome = {"validator": name, "status": "not_run"}
    if name.startswith("vector-"):
        paths = tuple(
            str(p.relative_to(MONITORING))
            for p in (MONITORING / "vector").rglob("*")
            if p.is_file() and p.suffix in {".yaml", ".vrl", ".json"}
        )
    elif name.startswith("loki-"):
        paths = ("loki/loki.yaml",)
    else:
        paths = ("prometheus.yml", "rules/foundation.yml", "rules/tests.yml")
    for relative in paths:
        path = MONITORING / relative
        if path.is_symlink() or any(
            parent.is_symlink()
            for parent in path.parents
            if parent.is_relative_to(MONITORING)
        ):
            return {"validator": name, "status": "blocked-public-symlink"}
    staging = None
    try:
        staging = tempfile.TemporaryDirectory(prefix="lh-public-validator-")
        public = Path(staging.name) / "validation"
        public.mkdir(mode=0o755)
        public.chmod(0o755)
        for relative in paths:
            target = public / relative
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            for parent in target.parents:
                if parent == public:
                    break
                parent.chmod(0o755)
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
                "--log-driver",
                "json-file",
                "--log-opt",
                "max-size=1m",
                "--log-opt",
                "max-file=1",
                "--user",
                "65534:65534",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=32m",
                "--workdir",
                "/validation/vector" if name.startswith("vector-") else "/",
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
        if name == "vector-fanout":
            # Public empty-token file is configuration syntax evidence only.
            # It cannot authenticate and never contacts an OpenObserve server.
            tokens = Path(staging.name) / "secrets"
            tokens.mkdir(mode=0o755)
            tokens.chmod(0o755)
            token_file = tokens / "openobserve-vector.json"
            token_file.write_text('{"token":""}\n')
            token_file.chmod(0o644)
            copied = subprocess.run(
                ["docker", "cp", str(tokens), container + ":/run/secrets"],
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
        if result.returncode and public_diagnostics:
            # Only this fresh, network-none validator receives public source
            # plus the empty synthetic token above. Never read application
            # logs or reuse an operator container/real private configuration.
            diagnostic = subprocess.run(
                ["docker", "logs", "--tail", "100", container],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=5,
                check=False,
            )
            if diagnostic.returncode == 0:
                outcome["public_diagnostic"] = diagnostic.stdout.decode(
                    "utf-8", errors="replace"
                )[-8192:]
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
    parser.add_argument(
        "--public-diagnostics",
        action="store_true",
        help="失敗した使い捨てvalidatorの公開入力だけの診断を最大8KiB表示",
    )
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
    results = [
        execute(job, public_diagnostics=args.public_diagnostics)
        for job in selected
    ]
    print(json.dumps({"results": results}))
    if any(item["status"] != "passed" for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
