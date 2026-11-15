"""Fixed Compose groups; browser inputs never become Docker arguments."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Group:
    """Keep dependency starts separate from daemons that may be stopped."""

    id: str
    cards: tuple[str, ...]
    daemons: tuple[str, ...]
    starts: tuple[str, ...]
    profiles: tuple[str, ...]


GROUPS = {
    item.id: item
    for item in (
        Group(
            "monitoring",
            ("grafana", "prometheus"),
            ("prometheus", "grafana", "loki", "vector"),
            ("prometheus", "grafana", "loki", "vector"),
            ("monitoring",),
        ),
        Group(
            "automation",
            ("n8n",),
            ("n8n",),
            ("n8n-db", "n8n"),
            ("automation",),
        ),
        Group(
            "bi",
            ("superset",),
            ("superset",),
            ("superset-db", "superset-redis", "superset"),
            ("bi",),
        ),
        Group(
            "compute",
            ("spark-master", "spark-worker", "spark-history"),
            ("spark-master", "spark-worker", "spark-history"),
            (
                "spark-volumes-init",
                "spark-master",
                "spark-worker",
                "spark-history",
            ),
            ("compute",),
        ),
        Group(
            "notebooks",
            ("jupyter",),
            ("jupyter",),
            ("spark-volumes-init", "jupyter"),
            ("notebooks",),
        ),
        Group(
            "openobserve-lab",
            ("openobserve",),
            ("openobserve",),
            ("openobserve",),
            ("monitoring", "openobserve-lab"),
        ),
        Group(
            "sail-lab",
            ("sail-server",),
            ("sail-server",),
            (
                "spark-volumes-init",
                "spark-master",
                "spark-worker",
                "spark-history",
                "sail-server",
            ),
            ("compute", "sail-lab"),
        ),
    )
}
CARD_GROUP = {
    card: group.id for group in GROUPS.values() for card in group.cards
}
PROTECTED = frozenset(
    {
        "swagger",
        "demo",
        "demo-api",
        "worker",
        "catalog",
        "catalog-db",
        "storage",
        "trino",
    }
)
FORBIDDEN = frozenset(
    {
        "kafbat",
        "cdc",
        "cdc-connect",
        "cdc-register",
        "cdc-consumer",
        "monitoring-host",
        "node-exporter",
        "cadvisor",
    }
)
ACTIONS = frozenset({"start", "restart", "stop"})
OBSERVATIONS = frozenset({"running", "stopped", "unknown"})
FILES = tuple(
    "infra/" + name + "/compose.yaml"
    for name in (
        "lakehouse",
        "compute",
        "orchestration",
        "demo",
        "cdc",
        "poc",
        "mcp",
        "openobserve",
        "sail",
    )
)


def group_for(service_id):
    """Return one fixed group or a safe application-owned rejection code."""
    if service_id in PROTECTED:
        raise ValueError("SERVICE_PROTECTED")
    if service_id in FORBIDDEN:
        code = (
            "HOST_PRIVILEGE_APPROVAL_REQUIRED"
            if service_id in {"monitoring-host", "node-exporter", "cadvisor"}
            else "SOURCE_CONNECTION_APPROVAL_REQUIRED"
        )
        raise ValueError(code)
    if service_id not in CARD_GROUP:
        raise ValueError("SERVICE_UNSUPPORTED")
    return GROUPS[CARD_GROUP[service_id]]


def profile_set(values):
    """Read profile membership without changing applied connection settings."""
    return set(filter(None, values.get("COMPOSE_PROFILES", "").split(",")))
