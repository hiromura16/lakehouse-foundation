#!/usr/bin/env python3
"""Generate private site settings and secrets without displaying values."""

import argparse
import base64
import os
import secrets
from pathlib import Path

from settings import ROOT

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", required=True, type=Path)
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
    "COMPOSE_PROFILES": "",
    "WAREHOUSE_NAME": "lakehouse",
    "S3_BUCKET_RAW": "lakehouse-raw",
    "S3_BUCKET_WAREHOUSE": "lakehouse-warehouse",
    "S3_ACCESS_KEY": secrets.token_hex(16),
    "S3_SECRET_KEY": secrets.token_hex(32),
    "CATALOG_DB_PASSWORD": secrets.token_hex(32),
    "CATALOG_ENCRYPTION_KEY": secrets.token_hex(32),
    "WORKER_TOKEN": secrets.token_hex(32),
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
    "DOKPLOY_BIND_IP": "",
    "DOKPLOY_ALLOWED_CIDRS": "",
    "DOKPLOY_ADMIN_PORT": "3100",
    "SWARM_ADVERTISE_ADDR": "",
    "SWARM_ADDRESS_POOL": "172.30.0.0/16",
    "SWARM_SUBNET_SIZE": "24",
    "DOCKER_BRIDGE_CIDR": "",
    "HTTP_PROXY": "",
    "HTTPS_PROXY": "",
    "NO_PROXY": (
        "localhost,127.0.0.1,::1,storage,catalog,catalog-db,trino,worker,"
        "n8n,n8n-db,prometheus,grafana,airflow,airflow-db,"
        "dokploy,dokploy-postgres"
    ),
}
for key in ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"]:
    values[key.lower()] = values[key]
# Exclusive creation prevents replacing an existing site's credentials.
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as output:
    output.write(
        "# Private site settings. Keep outside Git; "
        "do not paste contents into logs.\n"
    )
    output.write(
        "\n".join(key + "=" + value for key, value in values.items()) + "\n"
    )
print(
    "Created private configuration (0600). Edit site-specific paths, "
    "ports and optional source settings locally."
)
