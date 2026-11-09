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
    # Only migrations need a data name independent of the running project.
    volume_prefix = values.get("DATA_VOLUME_PREFIX", "")
    if volume_prefix and not re.fullmatch(
        r"[a-z][a-z0-9_-]{0,40}", volume_prefix
    ):
        raise ValueError("Invalid DATA_VOLUME_PREFIX.")
    address = ipaddress.ip_address(values.get("BIND_IP", ""))
    if address.version != 4 or address.is_unspecified or address.is_global:
        raise ValueError(
            "BIND_IP must be a specific local/private IPv4 address."
        )
    port_defaults = {
        "TRINO_PORT": "8080",
        "CATALOG_PORT": "8181",
        "S3_PORT": "8333",
        "GRAFANA_PORT": "3000",
        "PROMETHEUS_PORT": "9090",
        "N8N_PORT": "5678",
        "AIRFLOW_PORT": "8081",
        "DEMO_GUI_PORT": "8501",
        "DEMO_API_PORT": "8502",
        "CDC_CONNECT_PORT": "8083",
        "CDC_UI_PORT": "8084",
        "SUPERSET_PORT": "8088",
        "JUPYTER_PORT": "8888",
        "QDRANT_PORT": "6333",
        "MARQUEZ_API_PORT": "5000",
        "MARQUEZ_UI_PORT": "3001",
        "MCP_PORT": "8503",
    }
    for key, default in port_defaults.items():
        # New inactive profiles remain compatible with older site settings.
        values.setdefault(key, default)
        if not values[key].isdigit() or not 1024 <= int(values[key]) <= 65535:
            raise ValueError("Invalid port: " + key)
    ports = [
        values[k]
        for k in values
        if k.endswith("_PORT")
        and not k.endswith("_SQLSERVER_PORT")
        and values[k]
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
        "demo",
        "cdc",
        "bi",
        "notebooks",
        "vector",
        "lineage",
        "mcp",
    }:
        raise ValueError(
            "Profiles may be automation, monitoring, compute, "
            "orchestration, demo, cdc, bi, notebooks, vector, lineage, mcp."
        )
    if profiles & {"demo", "mcp"}:
        if not re.fullmatch(
            r"[A-Za-z0-9]{20,128}", values.get("CONTROL_API_TOKEN", "")
        ):
            raise ValueError("Generate a fresh CONTROL_API_TOKEN.")
        directory = Path(values.get("CONTROL_SETTINGS_DIR", ""))
        if not directory.is_absolute() or directory.resolve().is_relative_to(
            ROOT
        ):
            raise ValueError("Use an external CONTROL_SETTINGS_DIR.")
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}",
            values.get("CONTROL_SETTINGS_NAME", ""),
        ):
            raise ValueError("Invalid CONTROL_SETTINGS_NAME.")
    profile_secrets = {
        "bi": (
            "SUPERSET_DB_PASSWORD",
            "SUPERSET_SECRET_KEY",
            "SUPERSET_ADMIN_PASSWORD",
            "SUPERSET_REDIS_PASSWORD",
        ),
        "notebooks": ("JUPYTER_TOKEN",),
        "vector": ("QDRANT_API_KEY", "QDRANT_READ_ONLY_API_KEY"),
        "lineage": ("MARQUEZ_DB_PASSWORD",),
        "mcp": ("MCP_API_TOKEN",),
    }
    for profile, keys in profile_secrets.items():
        if profile not in profiles:
            continue
        for key in keys:
            minimum = (
                32 if key in {"SUPERSET_SECRET_KEY", "JUPYTER_TOKEN"} else 20
            )
            pattern = r"[A-Za-z0-9]{" + str(minimum) + r",128}"
            if not re.fullmatch(pattern, values.get(key, "")):
                raise ValueError("Generate a fresh secret for " + key)
    if "vector" in profiles and (
        values["QDRANT_API_KEY"] == values["QDRANT_READ_ONLY_API_KEY"]
    ):
        raise ValueError("Use distinct Qdrant management and read-only keys.")
    if "notebooks" in profiles:
        if (
            not values.get("WORKER_UID", "").isdigit()
            or int(values["WORKER_UID"]) == 0
            or not values.get("WORKER_GID", "").isdigit()
        ):
            raise ValueError("Notebook UID must be non-root; GID numeric.")
        workspace = Path(values.get("NOTEBOOKS_DIR", ""))
        if not workspace.is_absolute() or workspace.resolve().is_relative_to(
            ROOT
        ):
            raise ValueError("Use an external absolute NOTEBOOKS_DIR.")
    if values.get("MCP_ENABLE_MUTATIONS", "false") not in {"true", "false"}:
        raise ValueError("MCP_ENABLE_MUTATIONS must be true or false.")
    ca_file = values.get("CORPORATE_CA_FILE", "")
    if ca_file and not Path(ca_file).is_absolute():
        raise ValueError("CORPORATE_CA_FILE must be absolute when configured.")
    return values


def environment(values):
    """Overlay validated site settings on the subprocess environment."""
    result = dict(os.environ)
    result.update(values)
    return result
