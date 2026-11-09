"""Validate CDC settings and six explicit source-to-contract mappings."""

import json
import os
import re
from pathlib import Path

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,62}")
ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "medallion").is_dir():
    ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = json.loads((ROOT / "medallion/sources.json").read_text())[
    "sources"
]


def read_env(path):
    """Parse a private external env file literally, never as shell code."""
    path = Path(path).expanduser().absolute()
    if (
        path.resolve().is_relative_to(ROOT)
        or not path.is_file()
        or path.stat().st_mode & 0o077
        or any(part.is_symlink() for part in (path, *path.parents))
    ):
        raise ValueError("Use an external regular env file with mode 0600.")
    values = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or key in values
            or any(ord(char) < 32 or ord(char) > 126 for char in value)
        ):
            raise ValueError("Invalid private environment assignment.")
        values[key] = value
    return values


def settings(values=None):
    """Select CDC credentials and validate transport and namespace settings."""
    values = dict(os.environ if values is None else values)
    for suffix in ["HOST", "DATABASE", "USER", "PASSWORD"]:
        if not values.get("CDC_SQLSERVER_" + suffix):
            raise ValueError("Complete the separate CDC_SQLSERVER settings.")
    host = values["CDC_SQLSERVER_HOST"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        raise ValueError("Use a DNS name or IPv4 CDC host.")
    database = values["CDC_SQLSERVER_DATABASE"]
    if not IDENTIFIER.fullmatch(database) or database.casefold() in {
        "master",
        "model",
        "msdb",
        "tempdb",
    }:
        raise ValueError("Use an application database identifier for CDC.")
    defaults = {
        "CDC_SQLSERVER_PORT": "1433",
        "CDC_TOPIC_PREFIX": "factory",
        "CDC_CONNECTOR_NAME": "factory-source",
        "CDC_GROUP_ID": "lakehouse-cdc",
        "CDC_BATCH_SIZE": "100",
        "CDC_POLL_SECONDS": "5",
        "CDC_CONNECT_PORT": "8083",
        "CDC_TEST_TRUST_SERVER_CERTIFICATE": "false",
    }
    for key, default in defaults.items():
        values.setdefault(key, default)
    for key in ["CDC_TOPIC_PREFIX", "CDC_CONNECTOR_NAME", "CDC_GROUP_ID"]:
        if not NAME.fullmatch(values[key]):
            raise ValueError("Invalid CDC component name.")
    for key, maximum in [
        ("CDC_SQLSERVER_PORT", 65535),
        ("CDC_CONNECT_PORT", 65535),
        ("CDC_BATCH_SIZE", 1000),
        ("CDC_POLL_SECONDS", 60),
    ]:
        value = values[key]
        if not value.isdigit() or not 1 <= int(value) <= maximum:
            raise ValueError("Invalid CDC numeric setting.")
    test = values["CDC_TEST_TRUST_SERVER_CERTIFICATE"]
    if test not in {"false", "true"}:
        raise ValueError("CDC test certificate flag must be true or false.")
    if test == "true" and (
        host not in {"localhost", "127.0.0.1"}
        or not database.startswith("lh_cdc_demo_")
    ):
        raise ValueError("Certificate bypass is limited to loopback demo DBs.")
    return values


def reject_duplicate_keys(pairs):
    """Reject ambiguous duplicate JSON mapping keys."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate mapping key.")
        result[key] = value
    return result


def mappings(path):
    """Accept only six explicit, one-to-one table and column mappings."""
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError("Provide the private table-map.json file.")
    document = json.loads(
        path.read_text(), object_pairs_hook=reject_duplicate_keys
    )
    if not isinstance(document, dict) or set(document) != {"tables"}:
        raise ValueError("A CDC mapping must contain a tables array.")
    tables = document["tables"]
    if not isinstance(tables, list) or len(tables) != len(CONTRACTS):
        raise ValueError("Map all six contract tables exactly once.")
    declared = {item["table"]: item for item in CONTRACTS}
    selected = {}
    origins = set()
    for item in tables:
        if not isinstance(item, dict) or set(item) != {
            "target_table",
            "source_schema",
            "source_table",
            "columns",
        }:
            raise ValueError("Invalid CDC mapping entry.")
        target = item["target_table"]
        if target not in declared or target in selected:
            raise ValueError("Unknown or duplicate target table.")
        for key in ["source_schema", "source_table"]:
            if not isinstance(item[key], str) or not IDENTIFIER.fullmatch(
                item[key]
            ):
                raise ValueError("Invalid source SQL identifier.")
        columns = item["columns"]
        if (
            not isinstance(columns, dict)
            or not all(IDENTIFIER.fullmatch(key) for key in columns)
            or list(sorted(columns.values()))
            != sorted(declared[target]["columns"])
            or len(columns) != len(declared[target]["columns"])
        ):
            raise ValueError("Map every contract column exactly once.")
        origin = (item["source_schema"], item["source_table"])
        if origin in origins:
            raise ValueError("One source table cannot target two tables.")
        origins.add(origin)
        selected[target] = item
    return selected


def topic_names(values, mapping):
    """Support SQL Server topic naming with or without its database token."""
    names = {}
    prefix = values["CDC_TOPIC_PREFIX"]
    database = values["CDC_SQLSERVER_DATABASE"]
    for target, item in mapping.items():
        suffix = item["source_schema"] + "." + item["source_table"]
        for middle in ["", database + "."]:
            names[prefix + "." + middle + suffix] = target
    return names
