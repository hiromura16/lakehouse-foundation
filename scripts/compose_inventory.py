#!/usr/bin/env python3
"""Derive a deterministic inventory from seven public Compose source files.

This never reads site.env, resolves interpolation, invokes Docker, inspects
the host, or measures runtime memory. Memory values are declared container
limits, not reservations or measured usage. Per-profile selections include
core; their totals must not be added together because core/shared services
would be counted repeatedly. all_profiles is the deduplicated union.
"""

import argparse
import hashlib
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path("docs/reports/COMPOSE-INVENTORY.json")
COMPOSE_FILES = tuple(
    f"infra/{name}/compose.yaml"
    for name in (
        "lakehouse",
        "compute",
        "orchestration",
        "demo",
        "cdc",
        "poc",
        "mcp",
    )
)
MAX_MEMORY = 1024**4
LIFECYCLES = ("resident", "one_shot", "explicit_only", "unknown")


class InventoryError(ValueError):
    """Expose fixed public-source diagnostics, without configuration values."""


class SourceLoader(yaml.SafeLoader):
    """Allow ordinary YAML merges but reject duplicate explicit keys."""

    def construct_mapping(self, node, deep=False):
        explicit = set()
        for key, _ in node.value:
            if key.tag == "tag:yaml.org,2002:merge":
                continue
            if (
                not isinstance(key, yaml.ScalarNode)
                or key.tag != "tag:yaml.org,2002:str"
            ):
                raise InventoryError("COMPOSE_MAPPING_KEY_INVALID")
            if key.value in explicit:
                raise InventoryError("COMPOSE_DUPLICATE_KEY")
            explicit.add(key.value)
        return super().construct_mapping(node, deep=deep)


def memory_bytes(value):
    """Parse bounded Compose byte values using binary Docker RAM units.

    Numeric suffixes k/m/g/t (and KB/KiB variants) use powers of 1024.
    Fractional bytes, interpolation, booleans, negatives, and >1 TiB values
    fail instead of silently producing stale or underestimated budgets.
    """
    if type(value) is int:
        result = value
    elif isinstance(value, str) and len(value) <= 32:
        match = re.fullmatch(
            r"([0-9]+(?:\.[0-9]+)?)\s*"
            r"([bB]|[kKmMgGtT](?:[iI]?[bB])?)?",
            value,
        )
        if not match:
            raise InventoryError("COMPOSE_MEMORY_INVALID")
        amount, suffix = match.groups()
        power = {"b": 0, "k": 1, "m": 2, "g": 3, "t": 4}[
            (suffix or "b")[0].lower()
        ]
        try:
            exact = Decimal(amount) * (1024**power)
            result = int(exact)
            if exact != result:
                raise InventoryError("COMPOSE_MEMORY_FRACTIONAL_BYTE")
        except (InvalidOperation, OverflowError):
            raise InventoryError("COMPOSE_MEMORY_INVALID") from None
    else:
        raise InventoryError("COMPOSE_MEMORY_INVALID")
    if not 0 < result <= MAX_MEMORY:
        raise InventoryError("COMPOSE_MEMORY_OUT_OF_RANGE")
    return result


def service_memory(service):
    """Check both Compose limit spellings and reject inconsistent sources."""
    first = service.get("mem_limit")
    second = (
        service.get("deploy", {})
        .get("resources", {})
        .get("limits", {})
        .get("memory")
    )
    values = [
        memory_bytes(value) for value in (first, second) if value is not None
    ]
    if not values or len(set(values)) != 1:
        raise InventoryError("COMPOSE_MEMORY_MISSING_OR_CONFLICTING")
    return values[0]


def replicas(service):
    """Count declared replicas; absent means Compose's default one instance."""
    value = service.get("deploy", {}).get("replicas", 1)
    if type(value) is not int or not 0 <= value <= 128:
        raise InventoryError("COMPOSE_REPLICAS_INVALID")
    return value


def lifecycle(name, service, completed):
    """Report source evidence of intent, never a measured process lifecycle.

    A completed-successfully dependency is strong one-shot intent. restart=no
    alone is insufficient: CDC's continuous consumer deliberately stops on
    failure. Its active healthcheck identifies resident intent. Unclassified
    sources remain unknown and retain a separate memory budget.
    """
    if replicas(service) == 0:
        return "explicit_only", "deploy.replicas=0"
    if name in completed:
        return "one_shot", "dependency.service_completed_successfully"
    restart = service.get("restart", "no")
    if restart in {"always", "unless-stopped"} or str(restart).startswith(
        "on-failure"
    ):
        return "resident", "restart-policy"
    health = service.get("healthcheck", {})
    if (
        isinstance(health, dict)
        and health.get("test")
        and health.get("disable") is not True
    ):
        return "resident", "active-healthcheck"
    return "unknown", "insufficient-source-lifecycle-evidence"


def volume_references(service):
    """Count named volumes separately from bind mounts and anonymous mounts."""
    named, binds, anonymous = set(), 0, 0
    for mount in service.get("volumes", []):
        if isinstance(mount, dict):
            kind = mount.get("type", "volume")
            source = mount.get("source")
            if kind == "bind":
                binds += 1
            elif kind == "volume" and isinstance(source, str) and source:
                named.add(source)
            elif kind == "volume" and not source:
                anonymous += 1
            elif kind != "tmpfs":
                raise InventoryError("COMPOSE_MOUNT_UNSUPPORTED")
        elif isinstance(mount, str):
            parts = mount.split(":")
            if len(parts) == 1:
                anonymous += 1
            elif parts[0].startswith(("/", "./", "../", "~")):
                binds += 1
            elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", parts[0]):
                named.add(parts[0])
            else:
                raise InventoryError("COMPOSE_MOUNT_UNSUPPORTED")
        else:
            raise InventoryError("COMPOSE_MOUNT_UNSUPPORTED")
    return sorted(named), binds, anonymous


def read_sources(root):
    """Read only fixed public YAML; avoid environment/config interpolation."""
    services, volumes, hashes = {}, {}, {}
    for relative in COMPOSE_FILES:
        path = root / relative
        if path.is_symlink() or any(
            parent.is_symlink()
            for parent in path.parents
            if parent != root and root in parent.parents
        ):
            raise InventoryError("COMPOSE_PUBLIC_SOURCE_SYMLINK")
        try:
            content = path.read_bytes()
            if len(content) > 1024 * 1024:
                raise InventoryError("COMPOSE_SOURCE_TOO_LARGE")
            document = yaml.load(content, Loader=SourceLoader)
        except (OSError, yaml.YAMLError):
            raise InventoryError("COMPOSE_PUBLIC_SOURCE_UNREADABLE") from None
        if not isinstance(document, dict) or not isinstance(
            document.get("services"), dict
        ):
            raise InventoryError("COMPOSE_SERVICES_INVALID")
        hashes[relative] = hashlib.sha256(content).hexdigest()
        for name, definition in document["services"].items():
            if not re.fullmatch(
                r"[a-z0-9][a-z0-9_-]{0,62}", name
            ) or not isinstance(definition, dict):
                raise InventoryError("COMPOSE_SERVICE_INVALID")
            if name in services:
                # The selected public files define disjoint services. A new
                # overlay needs deliberate merge semantics, not silent update.
                raise InventoryError("COMPOSE_SERVICE_DEFINED_TWICE")
            services[name] = {"source": relative, "definition": definition}
        volume_definitions = document.get("volumes") or {}
        if not isinstance(volume_definitions, dict):
            raise InventoryError("COMPOSE_VOLUME_INVALID")
        for name, definition in volume_definitions.items():
            definition = definition or {}
            if not isinstance(definition, dict):
                raise InventoryError("COMPOSE_VOLUME_INVALID")
            if name in volumes and volumes[name]["definition"] != definition:
                raise InventoryError("COMPOSE_VOLUME_CONFLICT")
            if name not in volumes:
                volumes[name] = {"definition": definition, "sources": []}
            volumes[name]["sources"].append(relative)
    return services, volumes, hashes


def budget(rows):
    """Sum each selected unique service once, respecting replica zero."""
    totals = {name + "_bytes": 0 for name in LIFECYCLES}
    instances = {name + "_instances": 0 for name in LIFECYCLES}
    counts = {name + "_services": 0 for name in LIFECYCLES}
    seen = set()
    for row in rows:
        if row["service"] in seen:
            raise InventoryError("COMPOSE_BUDGET_SERVICE_REPEATED")
        seen.add(row["service"])
        kind = row["lifecycle"]
        # Explicit checks have a per-invocation budget, but no resident slot.
        count = 1 if kind == "explicit_only" else row["replicas"]
        totals[kind + "_bytes"] += row["memory_limit_bytes"] * count
        instances[kind + "_instances"] += row["replicas"]
        counts[kind + "_services"] += 1
    return {
        **totals,
        **instances,
        **counts,
        "steady_resident_limit_bytes": totals["resident_bytes"],
        "startup_plus_resident_limit_bytes": totals["resident_bytes"]
        + totals["one_shot_bytes"],
        "all_declared_operations_limit_bytes": sum(totals.values()),
    }


def select_services(rows, profiles):
    """Select core and a profile union without repeating shared users."""
    enabled = set(profiles)
    known = {profile for row in rows for profile in row["profiles"]}
    if not enabled <= known:
        raise InventoryError("COMPOSE_PROFILE_UNKNOWN")
    return [
        row
        for row in rows
        if row["core"] or enabled.intersection(row["profiles"])
    ]


def derive(root=ROOT):
    """Generate counts without Docker, private settings or host inspection."""
    sources, volumes, hashes = read_sources(root)
    completed = set()
    for item in sources.values():
        dependencies = item["definition"].get("depends_on", {})
        if isinstance(dependencies, dict):
            completed.update(
                name
                for name, options in dependencies.items()
                if isinstance(options, dict)
                and options.get("condition")
                == "service_completed_successfully"
            )
    rows, profile_names, local = [], set(), defaultdict(list)
    for name, item in sorted(sources.items()):
        service = item["definition"]
        profiles = service.get("profiles", [])
        if (
            not isinstance(profiles, list)
            or any(
                not isinstance(profile, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", profile)
                for profile in profiles
            )
            or len(set(profiles)) != len(profiles)
        ):
            raise InventoryError("COMPOSE_PROFILE_INVALID")
        profile_names.update(profiles)
        kind, reason = lifecycle(name, service, completed)
        image = service.get("image")
        if not isinstance(image, str) or not image:
            raise InventoryError("COMPOSE_IMAGE_MISSING")
        owns_build = "build" in service
        named, binds, anonymous = volume_references(service)
        if any(volume not in volumes for volume in named):
            raise InventoryError("COMPOSE_VOLUME_UNDECLARED")
        row = {
            "service": name,
            "source": item["source"],
            "profiles": sorted(profiles),
            "core": not profiles,
            "image": image,
            "owns_build": owns_build,
            "local_image": image.endswith(":local"),
            "pull_policy": service.get("pull_policy", "compose-default"),
            "lifecycle": kind,
            "lifecycle_basis": reason,
            "restart_policy": service.get("restart", "no"),
            "replicas": replicas(service),
            "memory_limit_bytes": service_memory(service),
            "named_volumes": named,
            "bind_mount_count": binds,
            "anonymous_volume_count": anonymous,
        }
        rows.append(row)
        if row["local_image"]:
            local[image].append(row)
    image_rows = []
    for image, users in sorted(local.items()):
        owners = [row for row in users if row["owns_build"]]
        if len(owners) != 1:
            raise InventoryError("COMPOSE_LOCAL_IMAGE_OWNER_NOT_UNIQUE")
        image_rows.append(
            {
                "image": image,
                "builder": owners[0]["service"],
                "consumers": [
                    row["service"] for row in users if not row["owns_build"]
                ],
                "all_users": [row["service"] for row in users],
            }
        )
    profile_rows = []
    for profile in [None, *sorted(profile_names)]:
        chosen = select_services(rows, [] if profile is None else [profile])
        additional = [row for row in chosen if not row["core"]]
        profile_rows.append(
            {
                "profile": "core" if profile is None else profile,
                "service_count": len(chosen),
                "additional_service_count": len(additional),
                "services": [row["service"] for row in chosen],
                "budget_including_core": budget(chosen),
                "additional_budget": budget(additional),
                "named_volumes": sorted(
                    {name for row in chosen for name in row["named_volumes"]}
                ),
            }
        )
    volume_rows = [
        {
            "volume": name,
            "sources": sorted(item["sources"]),
            "name_template": item["definition"].get(
                "name", "compose-project-default"
            ),
            "external": item["definition"].get("external", False),
            "services": [
                row["service"] for row in rows if name in row["named_volumes"]
            ],
        }
        for name, item in sorted(volumes.items())
    ]
    return {
        "schema_version": 1,
        "kind": "public-compose-source-inventory",
        "runtime_measured": False,
        "scope": (
            "seven-public-files-with-YAML-anchors-"
            "no-env-interpolation-no-deployment"
        ),
        "memory_semantics": (
            "Docker-binary-RAM-units-container-limits-not-observed-usage"
        ),
        "profile_semantics": (
            "each-profile-includes-core-do-not-add-profile-totals"
        ),
        "compose_sha256": hashes,
        "counts": {
            "compose_files": len(COMPOSE_FILES),
            "services": len(rows),
            "core_services": sum(row["core"] for row in rows),
            "profiles": len(profile_names),
            "local_images": len(image_rows),
            "build_owners": sum(row["owns_build"] for row in rows),
            "local_image_consumers": sum(
                row["local_image"] and not row["owns_build"] for row in rows
            ),
            "named_volumes": len(volume_rows),
            "named_volume_service_references": sum(
                len(row["named_volumes"]) for row in rows
            ),
            "unknown_lifecycles": sum(
                row["lifecycle"] == "unknown" for row in rows
            ),
        },
        "all_profiles": {
            "services": [row["service"] for row in rows],
            "budget": budget(rows),
        },
        "profiles": profile_rows,
        "services": rows,
        "local_images": image_rows,
        "volumes": volume_rows,
    }


def rendered(root=ROOT):
    """Return stable UTF-8 JSON; input hashes detect otherwise hidden drift."""
    return (
        json.dumps(derive(root), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )


def main(argv=None):
    """Generate or check the one repository-owned public report artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        content = rendered(ROOT)
        output = ROOT / OUTPUT
        if output.is_symlink():
            raise InventoryError("COMPOSE_INVENTORY_OUTPUT_SYMLINK")
        if args.check:
            if not output.is_file() or output.read_text() != content:
                raise InventoryError("COMPOSE_INVENTORY_STALE")
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            if not output.is_file() or output.read_text() != content:
                output.write_text(content)
    except (InventoryError, OSError, TypeError, KeyError, AttributeError):
        print(
            "FAIL: public Compose inventory needs source review/regeneration."
        )
        return 1
    print(
        "PASS: public Compose inventory checked."
        if args.check
        else "PASS: public Compose inventory generated."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
