#!/usr/bin/env python3
"""Operate a portable Compose foundation using external private settings."""

import argparse
import json
import os
import socket
import subprocess
from pathlib import Path

from settings import ROOT, environment, load

parser = argparse.ArgumentParser(
    description=__doc__,
    epilog=(
        "private設定から9つのComposeを同じprojectへ結合します。"
        "planは構成検査だけ、source-checkはTCP到達だけです。"
        "起動停止はCLIかDokployの一つへ統一し、同じ環境を別名で起動しません。"
    ),
)
# An explicit flag overrides the shell's default site selection.
parser.add_argument(
    "--project-name",
    help="spark-checkだけの明示的な既存project。env-fileの秘密値は保持",
)
parser.add_argument(
    "--local-only",
    action="store_true",
    help="spark-checkでcluster readinessを省略しlocal[2]互換性だけを確認",
)
parser.add_argument(
    "--result-dir",
    type=Path,
    help="spark-check結果の既存private directory（0700）。省略時はstdoutのみ",
)
parser.add_argument(
    "--apply",
    action="store_true",
    help="notebook-workspaceだけの明示権限準備。通常planは変更しない",
)
parser.add_argument(
    "--probe",
    action="store_true",
    help="notebook-workspaceだけの明示非root一時write/read/delete probe",
)
parser.add_argument(
    "--env-file",
    default=os.environ.get("LAKEHOUSE_ENV_FILE") or None,
    type=Path,
    help="対象環境のGit外site.env。省略時はLAKEHOUSE_ENV_FILEを使う",
)
parser.add_argument(
    "action",
    choices=[
        "plan",
        "predeploy",
        "notebook-workspace",
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
    help=(
        "plan/status/source-check=検査。up/down=起動停止（volume保持）。"
        "ingest/medallion=原本取込/変換。verify=隔離検証。"
        "spark-check=互換smoke、grafana-init=初回dashboard作成"
    ),
)
args = parser.parse_args()
if args.action != "spark-check" and (
    args.project_name is not None or args.local_only or args.result_dir
):
    parser.error("Project/local-only/result options are spark-check only.")
if args.action != "notebook-workspace" and (args.apply or args.probe):
    parser.error("Apply/probe options are notebook-workspace only.")
if args.env_file is None:
    parser.error("Provide --env-file or set LAKEHOUSE_ENV_FILE.")
try:
    values = load(args.env_file)
except (ValueError, OSError) as error:
    if args.action == "spark-check":
        from compute_check import settings_failure

        raise SystemExit(
            settings_failure(
                local_only=args.local_only, result_dir=args.result_dir
            )
        ) from None
    raise SystemExit(str(error)) from None
if args.action == "spark-check":
    from compute_check import selected_project, settings_failure

    try:
        values = selected_project(values, args.project_name)
    except ValueError:
        raise SystemExit(
            settings_failure(
                local_only=args.local_only, result_dir=args.result_dir
            )
        ) from None
compose = [
    "docker",
    "compose",
    "--env-file",
    str(args.env_file.resolve()),
    "-f",
    str(ROOT / "infra/lakehouse/compose.yaml"),
]
# Optional engines stay in separate Compose files to keep the core small.
for extension in (
    "compute",
    "orchestration",
    "demo",
    "cdc",
    "poc",
    "mcp",
    "openobserve",
    "sail",
):
    extra_compose = ROOT / "infra" / extension / "compose.yaml"
    if extra_compose.is_file():
        compose.extend(["-f", str(extra_compose)])
# Override inherited shell interpolation, including old case variants.
# This also exports the dedicated key for raw Compose's canonical branch.
env = environment(values)
if args.action == "spark-check":
    # Match service labels, interpolated image tags and default volume names.
    # No other lifecycle action accepts this narrow override.
    compose[2:2] = ["--project-name", values["COMPOSE_PROJECT_NAME"]]


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
elif args.action == "notebook-workspace":
    from notebook_workspace import prepare_workspace

    result = prepare_workspace(values, apply=args.apply, probe=args.probe)
    print(json.dumps(result))
    if result.get("action") == "blocked" or (
        args.probe
        and result.get("enabled")
        and result.get("probe") != "passed"
    ):
        raise SystemExit(1)
elif args.action == "predeploy":
    from notebook_workspace import inspect_workspace

    run(["config", "--quiet"])
    result = inspect_workspace(values)
    print(json.dumps(result))
    if result.get("enabled") and result.get("action") != "none":
        raise SystemExit(1)
elif args.action == "up":
    if not Path(values["LANDING_DIR"]).is_dir():
        raise SystemExit(
            "Create LANDING_DIR with the intended owner "
            "before starting services."
        )
    if "notebooks" in values.get("COMPOSE_PROFILES", "").split(","):
        from notebook_workspace import inspect_workspace

        if inspect_workspace(values).get("action") != "none":
            raise SystemExit(
                "Review notebook-workspace plan and approved apply/probe "
                "first."
            )
    run(["up", "-d", "--build", "--wait", "--wait-timeout", "600"])
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
        if "dashboards" in result:
            print(json.dumps(result))
        if result["status"] == "partial":
            raise SystemExit(
                "Some dashboard seeds failed; existing UIDs kept."
            )
elif args.action == "grafana-init":
    from grafana_setup import setup

    result = setup(values)
    print("Grafana dashboard: " + result["status"])
    if "dashboards" in result:
        print(json.dumps(result))
    if result["status"] == "partial":
        raise SystemExit("Some dashboard seeds failed; existing UIDs kept.")
elif args.action == "spark-check":
    from compute_check import run_check

    raise SystemExit(
        run_check(
            compose,
            values,
            env,
            local_only=args.local_only,
            result_dir=args.result_dir,
        )
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
