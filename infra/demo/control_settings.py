"""Validate and save private settings without applying host configuration."""

import importlib.util
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "control_site_validator", ROOT / "scripts/settings.py"
)
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)
SETTING_KEYS = frozenset(
    "COMPOSE_PROJECT_NAME DATA_VOLUME_PREFIX BIND_IP LANDING_DIR WORKER_UID "
    "WORKER_GID TRINO_PORT CATALOG_PORT S3_PORT GRAFANA_PORT PROMETHEUS_PORT "
    "N8N_PORT AIRFLOW_PORT COMPOSE_PROFILES WAREHOUSE_NAME S3_BUCKET_RAW "
    "S3_BUCKET_WAREHOUSE S3_ACCESS_KEY S3_SECRET_KEY CATALOG_DB_PASSWORD "
    "CATALOG_ENCRYPTION_KEY WORKER_TOKEN N8N_DB_PASSWORD N8N_ENCRYPTION_KEY "
    "GRAFANA_ADMIN_PASSWORD AIRFLOW_DB_PASSWORD AIRFLOW_FERNET_KEY "
    "AIRFLOW_JWT_SECRET N8N_HOST N8N_PROTOCOL N8N_SECURE_COOKIE "
    "SOURCE_SQLSERVER_HOST SOURCE_SQLSERVER_PORT SOURCE_SQLSERVER_DATABASE "
    "SOURCE_SQLSERVER_USER SOURCE_SQLSERVER_PASSWORD SOURCE_SQLSERVER_CA_FILE "
    "SOURCE_SQLSERVER_TIMEZONE DEMO_SQLSERVER_HOST DEMO_SQLSERVER_PORT "
    "DEMO_SQLSERVER_DATABASE DEMO_SQLSERVER_USER DEMO_SQLSERVER_PASSWORD "
    "DEMO_SQLSERVER_CA_FILE DEMO_SQLSERVER_HOSTNAME_IN_CERTIFICATE "
    "DOKPLOY_BIND_IP DOKPLOY_ALLOWED_CIDRS "
    "DOKPLOY_ADMIN_PORT SWARM_ADVERTISE_ADDR SWARM_ADDRESS_POOL "
    "SWARM_SUBNET_SIZE DOCKER_BRIDGE_CIDR HTTP_PROXY HTTPS_PROXY NO_PROXY "
    "http_proxy https_proxy no_proxy CONTROL_API_TOKEN CONTROL_SETTINGS_DIR "
    "CONTROL_SETTINGS_NAME DEMO_GUI_PORT DEMO_API_PORT CDC_CONFIG_DIR "
    "CDC_SQLSERVER_HOST CDC_SQLSERVER_PORT CDC_SQLSERVER_DATABASE "
    "CDC_SQLSERVER_USER CDC_SQLSERVER_PASSWORD CDC_SQLSERVER_TRUSTSTORE "
    "CDC_SQLSERVER_TRUSTSTORE_FILE CDC_SQLSERVER_TRUSTSTORE_PASSWORD "
    "CDC_SQLSERVER_HOSTNAME_IN_CERTIFICATE "
    "CDC_SQLSERVER_AGENT_STATUS_QUERY CDC_TOPIC_PREFIX CDC_CONNECTOR_NAME "
    "CDC_CONNECT_PORT CDC_UI_PORT CDC_KAFKA_CLUSTER_ID CDC_GROUP_ID "
    "CDC_BATCH_SIZE CDC_POLL_SECONDS CDC_DEMO_OWNERSHIP_TOKEN "
    "SUPERSET_PORT SUPERSET_DB_PASSWORD SUPERSET_SECRET_KEY "
    "SUPERSET_ADMIN_USERNAME SUPERSET_ADMIN_PASSWORD SUPERSET_ADMIN_EMAIL "
    "SUPERSET_REDIS_PASSWORD JUPYTER_PORT JUPYTER_TOKEN NOTEBOOKS_DIR "
    "QDRANT_PORT QDRANT_API_KEY QDRANT_READ_ONLY_API_KEY "
    "MARQUEZ_API_PORT MARQUEZ_UI_PORT MARQUEZ_DB_PASSWORD "
    "CORPORATE_CA_FILE MCP_PORT MCP_API_TOKEN MCP_ENABLE_MUTATIONS "
    "MCP_ALLOWED_HOSTS".split()
)
SECRET_KEYS = frozenset(
    key
    for key in SETTING_KEYS
    if re.search(
        r"PASSWORD|SECRET|TOKEN|ENCRYPTION_KEY|FERNET_KEY|API_KEY", key
    )
    or key in {"S3_ACCESS_KEY", "HTTP_PROXY", "HTTPS_PROXY"}
    or key in {"http_proxy", "https_proxy"}
)
MOUNT_KEYS = frozenset(
    {
        "LANDING_DIR",
        "WORKER_UID",
        "WORKER_GID",
        "CONTROL_SETTINGS_DIR",
        "CONTROL_SETTINGS_NAME",
        "DATA_VOLUME_PREFIX",
        "COMPOSE_PROJECT_NAME",
        "CDC_CONFIG_DIR",
        "CDC_SQLSERVER_TRUSTSTORE_FILE",
        "NOTEBOOKS_DIR",
        "CORPORATE_CA_FILE",
    }
)


class SettingsError(ValueError):
    """Report a fixed configuration failure without exposing input values."""


class SettingsStore:
    """Keep saved settings separate from the process's applied snapshot."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        try:
            self.applied = self.read()
        except (OSError, ValueError):
            self.applied = {}

    def check_path(self) -> None:
        """Require a private regular file in a writable private directory."""
        if (
            not self.path.is_absolute()
            or any(
                part.is_symlink() for part in (self.path, *self.path.parents)
            )
            or not self.path.is_file()
            or self.path.resolve().is_relative_to(ROOT)
            or self.path.parent.stat().st_mode & 0o077
            or self.path.stat().st_mode & 0o077
            or self.path.stat().st_size > 128 * 1024
        ):
            raise SettingsError("Invalid private settings file.")

    def read(self) -> dict[str, str]:
        """Use the same validation as the CLI without evaluating shell text."""
        with self.lock:
            self.check_path()
            return VALIDATOR.load(self.path)

    def public(self) -> dict:
        """Expose non-secret values and flags without stored credentials."""
        values = self.read()
        changed = {
            key for key in values if values[key] != self.applied.get(key)
        }
        return {
            "values": {
                key: value
                for key, value in values.items()
                if key in SETTING_KEYS and key not in SECRET_KEYS
            },
            "secret_keys": sorted(SECRET_KEYS),
            "configured_secrets": sorted(
                key for key in SECRET_KEYS if values.get(key)
            ),
            "pending_restart": bool(changed),
            "pending_mount_change": bool(changed & MOUNT_KEYS),
        }

    def source_settings(self) -> dict[str, str]:
        """Map a CA in the dedicated host directory to its private mount."""
        values = {
            key: value
            for key, value in self.applied.items()
            if key.startswith("DEMO_SQLSERVER_")
            or key == "SOURCE_SQLSERVER_DATABASE"
        }
        certificate = values.get("DEMO_SQLSERVER_CA_FILE", "")
        if not certificate or certificate == "system":
            return values
        selected = Path(certificate)
        host_directory = Path(self.applied.get("CONTROL_SETTINGS_DIR", ""))
        if selected.is_absolute() and selected.is_relative_to(host_directory):
            selected = self.path.parent / selected.relative_to(host_directory)
        if (
            not selected.is_absolute()
            or not selected.resolve().is_relative_to(
                self.path.parent.resolve()
            )
            or any(part.is_symlink() for part in (selected, *selected.parents))
            or not selected.is_file()
        ):
            raise SettingsError("Keep the demo CA in the private directory.")
        values["DEMO_SQLSERVER_CA_FILE"] = str(selected)
        return values

    def save(self, updates: dict[str, str]) -> dict:
        """Atomically save a validated file without reconfiguring Docker."""
        if not updates.keys() <= SETTING_KEYS:
            raise SettingsError("Unknown settings key.")
        if any(
            len(value) > 4096
            or any(ord(c) < 32 or ord(c) > 126 for c in value)
            for value in updates.values()
        ):
            raise SettingsError("Settings must be single-line ASCII.")
        with self.lock:
            current = self.read()
            # Moving the directory that stores this file needs host-side work.
            for key in ("CONTROL_SETTINGS_DIR", "CONTROL_SETTINGS_NAME"):
                if key in updates and updates[key] != current.get(key):
                    raise SettingsError("Move private settings on the host.")
            original = self.path.read_text()
            selected = {
                key: value
                for key, value in updates.items()
                if value or key not in SECRET_KEYS
            }
            # One GUI edit updates both client conventions consistently.
            for upper in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
                lower = upper.lower()
                if upper in selected and lower not in selected:
                    selected[lower] = selected[upper]
                elif lower in selected and upper not in selected:
                    selected[upper] = selected[lower]
                elif (
                    upper in selected
                    and lower in selected
                    and selected[upper] != selected[lower]
                ):
                    raise SettingsError("Proxy case variants must match.")
            candidate = {**current, **selected}
            for key in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "http_proxy",
                "https_proxy",
            ):
                if candidate.get(key):
                    address = urlsplit(candidate[key])
                    if (
                        address.scheme not in {"http", "https"}
                        or not address.hostname
                        or address.port is None
                        or address.query
                        or address.fragment
                        or address.path not in {"", "/"}
                    ):
                        raise SettingsError("Invalid proxy URL.")
            name = candidate.get("CONTROL_SETTINGS_NAME", self.path.name)
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in {
                ".",
                "..",
            }:
                raise SettingsError("Invalid settings filename.")
            lines = []
            written = set()
            for line in original.splitlines():
                key = line.partition("=")[0]
                if key in selected:
                    lines.append(key + "=" + selected[key])
                    written.add(key)
                else:
                    lines.append(line)
            lines.extend(
                key + "=" + selected[key]
                for key in sorted(selected.keys() - written)
            )
            content = "\n".join(lines) + "\n"
            descriptor, temporary = tempfile.mkstemp(
                prefix=".site-settings-", dir=self.path.parent
            )
            try:
                with os.fdopen(descriptor, "w") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary, 0o600)
                # Validate before replacing the active settings file.
                VALIDATOR.load(temporary)
                if self.path.read_text() != original:
                    raise SettingsError("Settings changed during save.")
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return {**self.public(), "saved": True, "applied": False}
