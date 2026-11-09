"""Read a private Compose env file without evaluating shell code."""

import base64
import binascii
import ipaddress
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path):
    """Parse private settings literally and validate deployment boundaries."""
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(ROOT) or not path.is_file():
        raise ValueError("Use a private env file outside the repository.")
    if path.stat().st_mode & 0o077:
        raise ValueError("Env file must have mode 0600 or stricter.")
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if (
            not sep
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or key in values
        ):
            raise ValueError(
                f"Invalid assignment at line {number}; "
                "values are not displayed."
            )
        # Strip Compose literal quoting without evaluating shell syntax.
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        if any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError(f"Use ASCII settings at line {number}.")
        values[key] = value
    if not re.fullmatch(
        r"[a-z][a-z0-9_-]{0,40}", values.get("COMPOSE_PROJECT_NAME", "")
    ):
        raise ValueError("Invalid COMPOSE_PROJECT_NAME.")
    address = ipaddress.ip_address(values.get("BIND_IP", ""))
    if address.version != 4 or address.is_unspecified or address.is_global:
        raise ValueError(
            "BIND_IP must be a specific local/private IPv4 address."
        )
    for key in [
        "TRINO_PORT",
        "CATALOG_PORT",
        "S3_PORT",
        "GRAFANA_PORT",
        "PROMETHEUS_PORT",
        "N8N_PORT",
        "AIRFLOW_PORT",
    ]:
        if (
            not values.get(key, "").isdigit()
            or not 1024 <= int(values[key]) <= 65535
        ):
            raise ValueError("Invalid port: " + key)
    ports = [
        values[k]
        for k in values
        if k.endswith("_PORT") and k != "SOURCE_SQLSERVER_PORT" and values[k]
    ]
    if len(ports) != len(set(ports)):
        raise ValueError("Host service ports must be distinct.")
    for key in [
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
        "CATALOG_DB_PASSWORD",
        "CATALOG_ENCRYPTION_KEY",
        "WORKER_TOKEN",
        "N8N_DB_PASSWORD",
        "N8N_ENCRYPTION_KEY",
        "GRAFANA_ADMIN_PASSWORD",
        "AIRFLOW_DB_PASSWORD",
        "AIRFLOW_JWT_SECRET",
    ]:
        if not re.fullmatch(r"[A-Za-z0-9]{20,128}", values.get(key, "")):
            raise ValueError("Generate a fresh secret for " + key)
    # Airflow requires a URL-safe base64 key encoding exactly 32 bytes.
    try:
        fernet_key = base64.b64decode(
            values.get("AIRFLOW_FERNET_KEY", ""),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error):
        raise ValueError("Invalid AIRFLOW_FERNET_KEY encoding.") from None
    if len(fernet_key) != 32:
        raise ValueError("AIRFLOW_FERNET_KEY must encode exactly 32 bytes.")
    for key in ["WAREHOUSE_NAME", "S3_BUCKET_RAW", "S3_BUCKET_WAREHOUSE"]:
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,50}", values.get(key, "")):
            raise ValueError("Invalid name: " + key)
    landing = Path(values.get("LANDING_DIR", ""))
    if not landing.is_absolute() or landing.resolve().is_relative_to(ROOT):
        raise ValueError(
            "LANDING_DIR must be an absolute directory "
            "outside the source tree."
        )
    profiles = set(filter(None, values.get("COMPOSE_PROFILES", "").split(",")))
    if not profiles <= {
        "automation",
        "monitoring",
        "compute",
        "orchestration",
    }:
        raise ValueError(
            "Profiles may be automation, monitoring, compute, "
            "and/or orchestration."
        )
    return values


def environment(values):
    """Overlay validated site settings on the subprocess environment."""
    result = dict(os.environ)
    result.update(values)
    return result
