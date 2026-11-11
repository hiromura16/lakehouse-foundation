"""Keep fixed CDC on the owned six-table source and the read-only principal.

The registration preflight only executes metadata SELECTs. It cannot enable
CDC, alter permissions, create SQL objects or write a checkpoint. SQL Server
DDL is a separate, manually reviewed DBA operation.
"""

import importlib
import re
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "medallion").is_dir():
    ROOT = Path(__file__).resolve().parents[1]
FIXED_PREFIX = "lakehouse-demo-sqlserver"
FIXED_GROUP = FIXED_PREFIX + "-consumer"
EXTRA_COLUMNS = ("original_values", "run_id", "created_at")


@lru_cache(maxsize=1)
def source_adapter():
    """Load the common ownership and reader checks without a second driver."""
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("sqlserver_demo")


def select_settings(values):
    """Derive CDC's internal names from SOURCE settings, never writer auth.

    The public private-env interface stays SOURCE_SQLSERVER_*. The internal
    CDC names allow existing generic consumer code to compare the same source
    identity. Connector credentials remain SOURCE environment references.
    """
    certificate = values.get("SOURCE_SQLSERVER_CA_FILE", "")
    if certificate == "/run/source-certs/source-ca.pem" and (
        not Path(certificate).is_file()
        or Path(certificate).stat().st_size == 0
    ):
        raise ValueError(
            "Mount the approved SOURCE PEM with a private Compose override; "
            "the empty CDC placeholder cannot provide certificate trust."
        )
    if (
        certificate not in {"", "system"}
        and values.get("SOURCE_SQLSERVER_TRUST_SERVER_CERTIFICATE", "false")
        == "false"
        and not values.get("CDC_SQLSERVER_TRUSTSTORE")
    ):
        raise ValueError(
            "Custom SOURCE PEM does not configure Java trust; also select "
            "the approved CDC SQL Server truststore."
        )
    selected = source_adapter().validate_settings(values, role="source")
    if selected.get("SOURCE_SQLSERVER_TIMEZONE", "UTC") != "UTC":
        raise ValueError(
            "Fixed CDC requires the approved UTC source contract."
        )
    selected.update(
        CDC_TOPIC_PREFIX=FIXED_PREFIX,
        CDC_CONNECTOR_NAME=FIXED_PREFIX,
        CDC_GROUP_ID=FIXED_GROUP,
        CDC_TEST_TRUST_SERVER_CERTIFICATE="false",
    )
    for suffix in ("HOST", "PORT", "DATABASE", "USER", "PASSWORD"):
        selected["CDC_SQLSERVER_" + suffix] = selected.get(
            "SOURCE_SQLSERVER_" + suffix, "1433" if suffix == "PORT" else ""
        )
    return selected


def table_mapping():
    """Return fixed mappings without accepting a request or private map."""
    return {
        name: {
            "target_table": name,
            "source_schema": item["schema"],
            "source_table": item["table"],
            "columns": {column: column for column in item["columns"]},
        }
        for name, item in source_adapter().TABLES.items()
    }


def validate_mapping(mapping):
    """Reject substituted or additional tables even on an internal call."""
    if mapping != table_mapping():
        raise ValueError("Fixed CDC accepts only the six owned demo tables.")


def preflight(values, mapping, *, adapter=None):
    """Check the connected identity, reader boundary and exact CDC captures.

    Successful checks are local observations, not evidence that Kafka, SQL
    Server Agent, or the full Lakehouse pipeline has processed an event.
    Metadata hidden from the selected principal causes a safe refusal.
    """
    adapter = source_adapter() if adapter is None else adapter
    validate_mapping(mapping)
    selected = adapter.validate_settings(values, role="source")
    try:
        with adapter.connection(selected, role="source") as handle:
            cursor = handle.cursor()
            adapter.ownership(cursor, selected, role="source")
            if not adapter.permissions_safe(cursor, role="source"):
                raise ValueError("Fixed CDC reader permissions are unsafe.")
            cursor.execute(
                "SELECT is_cdc_enabled FROM sys.databases "
                "WHERE database_id=DB_ID()"
            )
            if cursor.fetchone() != (True,):
                raise ValueError("Fixed source database CDC is not ready.")
            for name, item in adapter.TABLES.items():
                capture = adapter.CAPTURE_INSTANCES[name]
                qualified = item["schema"] + "." + item["table"]
                cursor.execute(
                    "SELECT ct.capture_instance,ct.role_name "
                    "FROM cdc.change_tables ct JOIN sys.tables t ON "
                    "ct.source_object_id=t.object_id WHERE "
                    "t.object_id=OBJECT_ID(%s) AND t.is_tracked_by_cdc=1",
                    (qualified,),
                )
                if cursor.fetchall() != [(capture, "lh_demo_cdc_reader")]:
                    raise ValueError("Fixed CDC capture ownership differs.")
                cursor.execute(
                    "SELECT cc.column_name FROM cdc.captured_columns cc "
                    "JOIN cdc.change_tables ct ON cc.object_id=ct.object_id "
                    "WHERE ct.capture_instance=%s",
                    (capture,),
                )
                captured = [row[0] for row in cursor.fetchall()]
                expected = [*item["columns"], *EXTRA_COLUMNS]
                if len(captured) != len(expected) or set(captured) != set(
                    expected
                ):
                    raise ValueError("Fixed CDC captured columns differ.")
                # JsonConverter has schemas disabled, so the consumer must
                # know that every typed timestamp uses nanosecond units.
                cursor.execute(
                    "SELECT c.name,ty.name,c.scale FROM sys.columns c "
                    "JOIN sys.types ty ON c.user_type_id=ty.user_type_id "
                    "WHERE c.object_id=OBJECT_ID(%s)",
                    (qualified,),
                )
                types = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
                timestamp_columns = {"updated_at", "created_at"} | (
                    set(item["columns"]) & adapter.TIME_COLUMNS
                )
                if any(
                    types.get(column) != ("datetime2", 7)
                    for column in timestamp_columns
                ):
                    raise ValueError("Fixed CDC timestamp precision differs.")
                cursor.execute("SELECT IS_MEMBER(N'lh_demo_cdc_reader')")
                if cursor.fetchone() != (1,):
                    raise ValueError("Fixed CDC reader role is missing.")
    except Exception:
        # Driver messages can contain SQL names, credentials and server text.
        raise ValueError(
            "Fixed CDC preflight failed; review ownership, read-only "
            "permissions and the six captures privately."
        ) from None
    return {"checked": True, "tables": 6, "schema_version": 1}


def validate_state_volume(value):
    """Permit an optional reviewed volume name without interpreting a path."""
    if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("Invalid CDC state volume name.")
