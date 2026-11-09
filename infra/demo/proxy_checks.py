"""Fixed-target proxy diagnostics with no secret or arbitrary URL output."""

import base64
import http.client
import math
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import proxy_bypass_environment

TARGETS = {
    "pypi": "https://pypi.org/simple/",
    "maven": "https://repo.maven.apache.org/maven2/",
    "npm": "https://registry.npmjs.org/-/ping",
    "github": "https://api.github.com/meta",
}
INTERNAL_SERVICES = (
    "storage",
    "catalog",
    "catalog-db",
    "trino",
    "worker",
    "n8n",
    "n8n-db",
    "prometheus",
    "grafana",
    "airflow",
    "airflow-db",
    "demo",
    "demo-api",
    "cdc-kafka",
    "cdc-connect",
    "cdc-ui",
    "cdc-consumer",
    "dokploy",
    "dokploy-postgres",
    "superset",
    "superset-db",
    "superset-redis",
    "jupyter",
    "qdrant",
    "marquez",
    "marquez-db",
    "marquez-web",
)


def _effective(values, key):
    """Follow lowercase precedence without consulting process environment."""
    return values.get(key.lower(), values.get(key, ""))


def _bypass(host, values):
    """Evaluate Python urllib's NO_PROXY semantics for one known hostname."""
    return proxy_bypass_environment(
        host, {"no": _effective(values, "NO_PROXY")}
    )


def configuration_report(values):
    """Describe configuration and internal routing without returning values."""
    variables = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        variables[key] = {
            "uppercase_present": bool(values.get(key)),
            "lowercase_present": bool(values.get(key.lower())),
            "case_matches": values.get(key, "") == values.get(key.lower(), ""),
        }
    bypass = {name: _bypass(name, values) for name in INTERNAL_SERVICES}
    return {
        "variables": variables,
        "internal_bypass": bypass,
        "all_internal_bypassed": all(bypass.values()),
        "bypass_semantics": "python-urllib; JVM and SDK behavior differs",
        "custom_ca_configured": bool(
            values.get("SSL_CERT_FILE")
            or values.get("REQUESTS_CA_BUNDLE")
            or values.get("CORPORATE_CA_FILE")
        ),
        # Image build settings cannot be inferred from a site env file.
        "uv_system_certs": (
            values["UV_SYSTEM_CERTS"].lower() in {"1", "true"}
            if "UV_SYSTEM_CERTS" in values
            else None
        ),
    }


def _context(values):
    """Load a local CA bundle with standard TLS and hostname verification."""
    bundle = values.get("SSL_CERT_FILE") or values.get("REQUESTS_CA_BUNDLE")
    if bundle:
        context = ssl.create_default_context(cafile=str(_ca_file(bundle)))
    else:
        context = ssl.create_default_context()
    corporate = values.get("CORPORATE_CA_FILE")
    if corporate:
        # Saved-config checks add the proposed CA without discarding roots.
        context.load_verify_locations(cafile=str(_ca_file(corporate)))
    return context


def _ca_file(value):
    """Accept bounded regular public bundles without returning their bytes."""
    path = Path(value)
    if (
        not path.is_absolute()
        or not path.is_file()
        or any(part.is_symlink() for part in (path, *path.parents))
        or path.stat().st_size > 1024 * 1024
        or b"PRIVATE KEY" in path.read_bytes()
    ):
        raise ValueError("Invalid certificate bundle.")
    return path


def _request(target, values, timeout):
    """Contact one fixed HTTPS origin, optionally through HTTP CONNECT."""
    origin = urlsplit(TARGETS[target])
    proxy_value = _effective(values, "HTTPS_PROXY")
    use_proxy = bool(proxy_value) and not _bypass(origin.hostname, values)
    connection = None
    started = time.monotonic()
    result = {"target": target, "via_proxy": use_proxy, "ok": False}
    try:
        context = _context(values)
        if use_proxy:
            proxy = urlsplit(proxy_value)
            if (
                proxy.scheme not in {"http", "https"}
                or not proxy.hostname
                or proxy.port is None
                or proxy.path not in {"", "/"}
                or proxy.query
                or proxy.fragment
            ):
                raise ValueError("Invalid proxy settings.")
            if proxy.scheme == "https":
                result["code"] = "TLS_TO_PROXY_NOT_SUPPORTED"
                return result
            headers = {}
            if proxy.username is not None:
                credentials = (
                    unquote(proxy.username)
                    + ":"
                    + unquote(proxy.password or "")
                )
                encoded = base64.b64encode(credentials.encode()).decode()
                headers["Proxy-Authorization"] = "Basic " + encoded
            connection = http.client.HTTPSConnection(
                proxy.hostname, proxy.port, timeout=timeout, context=context
            )
            connection.set_tunnel(
                origin.hostname, origin.port or 443, headers=headers
            )
        else:
            connection = http.client.HTTPSConnection(
                origin.hostname,
                origin.port or 443,
                timeout=timeout,
                context=context,
            )
        connection.request(
            "HEAD",
            origin.path or "/",
            headers={"User-Agent": "lakehouse-proxy-doctor/1"},
        )
        response = connection.getresponse()
        result["status"] = response.status
        result["ok"] = 200 <= response.status < 300
        result["code"] = (
            "OK"
            if result["ok"]
            else "REDIRECT_BLOCKED"
            if 300 <= response.status < 400
            else "HTTP_REJECTED"
        )
    except ssl.SSLCertVerificationError:
        result["code"] = "TLS_VERIFY_FAILED"
    except ssl.SSLError:
        result["code"] = "TLS_FAILED"
    except socket.gaierror:
        result["code"] = "DNS_FAILED"
    except (TimeoutError, socket.timeout):
        result["code"] = "TIMEOUT"
    except ValueError:
        result["code"] = "INVALID_CONFIGURATION"
    except OSError as error:
        message = str(error)
        result["code"] = (
            "PROXY_AUTH_REQUIRED"
            if message.startswith("Tunnel connection failed: 407")
            else "PROXY_CONNECT_REJECTED"
            if message.startswith("Tunnel connection failed:")
            else "CONNECTION_FAILED"
        )
    except http.client.HTTPException:
        result["code"] = "HTTP_PROTOCOL_FAILED"
    finally:
        if connection is not None:
            connection.close()
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return result


def run_checks(values, targets=("pypi", "maven", "npm", "github"), timeout=5):
    """Probe selected fixed origins and return sanitized diagnostic JSON."""
    if (
        not isinstance(targets, (list, tuple))
        or not targets
        or any(
            not isinstance(item, str) or item not in TARGETS
            for item in targets
        )
        or len(set(targets)) != len(targets)
    ):
        raise ValueError("Choose distinct known proxy diagnostic targets.")
    if (
        type(timeout) not in {int, float}
        or not math.isfinite(timeout)
        or not 0 < timeout <= 10
    ):
        raise ValueError("Diagnostic timeout must be in 0..10 seconds.")
    with ThreadPoolExecutor(max_workers=4) as executor:
        checks = list(
            executor.map(
                lambda target: _request(target, values, timeout), targets
            )
        )
    return {
        "configuration": configuration_report(values),
        "checks": checks,
        "scope": "this Python process; not Docker daemon, builds or JVM",
    }
