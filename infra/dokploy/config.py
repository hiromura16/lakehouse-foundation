#!/usr/bin/env python3
"""Validate and render portable Dokploy settings without evaluating shell code.

The installer calls these small commands for configuration and JSON checks.
Only render-nginx writes a file, at the installer-selected configuration path.
"""

import argparse
import ipaddress
import json
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETTING_KEYS = (
    "DOKPLOY_BIND_IP",
    "DOKPLOY_ALLOWED_CIDRS",
    "DOKPLOY_ADMIN_PORT",
    "SWARM_ADVERTISE_ADDR",
    "SWARM_ADDRESS_POOL",
    "SWARM_SUBNET_SIZE",
    "DOCKER_BRIDGE_CIDR",
)
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
CLIENT_NETWORKS = PRIVATE_NETWORKS + (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
)
NGINX_TEMPLATE = """events {}
http {
  map $http_upgrade $connection_upgrade { default upgrade; '' close; }
  server {
    listen 80;
    resolver 127.0.0.11 valid=10s ipv6=off;
__ALLOWED_NETWORKS__
    deny all;
    location / {
      set $upstream dokploy:3000;
      proxy_pass http://$upstream;
      proxy_http_version 1.1;
      proxy_set_header Host $http_host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_read_timeout 3600s;
      client_max_body_size 20m;
    }
  }
}
"""


def private_address(value, setting, allow_loopback):
    """Accept a configured private IPv4 address, with optional loopback."""
    address = ipaddress.ip_address(value)
    if (
        address.version != 4
        or not any(address in network for network in CLIENT_NETWORKS)
        or (address.is_loopback and not allow_loopback)
    ):
        raise ValueError(setting + " must be a private unicast IPv4 address.")
    return str(address)


def private_network(value, setting, allowed):
    """Parse a canonical IPv4 CIDR contained by an explicitly allowed range."""
    network = ipaddress.ip_network(value, strict=True)
    if network.version != 4 or not any(
        network.subnet_of(parent) for parent in allowed
    ):
        raise ValueError(setting + " must be a restricted private IPv4 CIDR.")
    return network


def read_env(path):
    """Return only validated Dokploy settings from an external private env."""
    path = Path(path).expanduser()
    if not path.is_file() or path.is_symlink():
        raise ValueError("Use a regular private env file, not a symlink.")
    if path.resolve().is_relative_to(ROOT):
        raise ValueError("The env file must be outside the source repository.")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("The env file must have mode 0600 or stricter.")
    values = {}
    for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(
                f"Invalid env assignment at line {number}. "
                "Use KEY=unquotedASCII."
            )
        # Other settings can include source credentials and proxy passwords.
        # Do not interpret, emit, or import them into this environment.
        if key not in SETTING_KEYS:
            continue
        if key in values or not re.fullmatch(r"[A-Za-z0-9.,:/_-]*", value):
            raise ValueError(
                f"Invalid or duplicate Dokploy configuration at line {number}."
            )
        values[key] = value
    optional = {"DOKPLOY_ADMIN_PORT", "DOCKER_BRIDGE_CIDR"}
    for key in SETTING_KEYS:
        if not values.get(key) and key not in optional:
            raise ValueError("Missing " + key + ". Configure it first.")
    values["DOKPLOY_BIND_IP"] = private_address(
        values["DOKPLOY_BIND_IP"], "DOKPLOY_BIND_IP", True
    )
    values["SWARM_ADVERTISE_ADDR"] = private_address(
        values["SWARM_ADVERTISE_ADDR"], "SWARM_ADVERTISE_ADDR", False
    )
    clients = values["DOKPLOY_ALLOWED_CIDRS"].split(",")
    values["DOKPLOY_ALLOWED_CIDRS"] = ",".join(
        dict.fromkeys(
            str(
                private_network(
                    value, "DOKPLOY_ALLOWED_CIDRS", CLIENT_NETWORKS
                )
            )
            for value in clients
        )
    )
    pool = private_network(
        values["SWARM_ADDRESS_POOL"], "SWARM_ADDRESS_POOL", PRIVATE_NETWORKS
    )
    values["SWARM_ADDRESS_POOL"] = str(pool)
    try:
        subnet = int(values["SWARM_SUBNET_SIZE"])
        port = int(values.get("DOKPLOY_ADMIN_PORT") or "3100")
    except ValueError:
        raise ValueError(
            "Subnet size and admin port must be integers."
        ) from None
    if not pool.prefixlen <= subnet <= 28:
        raise ValueError("Subnet size must be between the pool prefix and 28.")
    if not 1024 <= port <= 65535 or port in {2377, 4789, 7946}:
        raise ValueError("Use an admin port excluding privileged/Swarm ports.")
    values["SWARM_SUBNET_SIZE"] = str(subnet)
    values["DOKPLOY_ADMIN_PORT"] = str(port)
    bridge = values.get("DOCKER_BRIDGE_CIDR", "")
    if bridge:
        bridge = private_network(
            bridge, "DOCKER_BRIDGE_CIDR", PRIVATE_NETWORKS
        )
        if bridge.overlaps(pool):
            raise ValueError("Docker bridge and Swarm address pool overlap.")
        values["DOCKER_BRIDGE_CIDR"] = str(bridge)
    else:
        values["DOCKER_BRIDGE_CIDR"] = ""
    return values


def validate_swarm(swarm, pool, subnet):
    """Refuse changes to the recorded Swarm's immutable network allocation."""
    if (
        not isinstance(swarm, dict)
        or swarm.get("DefaultAddrPool") != [pool]
        or int(swarm.get("SubnetSize", -1)) != int(subnet)
    ):
        raise ValueError("Existing Swarm pool differs. Do not change it here.")


def validate_ports(actual, kind, admin_port, bind_ip, ui_ips):
    """Compare exact bindings, rejecting old or wildcard addresses."""
    actual = actual or {}
    if kind == "gateway":
        expected = {"80/tcp": [(address, admin_port) for address in ui_ips]}
    else:
        expected = {
            "80/tcp": [(bind_ip, "80")],
            "443/tcp": [(bind_ip, "443")],
            "443/udp": [(bind_ip, "443")],
        }
    normalized = {
        key: sorted(
            (item["HostIp"], item["HostPort"]) for item in values or []
        )
        for key, values in actual.items()
    }
    if normalized != {key: sorted(values) for key, values in expected.items()}:
        raise ValueError(
            "Existing private port bindings differ. Reconcile them first."
        )


def validate_host_networks(interfaces, pool):
    """Reject an address pool that overlaps an assigned host network."""
    pool = ipaddress.ip_network(pool)
    for interface in interfaces:
        for address in interface.get("addr_info", []):
            network = ipaddress.ip_interface(
                address["local"] + "/" + str(address["prefixlen"])
            ).network
            if not network.is_loopback and pool.overlaps(network):
                raise ValueError(
                    "Swarm pool overlaps a host network. Choose another pool."
                )


def inspect_bridge(configuration, pool, expected):
    """Return Docker's gateway subnet, checking overlap and optional match."""
    networks = [
        ipaddress.ip_network(item["Subnet"])
        for item in configuration
        if item.get("Subnet") and ":" not in item["Subnet"]
    ]
    if len(networks) != 1 or networks[0].is_global or networks[0].is_loopback:
        raise ValueError(
            "Expected one private IPv4 subnet on docker_gwbridge."
        )
    if networks[0].overlaps(ipaddress.ip_network(pool)):
        raise ValueError("Docker gateway bridge overlaps the Swarm pool.")
    if expected and networks[0] != ipaddress.ip_network(expected):
        raise ValueError("DOCKER_BRIDGE_CIDR does not match docker_gwbridge.")
    return str(networks[0])


def render_nginx(path, allowed, pool, bridge):
    """Render only the configured client, overlay and detected bridge ACLs."""
    networks = list(
        dict.fromkeys(["127.0.0.0/8", *allowed.split(","), pool, bridge])
    )
    rules = "\n".join("    allow " + network + ";" for network in networks)
    path = Path(path)
    path.write_text(NGINX_TEMPLATE.replace("__ALLOWED_NETWORKS__", rules))
    path.chmod(0o644)


def arguments(argv=None):
    """Describe the installer's small validation/rendering subcommands."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("read-env")
    read.add_argument("path", type=Path)
    swarm = commands.add_parser("swarm")
    swarm.add_argument("pool")
    swarm.add_argument("subnet")
    ports = commands.add_parser("ports")
    ports.add_argument("kind", choices=["gateway", "traefik"])
    ports.add_argument("admin_port")
    ports.add_argument("bind_ip")
    ports.add_argument("ui_ips", nargs="*")
    host = commands.add_parser("host-networks")
    host.add_argument("pool")
    bridge = commands.add_parser("bridge")
    bridge.add_argument("pool")
    bridge.add_argument("expected", nargs="?", default="")
    nginx = commands.add_parser("render-nginx")
    nginx.add_argument("path", type=Path)
    nginx.add_argument("allowed")
    nginx.add_argument("pool")
    nginx.add_argument("bridge")
    return parser.parse_args(argv)


def main(argv=None):
    """Dispatch a fixed helper operation, consuming Docker JSON on stdin."""
    args = arguments(argv)
    if args.command == "read-env":
        values = read_env(args.path)
        for key in SETTING_KEYS:
            print(values[key])
    elif args.command == "swarm":
        validate_swarm(json.load(sys.stdin), args.pool, args.subnet)
    elif args.command == "ports":
        validate_ports(
            json.load(sys.stdin),
            args.kind,
            args.admin_port,
            args.bind_ip,
            args.ui_ips,
        )
    elif args.command == "host-networks":
        validate_host_networks(json.load(sys.stdin), args.pool)
    elif args.command == "bridge":
        print(inspect_bridge(json.load(sys.stdin), args.pool, args.expected))
    elif args.command == "render-nginx":
        render_nginx(args.path, args.allowed, args.pool, args.bridge)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
