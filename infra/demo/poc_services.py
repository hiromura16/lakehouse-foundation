"""Probe only known internal services and publish credential-free links."""

import ipaddress
from concurrent.futures import ThreadPoolExecutor

import httpx
from poc_catalog import SERVICES


def probe_services(values: dict[str, str]) -> dict[str, bool]:
    """Check enabled, configured services concurrently with a short timeout."""
    profiles = set(values.get("COMPOSE_PROFILES", "").split(","))
    active = [
        item
        for item in SERVICES
        if (not item.profiles or set(item.profiles) & profiles)
        and all(values.get(key) for key in item.required)
    ]
    with httpx.Client(
        timeout=0.8, trust_env=False, follow_redirects=False
    ) as client:

        def check(item):
            headers = {}
            if item.id == "jupyter":
                headers["Authorization"] = "token " + values["JUPYTER_TOKEN"]
            if item.id == "qdrant":
                headers["api-key"] = values["QDRANT_READ_ONLY_API_KEY"]
            try:
                response = client.get(item.health_url, headers=headers)
                return item.id, response.status_code == 200
            except httpx.HTTPError:
                return item.id, False

        with ThreadPoolExecutor(max_workers=6) as pool:
            return dict(pool.map(check, active))


def service_cards(values, health):
    """Separate unselected, missing config and unreachable service states."""
    profiles = set(values.get("COMPOSE_PROFILES", "").split(","))
    address = ipaddress.ip_address(values.get("BIND_IP", "127.0.0.1"))
    host = "localhost" if address.is_loopback else str(address)
    result = []
    for item in SERVICES:
        enabled = not item.profiles or bool(set(item.profiles) & profiles)
        missing = [key for key in item.required if not values.get(key)]
        state = "not_enabled"
        if enabled:
            state = (
                "unconfigured"
                if missing
                else ("available" if health.get(item.id) else "unreachable")
            )
        port = int(values.get(item.port_key, str(item.port)))
        # Optional PoC and CDC ports deliberately bind to host loopback.
        link_host = (
            "localhost"
            if item.profile in {"cdc", "bi", "notebooks", "vector", "lineage"}
            else host
        )
        result.append(
            {
                "id": item.id,
                "title": item.title,
                "profile": item.profile,
                "profiles": list(item.profiles),
                "profile_match": "any",
                "enabled": enabled,
                "configured": not missing,
                "state": state,
                "url": f"http://{link_host}:{port}{item.path}",
                "description": item.description
                + (
                    " 変更toolは有効です。"
                    if item.id == "mcp"
                    and values.get("MCP_ENABLE_MUTATIONS") == "true"
                    else " 読み取りtoolだけを公開します。"
                    if item.id == "mcp"
                    else ""
                ),
                "missing_settings": missing,
            }
        )
    return result
