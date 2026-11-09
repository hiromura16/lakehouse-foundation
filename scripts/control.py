#!/usr/bin/env python3
"""Operate a portable Compose foundation using external private settings."""

import argparse
import os
import socket
import subprocess
from pathlib import Path

from settings import ROOT, environment, load

parser = argparse.ArgumentParser(description=__doc__)
# An explicit flag overrides the shell's default site selection.
parser.add_argument(
    "--env-file",
    default=os.environ.get("LAKEHOUSE_ENV_FILE") or None,
    type=Path,
)
parser.add_argument(
    "action",
    choices=[
        "plan",
        "up",
        "verify",
        "ingest",
        "medallion",
        "down",
        "status",
        "source-check",
        "spark-check",
        "grafana-init",
    ],
)
args = parser.parse_args()
if args.env_file is None:
    parser.error("Provide --env-file or set LAKEHOUSE_ENV_FILE.")
try:
    values = load(args.env_file)
except (ValueError, OSError) as error:
    raise SystemExit(str(error)) from None
compose = [
    "docker",
    "compose",
    "--env-file",
    str(args.env_file.resolve()),
    "-f",
    str(ROOT / "infra/lakehouse/compose.yaml"),
]
# Optional engines stay in separate Compose files to keep the core small.
for extension in ("compute", "orchestration", "demo", "cdc"):
    extra_compose = ROOT / "infra" / extension / "compose.yaml"
    if extra_compose.is_file():
        compose.extend(["-f", str(extra_compose)])
env = environment(values)


def run(parts, **kwargs):
    """Run Compose with the selected private environment and fail clearly."""
    return subprocess.run(compose + parts, env=env, check=True, **kwargs)


if args.action == "source-check":
    host = values.get("SOURCE_SQLSERVER_HOST")
    if not host:
        raise SystemExit(
            "Set SOURCE_SQLSERVER_HOST in the private settings first."
        )
    # TCP reachability does not test SQL login, certificates, or CDC.
    with socket.create_connection(
        (host, int(values.get("SOURCE_SQLSERVER_PORT", "1433"))), timeout=5
    ):
        print(
            "PASS: external SQL Server TCP endpoint reachable. "
            "Authentication/CDC were not tested."
        )
elif args.action == "plan":
    run(["config", "--quiet"])
    print(
        "Compose configuration valid. Profiles: "
        + (values.get("COMPOSE_PROFILES") or "core only")
    )
elif args.action == "up":
    if not Path(values["LANDING_DIR"]).is_dir():
        raise SystemExit(
            "Create LANDING_DIR with the intended owner "
            "before starting services."
        )
    run(["up", "-d", "--build", "--wait", "--wait-timeout", "240"])
    run(
        [
            "exec",
            "-T",
            "worker",
            "python",
            "/opt/lakehouse/admin.py",
            "bootstrap",
        ]
    )
    if "monitoring" in values.get("COMPOSE_PROFILES", "").split(","):
        # Seed once after startup; never replace a dashboard edited in the GUI.
        from grafana_setup import setup

        result = setup(values)
        print("Grafana dashboard: " + result["status"])
elif args.action == "grafana-init":
    from grafana_setup import setup

    result = setup(values)
    print("Grafana dashboard: " + result["status"])
elif args.action == "spark-check":
    # Core bootstrap must finish before this isolated Iceberg smoke job runs.
    run(
        [
            "--profile",
            "compute",
            "run",
            "--rm",
            "--build",
            "--no-deps",
            "spark-check",
        ]
    )
elif args.action in {"verify", "ingest", "medallion"}:
    run(
        [
            "exec",
            "-T",
            "worker",
            "python",
            "/opt/lakehouse/admin.py",
            args.action,
        ]
    )
elif args.action == "down":
    # Preserve volumes so an ordinary stop cannot erase stored data.
    run(["down"])
    print("Containers stopped; named data volumes were preserved.")
else:
    run(["ps"])
