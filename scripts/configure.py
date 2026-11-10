#!/usr/bin/env python3
"""Generate private site settings and secrets without displaying values."""

import argparse
import base64
import os
import secrets
import tempfile
from pathlib import Path

from settings import BYPASS_KEYS, ROOT, load, synchronize_no_proxy

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", required=True, type=Path)
parser.add_argument(
    "--upgrade",
    action="store_true",
    help="Add missing keys, retaining existing values.",
)
args = parser.parse_args()
target = args.output.expanduser().absolute()
if target.resolve().is_relative_to(ROOT):
    raise SystemExit("Choose an env file outside the repository.")
for parent in (target, *target.parents):
    if parent.is_symlink():
        raise SystemExit("Configuration paths must not use symlinks.")
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
    "N8N_PORT": "5678",
    "AIRFLOW_PORT": "8081",
    "SPARK_MASTER_UI_PORT": "8082",
    "SPARK_WORKER_UI_PORT": "8085",
    "SPARK_HISTORY_UI_PORT": "18080",
    "DEMO_GUI_PORT": "8501",
    "DEMO_API_PORT": "8502",
    "CDC_CONNECT_PORT": "8083",
    "CDC_UI_PORT": "8084",
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
    "SOURCE_SQLSERVER_CA_FILE": "",
    "SOURCE_SQLSERVER_TIMEZONE": "UTC",
    "DEMO_SQLSERVER_HOST": "",
    "DEMO_SQLSERVER_PORT": "1433",
    "DEMO_SQLSERVER_DATABASE": "",
    "DEMO_SQLSERVER_USER": "",
    "DEMO_SQLSERVER_PASSWORD": "",
    "DEMO_SQLSERVER_CA_FILE": "",
    "DEMO_SQLSERVER_HOSTNAME_IN_CERTIFICATE": "",
    "CDC_CONFIG_DIR": str(target.parent / "cdc"),
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
    "SWARM_ADVERTISE_ADDR": "",
    "SWARM_ADDRESS_POOL": "172.30.0.0/16",
    "SWARM_SUBNET_SIZE": "24",
    "DOCKER_BRIDGE_CIDR": "",
    "HTTP_PROXY": "",
    "HTTPS_PROXY": "",
}
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
    backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
    # Exclusive creation never replaces an existing site's credentials.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write("# Private site settings. Keep outside Git.\n")
        output.write(
            "\n".join(key + "=" + value for key, value in values.items())
        )
        output.write("\n")
    print(
        "Created private configuration (0600). Edit site paths and profiles."
    )
