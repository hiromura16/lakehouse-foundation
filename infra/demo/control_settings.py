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
    "N8N_SSRF_PROTECTION_ENABLED N8N_SSRF_BLOCKED_IP_RANGES "
    "N8N_SSRF_ALLOWED_IP_RANGES N8N_SSRF_ALLOWED_HOSTNAMES "
    "N8N_SSRF_BLOCKED_HOSTNAMES N8N_SSRF_DNS_CACHE_MAX_SIZE "
    "S3_BUCKET_WAREHOUSE S3_ACCESS_KEY S3_SECRET_KEY CATALOG_DB_PASSWORD "
    "CATALOG_ENCRYPTION_KEY WORKER_TOKEN N8N_DB_PASSWORD N8N_ENCRYPTION_KEY "
    "GRAFANA_ADMIN_PASSWORD AIRFLOW_DB_PASSWORD AIRFLOW_FERNET_KEY "
    "AIRFLOW_JWT_SECRET N8N_HOST N8N_PROTOCOL N8N_SECURE_COOKIE "
    "SOURCE_SQLSERVER_HOST SOURCE_SQLSERVER_PORT SOURCE_SQLSERVER_DATABASE "
    "SOURCE_SQLSERVER_USER SOURCE_SQLSERVER_PASSWORD SOURCE_SQLSERVER_CA_FILE "
    "SOURCE_SQLSERVER_TIMEZONE DEMO_SQLSERVER_HOST DEMO_SQLSERVER_PORT "
    "DEMO_SQLSERVER_DATABASE DEMO_SQLSERVER_USER DEMO_SQLSERVER_PASSWORD "
    "DEMO_SQLSERVER_CA_FILE DEMO_SQLSERVER_HOSTNAME_IN_CERTIFICATE "
    "DEMO_SQLSERVER_ALLOW_SHARED_SOURCE_DATABASE DEMO_SQLSERVER_ERP_SCHEMA "
    "DEMO_SQLSERVER_MES_SCHEMA DEMO_SQLSERVER_CONTROL_SCHEMA "
    "DEMO_SQLSERVER_INSTALLATION_ID DEMO_SQLSERVER_OWNERSHIP_TOKEN "
    "DEMO_SQLSERVER_ENCRYPT DEMO_SQLSERVER_TRUST_SERVER_CERTIFICATE "
    "SOURCE_SQLSERVER_ENCRYPT SOURCE_SQLSERVER_TRUST_SERVER_CERTIFICATE "
    "SOURCE_SQLSERVER_HOSTNAME_IN_CERTIFICATE "
    "SOURCE_SQLSERVER_CONTROL_USER SOURCE_SQLSERVER_CONTROL_PASSWORD "
    "SOURCE_SQLSERVER_BATCH_OVERLAP_SECONDS "
    "SOURCE_SQLSERVER_BATCH_SAFETY_LAG_SECONDS "
    "DOKPLOY_BIND_IP DOKPLOY_ALLOWED_CIDRS "
    "DOKPLOY_ADMIN_PORT DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN "
    "SWARM_ADVERTISE_ADDR SWARM_ADDRESS_POOL "
    "SWARM_SUBNET_SIZE DOCKER_BRIDGE_CIDR HTTP_PROXY HTTPS_PROXY NO_PROXY "
    "http_proxy https_proxy no_proxy LAKEHOUSE_NO_PROXY CONTROL_API_TOKEN "
    "CONTROL_SETTINGS_DIR "
    "CONTROL_SETTINGS_NAME DEMO_GUI_PORT DEMO_API_PORT CDC_CONFIG_DIR "
    "CDC_SOURCE_MODE CDC_STATE_VOLUME_NAME "
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
    "MCP_ALLOWED_HOSTS SPARK_MASTER_UI_PORT SPARK_WORKER_UI_PORT "
    "SPARK_HISTORY_UI_PORT CADVISOR_DOCKER_DATA_ROOT "
    "MONITORING_LOG_DIR MONITORING_HOST_ALIAS OPENOBSERVE_PORT "
    "OPENOBSERVE_ROOT_USER_EMAIL OPENOBSERVE_ROOT_USER_PASSWORD "
    "OPENOBSERVE_INGEST_TOKEN_FILE PORTAL_LINK_MODE PORTAL_UI_MODE".split()
) | frozenset(VALIDATOR.PUBLIC_URL_KEYS.values())
# A token file path is editable metadata; its contents remain private.
NON_SECRET_KEYS = {
    "DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN",
    "OPENOBSERVE_INGEST_TOKEN_FILE",
}
SECRET_KEYS = frozenset(
    key
    for key in SETTING_KEYS
    if key not in NON_SECRET_KEYS
    and re.search(
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
        "CDC_SOURCE_MODE",
        "CDC_STATE_VOLUME_NAME",
        "CDC_SQLSERVER_TRUSTSTORE_FILE",
        "NOTEBOOKS_DIR",
        "CADVISOR_DOCKER_DATA_ROOT",
        "MONITORING_LOG_DIR",
        "OPENOBSERVE_INGEST_TOKEN_FILE",
        "CORPORATE_CA_FILE",
    }
)


class SettingsError(ValueError):
    """Report a fixed configuration failure without exposing input values."""


class SettingsStore:
    """Keep saved settings separate from the process's applied snapshot."""

    def __init__(self, path: Path):
        """Keep the startup snapshot distinct from later saved settings."""
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
            return VALIDATOR.load(self.path, private_mount=self.path.parent)

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
            or key
            in {
                "SOURCE_SQLSERVER_HOST",
                "SOURCE_SQLSERVER_PORT",
                "SOURCE_SQLSERVER_DATABASE",
            }
        }
        return self._map_sql_ca(values, "DEMO_SQLSERVER_CA_FILE")

    def reader_settings(self, *, include_control=False) -> dict[str, str]:
        """Select SOURCE auth and marker policy, without DEMO credentials.

        Checkpoint auth is separate and opt-in. This internal dictionary is
        never serialized to worker requests or public API/GUI responses.
        """
        keys = {
            "DEMO_SQLSERVER_" + suffix
            for suffix in (
                "INSTALLATION_ID",
                "OWNERSHIP_TOKEN",
                "ERP_SCHEMA",
                "MES_SCHEMA",
                "CONTROL_SCHEMA",
                "ALLOW_SHARED_SOURCE_DATABASE",
            )
        }
        if include_control:
            keys.add(
                "DEMO_SQLSERVER_USER"
            )  # Identity check only, no DEMO auth.
        values = {
            key: value
            for key, value in self.applied.items()
            if (key.startswith("SOURCE_SQLSERVER_") or key in keys)
            and (
                include_control
                or not key.startswith("SOURCE_SQLSERVER_CONTROL_")
            )
        }
        return self._map_sql_ca(values, "SOURCE_SQLSERVER_CA_FILE")

    def _map_sql_ca(self, values, key):
        """Resolve one host-side CA inside the dedicated private mount."""
        certificate = values.get(key, "")
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
        values[key] = str(selected)
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
            for upper in ("HTTP_PROXY", "HTTPS_PROXY"):
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
            bypass_edits = {
                key: value
                for key, value in selected.items()
                if key in VALIDATOR.BYPASS_KEYS
            }
            if bypass_edits:
                # The read model already merged all legacy additions. A full
                # explicit edit replaces it, then restores required names.
                normalized = [
                    VALIDATOR.normalize_no_proxy(
                        {"LAKEHOUSE_NO_PROXY": bypass_edits[key]}
                    )
                    for key in VALIDATOR.BYPASS_KEYS
                    if key in bypass_edits
                ]
                if len({value.casefold() for value in normalized}) != 1:
                    raise SettingsError("Proxy bypass edits must agree.")
                canonical = normalized[0]
            else:
                canonical = VALIDATOR.normalize_no_proxy(current)
            selected.update({key: canonical for key in VALIDATOR.BYPASS_KEYS})
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
                VALIDATOR.load(temporary, private_mount=self.path.parent)
                if self.path.read_text() != original:
                    raise SettingsError("Settings changed during save.")
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return {**self.public(), "saved": True, "applied": False}
