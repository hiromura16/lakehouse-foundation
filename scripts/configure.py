#!/usr/bin/env python3
"""Generate private site settings and secrets without displaying values."""

import argparse
import base64
import os
import re
import secrets
import tempfile
from pathlib import Path

from settings import BYPASS_KEYS, ROOT, load, synchronize_no_proxy

parser = argparse.ArgumentParser(
    description=__doc__,
    epilog=(
        "新規のsite.envと秘密値をGit外へ生成します。配置やDB更新は行いません。"
        "既存環境では--upgradeを使い、DBと暗号鍵の生成し直しを避けます。"
    ),
)
parser.add_argument(
    "--output",
    required=True,
    type=Path,
    help="Git外のsite.env保存先。親0700・file0600。既存設定の新規生成は拒否",
)
parser.add_argument(
    "--upgrade",
    action="store_true",
    help=(
        "既存の秘密値を保持して欠落keyを追加し、NO_PROXYを正規化する。"
        "変更前backupは上書きしない"
    ),
)
parser.add_argument(
    "--preset",
    choices=["parallel-lab"],
    help=(
        "新しい比較PoC専用の初期設定。既定port+20000、loopback限定、"
        "demo/monitoring/bi/automationから段階起動。配置は行わない"
    ),
)
parser.add_argument(
    "--data-root",
    type=Path,
    help="parallel-lab専用の未使用なGit外絶対パス。作成・権限変更はしない",
)
parser.add_argument(
    "--project-name",
    help="parallel-lab専用の新しいCompose project。既存projectは指定しない",
)
args = parser.parse_args()
target = args.output.expanduser().absolute()
if target.resolve().is_relative_to(ROOT):
    raise SystemExit("Choose an env file outside the repository.")
for parent in (target, *target.parents):
    if parent.is_symlink():
        raise SystemExit("Configuration paths must not use symlinks.")


def parallel_lab_overrides():
    """Select fresh lab paths and ports without touching data or Docker.

    This preset never reads another deployment's settings. It deliberately
    leaves source DB connections and gated labs disabled. Checking that a
    project/port is free still requires the owning manager before deployment.
    The filesystem checks are preparation-time checks, not a race-safe host
    workspace apply; existing no-follow host preflight remains mandatory.
    """
    if not args.preset:
        if args.data_root is not None or args.project_name is not None:
            parser.error("Use data-root/project-name only with --preset.")
        return {}
    if args.upgrade:
        parser.error("A parallel-lab preset is new-only; never use --upgrade.")
    if ".." in target.parts or target.anchor != "/":
        parser.error("New lab settings paths must use a plain absolute path.")
    if args.data_root is None or args.project_name is None:
        parser.error("The preset requires --data-root and --project-name.")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", args.project_name):
        parser.error("Invalid new project name.")
    if args.project_name == "lakehouse" or os.getuid() == 0:
        parser.error("Use a new project name and a non-root operator.")
    root = args.data_root.expanduser()
    if (
        root.anchor != "/"
        or ".." in root.parts
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", str(root))
    ):
        parser.error("The new data root must be an external absolute path.")
    # Reject broad roots, checkout/config overlap, reused data and symlinks.
    # Do not repair or even create the selected data root in a generator.
    protected = {Path("/"), Path("/srv"), Path("/tmp"), Path("/var")}
    if root in protected or any(
        boundary.is_relative_to(root) or root.is_relative_to(boundary)
        for boundary in (ROOT, target.parent)
    ):
        parser.error("Keep new data, checkout and private settings separate.")
    if Path.home().is_relative_to(root):
        parser.error("Do not select an operator home or its ancestor.")
    if {".config", ".ssh", ".aws", ".docker", ".codex"} & set(root.parts):
        parser.error("Do not use a private configuration or credential root.")
    if any(
        root.is_relative_to(Path(path))
        for path in ("/etc", "/proc", "/sys", "/dev", "/run", "/root")
    ):
        parser.error("Do not use a system directory for lab data.")
    for component in (root, *root.parents):
        if component.is_symlink():
            parser.error("New data paths must not use symlinks.")
        if component.exists() and not component.is_dir():
            parser.error("A new data path has a non-directory component.")
    if root.exists() or target.exists():
        parser.error("Use unused data and settings paths; no replacement.")
    return {
        "COMPOSE_PROJECT_NAME": args.project_name,
        "DATA_VOLUME_PREFIX": "",
        "BIND_IP": "127.0.0.1",
        "LANDING_DIR": str(root / "landing"),
        "NOTEBOOKS_DIR": str(root / "notebooks"),
        "MONITORING_LOG_DIR": str(root / "log-spool"),
        "MONITORING_HOST_ALIAS": "onboarding-lab",
        "COMPOSE_PROFILES": "demo,monitoring,bi,automation",
        "CDC_CONNECTOR_NAME": args.project_name + "-source",
        "CDC_TOPIC_PREFIX": args.project_name,
        "CDC_GROUP_ID": args.project_name + "-cdc",
    }


lab_overrides = parallel_lab_overrides()
# Keep real deployment settings outside the checkout and private.
target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
if target.parent.stat().st_mode & 0o077:
    raise SystemExit("The parent directory must have mode 0700.")
values = {
    "COMPOSE_PROJECT_NAME": "lakehouse",
    "DATA_VOLUME_PREFIX": "",
    "BIND_IP": "127.0.0.1",
    "LANDING_DIR": "/srv/lakehouse/landing",
    "WORKER_UID": str(os.getuid()),
    "WORKER_GID": str(os.getgid()),
    "TRINO_PORT": "8080",
    "CATALOG_PORT": "8181",
    "S3_PORT": "8333",
    "GRAFANA_PORT": "3000",
    "PROMETHEUS_PORT": "9090",
    # Only monitoring-host reads the Docker data root; keep opt-in separate.
    "CADVISOR_DOCKER_DATA_ROOT": "",
    "MONITORING_LOG_DIR": "/srv/lakehouse/log-spool",
    "MONITORING_HOST_ALIAS": "local",
    "OPENOBSERVE_PORT": "5080",
    "OPENOBSERVE_ROOT_USER_EMAIL": "admin@example.invalid",
    "OPENOBSERVE_ROOT_USER_PASSWORD": secrets.token_hex(32),
    "OPENOBSERVE_INGEST_TOKEN_FILE": "",
    "N8N_PORT": "5678",
    "AIRFLOW_PORT": "8081",
    "SPARK_MASTER_UI_PORT": "8082",
    "SPARK_WORKER_UI_PORT": "8085",
    "SPARK_HISTORY_UI_PORT": "18080",
    "DEMO_GUI_PORT": "8501",
    "DEMO_API_PORT": "8502",
    "CDC_CONNECT_PORT": "8083",
    "CDC_UI_PORT": "8084",
    # monitoring-host needs monitoring; sail-lab needs compute explicitly.
    "COMPOSE_PROFILES": "",
    "SUPERSET_PORT": "8088",
    "SUPERSET_DB_PASSWORD": secrets.token_hex(32),
    "SUPERSET_SECRET_KEY": secrets.token_hex(32),
    "SUPERSET_ADMIN_USERNAME": "admin",
    "SUPERSET_ADMIN_PASSWORD": secrets.token_hex(32),
    "SUPERSET_ADMIN_EMAIL": "admin@example.invalid",
    "SUPERSET_REDIS_PASSWORD": secrets.token_hex(32),
    "JUPYTER_PORT": "8888",
    "JUPYTER_TOKEN": secrets.token_hex(32),
    "NOTEBOOKS_DIR": "/srv/lakehouse/notebooks",
    "QDRANT_PORT": "6333",
    "QDRANT_API_KEY": secrets.token_hex(32),
    "QDRANT_READ_ONLY_API_KEY": secrets.token_hex(32),
    "MARQUEZ_API_PORT": "5000",
    "MARQUEZ_UI_PORT": "3001",
    "MARQUEZ_DB_PASSWORD": secrets.token_hex(32),
    "MCP_PORT": "8503",
    "MCP_API_TOKEN": secrets.token_hex(32),
    "MCP_ENABLE_MUTATIONS": "false",
    "MCP_ALLOWED_HOSTS": "localhost:*,127.0.0.1:*,mcp:*",
    "CORPORATE_CA_FILE": "",
    "WAREHOUSE_NAME": "lakehouse",
    "S3_BUCKET_RAW": "lakehouse-raw",
    "S3_BUCKET_WAREHOUSE": "lakehouse-warehouse",
    "S3_ACCESS_KEY": secrets.token_hex(16),
    "S3_SECRET_KEY": secrets.token_hex(32),
    "CATALOG_DB_PASSWORD": secrets.token_hex(32),
    "CATALOG_ENCRYPTION_KEY": secrets.token_hex(32),
    "WORKER_TOKEN": secrets.token_hex(32),
    "CONTROL_API_TOKEN": secrets.token_hex(32),
    "CONTROL_SETTINGS_DIR": str(target.parent),
    "CONTROL_SETTINGS_NAME": target.name,
    "N8N_DB_PASSWORD": secrets.token_hex(32),
    "N8N_ENCRYPTION_KEY": secrets.token_hex(32),
    "GRAFANA_ADMIN_PASSWORD": secrets.token_hex(24),
    "AIRFLOW_DB_PASSWORD": secrets.token_hex(32),
    "AIRFLOW_FERNET_KEY": base64.urlsafe_b64encode(
        secrets.token_bytes(32)
    ).decode(),
    "AIRFLOW_JWT_SECRET": secrets.token_hex(32),
    "N8N_HOST": "localhost",
    "N8N_PROTOCOL": "http",
    "N8N_SECURE_COOKIE": "false",
    "N8N_SSRF_PROTECTION_ENABLED": "true",
    "N8N_SSRF_BLOCKED_IP_RANGES": "default,100.64.0.0/10",
    "N8N_SSRF_ALLOWED_IP_RANGES": "",
    "N8N_SSRF_ALLOWED_HOSTNAMES": "worker",
    "N8N_SSRF_BLOCKED_HOSTNAMES": "",
    "N8N_SSRF_DNS_CACHE_MAX_SIZE": "1048576",
    "SOURCE_SQLSERVER_HOST": "",
    "SOURCE_SQLSERVER_PORT": "1433",
    "SOURCE_SQLSERVER_DATABASE": "",
    "SOURCE_SQLSERVER_USER": "",
    "SOURCE_SQLSERVER_PASSWORD": "",
    "SOURCE_SQLSERVER_CONTROL_USER": "",
    "SOURCE_SQLSERVER_CONTROL_PASSWORD": "",
    "SOURCE_SQLSERVER_CA_FILE": "",
    "SOURCE_SQLSERVER_TIMEZONE": "UTC",
    "SOURCE_SQLSERVER_ENCRYPT": "true",
    "SOURCE_SQLSERVER_HOSTNAME_IN_CERTIFICATE": "",
    "SOURCE_SQLSERVER_TRUST_SERVER_CERTIFICATE": "false",
    "SOURCE_SQLSERVER_BATCH_OVERLAP_SECONDS": "300",
    "SOURCE_SQLSERVER_BATCH_SAFETY_LAG_SECONDS": "5",
    "DEMO_SQLSERVER_HOST": "",
    "DEMO_SQLSERVER_PORT": "1433",
    "DEMO_SQLSERVER_DATABASE": "",
    "DEMO_SQLSERVER_USER": "",
    "DEMO_SQLSERVER_PASSWORD": "",
    "DEMO_SQLSERVER_CA_FILE": "",
    "DEMO_SQLSERVER_HOSTNAME_IN_CERTIFICATE": "",
    "DEMO_SQLSERVER_ALLOW_SHARED_SOURCE_DATABASE": "false",
    "DEMO_SQLSERVER_ERP_SCHEMA": "lh_demo_erp",
    "DEMO_SQLSERVER_MES_SCHEMA": "lh_demo_mes",
    "DEMO_SQLSERVER_CONTROL_SCHEMA": "lh_demo_ctl",
    "DEMO_SQLSERVER_INSTALLATION_ID": secrets.token_hex(16),
    "DEMO_SQLSERVER_OWNERSHIP_TOKEN": secrets.token_hex(32),
    "DEMO_SQLSERVER_ENCRYPT": "true",
    "DEMO_SQLSERVER_TRUST_SERVER_CERTIFICATE": "false",
    "CDC_CONFIG_DIR": str(target.parent / "cdc"),
    "CDC_SOURCE_MODE": "generic",
    "CDC_STATE_VOLUME_NAME": "",
    "CDC_SQLSERVER_HOST": "",
    "CDC_SQLSERVER_PORT": "1433",
    "CDC_SQLSERVER_DATABASE": "",
    "CDC_SQLSERVER_USER": "",
    "CDC_SQLSERVER_PASSWORD": "",
    "CDC_SQLSERVER_TRUSTSTORE": "",
    "CDC_SQLSERVER_TRUSTSTORE_FILE": "",
    "CDC_SQLSERVER_TRUSTSTORE_PASSWORD": "",
    "CDC_SQLSERVER_HOSTNAME_IN_CERTIFICATE": "",
    "CDC_SQLSERVER_AGENT_STATUS_QUERY": "",
    "CDC_TOPIC_PREFIX": "factory",
    "CDC_CONNECTOR_NAME": "factory-source",
    "CDC_KAFKA_CLUSTER_ID": base64.urlsafe_b64encode(secrets.token_bytes(16))
    .decode()
    .rstrip("="),
    "CDC_GROUP_ID": "lakehouse-cdc",
    "CDC_BATCH_SIZE": "100",
    "CDC_POLL_SECONDS": "5",
    "CDC_DEMO_OWNERSHIP_TOKEN": "",
    "DOKPLOY_BIND_IP": "",
    "DOKPLOY_ALLOWED_CIDRS": "",
    "DOKPLOY_ADMIN_PORT": "3100",
    "DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN": "1",
    "SWARM_ADVERTISE_ADDR": "",
    "SWARM_ADDRESS_POOL": "172.30.0.0/16",
    "SWARM_SUBNET_SIZE": "24",
    "DOCKER_BRIDGE_CIDR": "",
    "HTTP_PROXY": "",
    "HTTPS_PROXY": "",
}
if lab_overrides:
    # Only application endpoints move. Dokploy itself belongs to its existing
    # manager, and no new daemon/admin/RPC endpoint is introduced here.
    for key in values:
        if key.endswith("_PORT") and key != "DOKPLOY_ADMIN_PORT":
            if key in {
                "SOURCE_SQLSERVER_PORT",
                "DEMO_SQLSERVER_PORT",
                "CDC_SQLSERVER_PORT",
            }:
                continue
            values[key] = str(int(values[key]) + 20000)
    values.update(lab_overrides)
for key in ["HTTP_PROXY", "HTTPS_PROXY"]:
    values[key.lower()] = values[key]
values = synchronize_no_proxy(values)
if args.upgrade:
    # Read and validate before adding defaults; never rotate existing keys.
    normalized = load(target)
    original = target.read_bytes()
    existing = {
        line.partition("=")[0]
        for line in original.decode().splitlines()
        if line and not line.startswith("#")
    }
    missing = {
        key: value for key, value in values.items() if key not in existing
    }
    # Persist the union of old case variants and prior canonical additions.
    bypass = {key: normalized[key] for key in BYPASS_KEYS}
    lines = []
    written = set()
    for line in original.decode().splitlines():
        key = line.partition("=")[0]
        if key in bypass:
            line = key + "=" + bypass[key]
            written.add(key)
        lines.append(line)
    for key in BYPASS_KEYS:
        if key not in written:
            lines.append(key + "=" + bypass[key])
    missing = {
        key: value for key, value in missing.items() if key not in BYPASS_KEYS
    }
    normalized_original = "\n".join(lines) + "\n"
    if not missing and normalized_original.encode() == original:
        print("Settings already include all current keys; no changes made.")
        raise SystemExit(0)
    backup = target.with_name(target.name + ".before-upgrade")
    try:
        backup_fd = os.open(
            backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError:
        # Keep the exclusive open: an existence precheck would race another
        # upgrade. The message gives recovery steps without private paths.
        raise SystemExit(
            "An upgrade backup already exists. Move the .before-upgrade "
            "backup to a private location, then rerun --upgrade. "
            "Existing settings and backup were not changed."
        ) from None
    with os.fdopen(backup_fd, "wb") as output:
        output.write(original)
    candidate = normalized_original
    if missing:
        candidate += "# Added keys; previous values preserved.\n"
        candidate += (
            "\n".join(key + "=" + value for key, value in missing.items())
            + "\n"
        )
    temporary_fd, temporary_name = tempfile.mkstemp(dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(temporary_fd, "w") as output:
            output.write(candidate)
        load(temporary)
        if target.read_bytes() != original:
            raise SystemExit(
                "Settings changed concurrently; review and retry."
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    print("Added missing keys (0600); previous file saved beside settings.")
else:
    candidate = "# Private site settings. Keep outside Git.\n"
    candidate += (
        "\n".join(key + "=" + value for key, value in values.items()) + "\n"
    )
    if lab_overrides:
        # Validate the complete fresh candidate before publishing it. Only
        # this newly generated private file is read; no existing env is used.
        check_fd, check_name = tempfile.mkstemp(dir=target.parent)
        check_path = Path(check_name)
        try:
            with os.fdopen(check_fd, "w") as output:
                output.write(candidate)
            load(check_path)
        except (ValueError, OSError):
            raise SystemExit("New lab settings validation failed.") from None
        finally:
            check_path.unlink(missing_ok=True)
    # Exclusive creation never replaces an existing site's credentials.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(candidate)
    print(
        "Created private configuration (0600). Edit site paths and profiles."
    )
