"""Read a private Compose env file without evaluating shell code."""

import base64
import binascii
import importlib.util
import ipaddress
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Keep this list aligned with actual Compose services and installer services.
# Short internal names are explicit because client CIDR semantics differ.
REQUIRED_NO_PROXY = (
    "localhost",
    "127.0.0.1",
    "::1",
    "storage",
    "catalog",
    "catalog-db",
    "trino",
    "worker",
    "catalog-migrate",
    "n8n-db",
    "n8n",
    "prometheus",
    "grafana",
    "node-exporter",
    "cadvisor",
    "loki",
    "vector",
    "openobserve",
    "sail-server",
    "sail-benchmark",
    "spark-volumes-init",
    "spark-master",
    "spark-worker",
    "spark-history",
    "spark-check",
    "airflow-db",
    "airflow",
    "demo-api",
    "demo",
    "cdc-kafka",
    "cdc-connect",
    "cdc-ui",
    "cdc-register",
    "cdc-consumer",
    "superset-db",
    "superset-redis",
    "superset-init",
    "superset",
    "jupyter",
    "qdrant",
    "marquez-db",
    "marquez",
    "marquez-web",
    "mcp",
    "dokploy",
    "dokploy-postgres",
    "dokploy-traefik",
)
BYPASS_KEYS = ("LAKEHOUSE_NO_PROXY", "NO_PROXY", "no_proxy")


def buildkit_client_token_mode(values):
    """Validate the private 0/1 switch; never accept shell-like booleans."""
    value = values.get("DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN", "1")
    if value not in {"0", "1"}:
        raise ValueError("DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN must be 0 or 1.")
    return value


def validate_sql_source_policy(values):
    """Validate inactive SQL defaults without enabling a DB connection.

    A shared source remains denied by default. Live ownership/permissions and
    explicit request consent are additionally checked by the fixed adapter.
    """
    for key in (
        "DEMO_SQLSERVER_ALLOW_SHARED_SOURCE_DATABASE",
        "DEMO_SQLSERVER_TRUST_SERVER_CERTIFICATE",
        "SOURCE_SQLSERVER_TRUST_SERVER_CERTIFICATE",
    ):
        values.setdefault(key, "false")
        if values[key] not in {"true", "false"}:
            raise ValueError("Use true or false for " + key)
    for prefix in ("DEMO", "SOURCE"):
        key = prefix + "_SQLSERVER_ENCRYPT"
        values.setdefault(key, "true")
        if values[key] != "true":
            raise ValueError("SQL source encryption must remain enabled.")
    for suffix, default, minimum, maximum in (
        ("OVERLAP_SECONDS", "300", 1, 86400),
        ("SAFETY_LAG_SECONDS", "5", 0, 3600),
    ):
        key = "SOURCE_SQLSERVER_BATCH_" + suffix
        values.setdefault(key, default)
        if (
            not values[key].isdigit()
            or not minimum <= int(values[key]) <= maximum
        ):
            raise ValueError("Invalid bounded SQL batch interval: " + key)
    for suffix, schema in (
        ("ERP_SCHEMA", "lh_demo_erp"),
        ("MES_SCHEMA", "lh_demo_mes"),
        ("CONTROL_SCHEMA", "lh_demo_ctl"),
    ):
        key = "DEMO_SQLSERVER_" + suffix
        values.setdefault(key, schema)
        if values[key] != schema:
            raise ValueError("Only the fixed SQL demo schemas are allowed.")
    values.setdefault("CDC_SOURCE_MODE", "generic")
    if values["CDC_SOURCE_MODE"] not in {"generic", "fixed-demo"}:
        raise ValueError("CDC_SOURCE_MODE must be generic or fixed-demo.")
    volume = values.setdefault("CDC_STATE_VOLUME_NAME", "")
    if volume and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", volume
    ):
        raise ValueError("Use a literal Docker volume name for CDC state.")
    if values["CDC_SOURCE_MODE"] == "fixed-demo" and (
        "cdc" in values.get("COMPOSE_PROFILES", "").split(",")
        and values.get("SOURCE_SQLSERVER_CA_FILE") == "system"
    ):
        raise ValueError(
            "Use an empty SOURCE_SQLSERVER_CA_FILE for system CA."
        )
    if (
        values["CDC_SOURCE_MODE"] == "fixed-demo"
        and "cdc" in values.get("COMPOSE_PROFILES", "").split(",")
        and values.get("SOURCE_SQLSERVER_CA_FILE")
        and values.get("SOURCE_SQLSERVER_TRUST_SERVER_CERTIFICATE") != "true"
        and not values.get("CDC_SQLSERVER_TRUSTSTORE")
    ):
        raise ValueError(
            "Custom SOURCE PEM also needs an approved Java CDC truststore."
        )


def normalize_no_proxy(values):
    """Merge private bypass values without consulting the host environment."""
    result = list(REQUIRED_NO_PROXY)
    seen = {entry.casefold() for entry in result}
    for key in BYPASS_KEYS:
        value = values.get(key, "")
        if not isinstance(value, str):
            raise ValueError("Proxy bypass values must be strings.")
        for entry in value.split(","):
            entry = entry.strip()
            if entry and entry.casefold() not in seen:
                result.append(entry)
                seen.add(entry.casefold())
    return ",".join(result)


def synchronize_no_proxy(values):
    """Return a copy with canonical and both legacy names kept identical."""
    result = dict(values)
    canonical = normalize_no_proxy(values)
    result.update({key: canonical for key in BYPASS_KEYS})
    return result


def validate_ssrf(values):
    """Reject malformed rules before n8n could ignore a CIDR typo."""
    defaults = {
        "N8N_SSRF_PROTECTION_ENABLED": "true",
        "N8N_SSRF_BLOCKED_IP_RANGES": "default,100.64.0.0/10",
        "N8N_SSRF_ALLOWED_IP_RANGES": "",
        "N8N_SSRF_ALLOWED_HOSTNAMES": "worker",
        "N8N_SSRF_BLOCKED_HOSTNAMES": "",
        "N8N_SSRF_DNS_CACHE_MAX_SIZE": "1048576",
    }
    for key, value in defaults.items():
        values.setdefault(key, value)
    if values["N8N_SSRF_PROTECTION_ENABLED"] not in {"true", "false"}:
        raise ValueError("N8N_SSRF_PROTECTION_ENABLED must be true or false.")
    size = values["N8N_SSRF_DNS_CACHE_MAX_SIZE"]
    if not size.isdigit() or not 65536 <= int(size) <= 16777216:
        raise ValueError("Invalid N8N_SSRF_DNS_CACHE_MAX_SIZE.")
    for key in ("N8N_SSRF_ALLOWED_IP_RANGES", "N8N_SSRF_BLOCKED_IP_RANGES"):
        entries = [entry.strip() for entry in values[key].split(",") if entry]
        if len(entries) > 64:
            raise ValueError("Too many CIDR entries in " + key)
        for entry in entries:
            if entry.lower() == "default" and key.endswith(
                "BLOCKED_IP_RANGES"
            ):
                continue
            try:
                ipaddress.ip_network(entry, strict=True)
            except ValueError:
                raise ValueError("Invalid CIDR entry in " + key) from None
    for key in ("N8N_SSRF_ALLOWED_HOSTNAMES", "N8N_SSRF_BLOCKED_HOSTNAMES"):
        for entry in filter(None, values[key].split(",")):
            hostname = entry.strip().removeprefix("*.")
            if len(hostname) > 253 or not all(
                re.fullmatch(
                    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label
                )
                for label in hostname.split(".")
            ):
                raise ValueError("Use DNS names without URL or port in " + key)


def validate_monitoring_paths(values, settings_path):
    """Check selected host mounts without preparing or correcting the host.

    Empty Docker root keeps config-only plans possible but is never runtime
    acceptance. Nonempty paths must be literal external directories, distinct
    from notebooks, landing and private settings. Never include their value in
    validation errors.
    """
    if "monitoring" not in values.get("COMPOSE_PROFILES", "").split(","):
        return
    # A public alias is deliberately independent of the host's real name.
    values.setdefault("MONITORING_HOST_ALIAS", "local")
    if not re.fullmatch(
        r"[a-z][a-z0-9_-]{0,31}", values["MONITORING_HOST_ALIAS"]
    ):
        raise ValueError("Use a short sanitized MONITORING_HOST_ALIAS.")
    for key, default in (
        ("CADVISOR_DOCKER_DATA_ROOT", ""),
        ("MONITORING_LOG_DIR", "/srv/lakehouse/log-spool"),
    ):
        selected = values.get(key, default)
        if selected:
            _validate_monitoring_mount(selected, values, settings_path)
    docker_root = values.get("CADVISOR_DOCKER_DATA_ROOT", "")
    spool = values.get("MONITORING_LOG_DIR", "/srv/lakehouse/log-spool")
    if docker_root and (
        Path(spool).is_relative_to(Path(docker_root))
        or Path(docker_root).is_relative_to(Path(spool))
    ):
        raise ValueError("Separate log spool and Docker data root.")


def _validate_monitoring_mount(selected, values, settings_path):
    """Inspect a selected path without following links or changing metadata."""
    candidate = Path(selected)
    excluded = [ROOT, Path(settings_path).parent]
    excluded.extend(
        Path(values[key])
        for key in ("LANDING_DIR", "NOTEBOOKS_DIR", "CONTROL_SETTINGS_DIR")
        if values.get(key)
    )
    blocked = {"/", "/var", "/srv", "/opt", "/mnt", "/tmp", "/home"}
    protected = {".config", ".ssh", ".aws", ".docker", ".codex"}
    if (
        not candidate.is_absolute()
        or ".." in candidate.parts
        or str(candidate) in blocked
        or protected.intersection(candidate.parts)
        or any(
            candidate == target or candidate.is_relative_to(target)
            for target in map(
                Path, ("/etc", "/proc", "/sys", "/dev", "/run", "/root")
            )
        )
        or any(
            candidate.is_relative_to(target)
            or target.is_relative_to(candidate)
            for target in excluded
        )
    ):
        raise ValueError("Use a dedicated external monitoring directory.")
    try:
        for part in (candidate, *candidate.parents):
            if part.is_symlink():
                raise ValueError(
                    "Monitoring mount paths must not use symlinks."
                )
        if candidate.exists() and not candidate.is_dir():
            raise ValueError("Monitoring mount must be a directory.")
    except OSError:
        raise ValueError("Monitoring mount could not be inspected.") from None


def load(path, *, private_mount=None):
    """Parse private settings literally and validate deployment boundaries."""
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("Use a private env file outside the repository.")
    if not path.is_file():
        raise ValueError("Private env file not found.")
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
    profiles = set(filter(None, values.get("COMPOSE_PROFILES", "").split(",")))
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
        "SPARK_MASTER_UI_PORT": "8082",
        "SPARK_WORKER_UI_PORT": "8085",
        "SPARK_HISTORY_UI_PORT": "18080",
        "OPENOBSERVE_PORT": "5080",
    }
    for key, default in port_defaults.items():
        # New inactive profiles remain compatible with older site settings.
        values.setdefault(key, default)
        if key == "OPENOBSERVE_PORT" and "openobserve-lab" not in profiles:
            continue
        if not values[key].isdigit() or not 1024 <= int(values[key]) <= 65535:
            raise ValueError("Invalid port: " + key)
    port_profiles = {
        "GRAFANA_PORT": {"monitoring"},
        "PROMETHEUS_PORT": {"monitoring"},
        "N8N_PORT": {"automation"},
        "AIRFLOW_PORT": {"orchestration"},
        "DEMO_GUI_PORT": {"demo"},
        "DEMO_API_PORT": {"demo", "mcp"},
        "CDC_CONNECT_PORT": {"cdc"},
        "CDC_UI_PORT": {"cdc"},
        "SUPERSET_PORT": {"bi"},
        "JUPYTER_PORT": {"notebooks"},
        "QDRANT_PORT": {"vector"},
        "MARQUEZ_API_PORT": {"lineage"},
        "MARQUEZ_UI_PORT": {"lineage"},
        "MCP_PORT": {"mcp"},
        "SPARK_MASTER_UI_PORT": {"compute"},
        "SPARK_WORKER_UI_PORT": {"compute"},
        "SPARK_HISTORY_UI_PORT": {"compute"},
        "OPENOBSERVE_PORT": {"openobserve-lab"},
    }
    # An inactive profile publishes no port and must not break an older site.
    ports = [
        int(values[key])
        for key in port_defaults
        if key not in port_profiles or profiles & port_profiles[key]
    ]
    if len(ports) != len(set(ports)):
        raise ValueError("Active host service ports must be distinct.")
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
        "openobserve-lab",
        "sail-lab",
    }:
        raise ValueError(
            "Profiles may be automation, monitoring, compute, "
            "orchestration, demo, cdc, bi, notebooks, vector, lineage, mcp, "
            "openobserve-lab, sail-lab."
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
    if profiles & {"notebooks", "compute", "monitoring", "sail-lab"}:
        if (
            not values.get("WORKER_UID", "").isdigit()
            or int(values["WORKER_UID"]) == 0
            or not values.get("WORKER_GID", "").isdigit()
        ):
            raise ValueError("Compute UID must be non-root; GID numeric.")
    if "openobserve-lab" in profiles:
        if "monitoring" not in profiles:
            raise ValueError("Select monitoring with openobserve-lab.")
        # Reuse the narrowly scoped, offline validator. Importing this helper
        # never runs its CLI, opens a socket or creates a token.
        specification = importlib.util.spec_from_file_location(
            "openobserve_site_validation", ROOT / "scripts/openobserve_lab.py"
        )
        lab = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(lab)
        lab_values = values
        if private_mount is not None:
            # GUI containers see only the existing dedicated private mount.
            # Map one sibling token basename, never an arbitrary host path.
            mount = Path(private_mount)
            host = Path(values.get("CONTROL_SETTINGS_DIR", ""))
            token = Path(values.get("OPENOBSERVE_INGEST_TOKEN_FILE", ""))
            if (
                not mount.is_absolute()
                or mount != Path(path).absolute().parent
                or not host.is_absolute()
                or not token.is_absolute()
                or token.parent != host
                or ".." in token.parts
                or token.name == values.get("CONTROL_SETTINGS_NAME")
            ):
                raise ValueError(
                    "Keep the lab token beside site settings for GUI access."
                )
            lab_values = {
                **values,
                "OPENOBSERVE_INGEST_TOKEN_FILE": str(mount / token.name),
            }
        lab.validate_config(lab_values, require_token=True)
    if "notebooks" in profiles:
        workspace = Path(values.get("NOTEBOOKS_DIR", ""))
        if (
            not workspace.is_absolute()
            or ".." in workspace.parts
            or workspace.is_relative_to(ROOT)
            or workspace == Path("/")
        ):
            raise ValueError("Use an external absolute NOTEBOOKS_DIR.")
    if values.get("MCP_ENABLE_MUTATIONS", "false") not in {"true", "false"}:
        raise ValueError("MCP_ENABLE_MUTATIONS must be true or false.")
    ca_file = values.get("CORPORATE_CA_FILE", "")
    if ca_file and not Path(ca_file).is_absolute():
        raise ValueError("CORPORATE_CA_FILE must be absolute when configured.")
    values["DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN"] = buildkit_client_token_mode(
        values
    )
    validate_monitoring_paths(values, path)
    validate_ssrf(values)
    validate_sql_source_policy(values)
    return synchronize_no_proxy(values)


def environment(values):
    """Overlay validated site settings on the subprocess environment."""
    result = dict(os.environ)
    # Compose interpolation prefers shell variables over --env-file. Always
    # replace all bypass names with the normalized selected private values.
    result.update(synchronize_no_proxy(values))
    return result
