"""Register one explicit SQL Server CDC connector through Kafka Connect."""

import argparse
import json
import os
import re
import sys
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from config import mappings, read_env, settings


def configuration(values, mapping):
    """Build a TLS-required connector with environment credential
    references.
    """
    selected = [
        re.escape(item["source_schema"] + "." + item["source_table"])
        for item in mapping.values()
    ]
    selected_columns = [
        re.escape(
            item["source_schema"] + "." + item["source_table"] + "." + column
        )
        for item in mapping.values()
        for column in item["columns"]
    ]
    result = {
        "connector.class": (
            "io.debezium.connector.sqlserver.SqlServerConnector"
        ),
        "tasks.max": "1",
        "database.hostname": values["CDC_SQLSERVER_HOST"],
        "database.port": values["CDC_SQLSERVER_PORT"],
        "database.names": values["CDC_SQLSERVER_DATABASE"],
        "database.user": "${env:CDC_SQLSERVER_USER}",
        "database.password": "${env:CDC_SQLSERVER_PASSWORD}",
        "topic.prefix": values["CDC_TOPIC_PREFIX"],
        "table.include.list": ",".join(selected),
        "column.include.list": ",".join(selected_columns),
        "schema.history.internal.kafka.bootstrap.servers": "cdc-kafka:9092",
        "schema.history.internal.kafka.topic": (
            "_history_" + values["CDC_TOPIC_PREFIX"]
        ),
        "snapshot.mode": "initial",
        "snapshot.isolation.mode": "read_committed",
        "include.schema.changes": "false",
        "tombstones.on.delete": "true",
        "decimal.handling.mode": "string",
        "time.precision.mode": "connect",
        "driver.encrypt": "true",
        "driver.trustServerCertificate": values[
            "CDC_TEST_TRUST_SERVER_CERTIFICATE"
        ],
        "key.converter": "org.apache.kafka.connect.json.JsonConverter",
        "key.converter.schemas.enable": "false",
        "value.converter": "org.apache.kafka.connect.json.JsonConverter",
        "value.converter.schemas.enable": "false",
        "topic.creation.default.partitions": "1",
        "topic.creation.default.replication.factor": "1",
    }
    truststore = values.get("CDC_SQLSERVER_TRUSTSTORE", "")
    if truststore:
        if not truststore.startswith("/run/cdc/") or ".." in truststore:
            raise ValueError("Mount the SQL truststore under /run/cdc/.")
        result["driver.trustStore"] = truststore
        result["driver.trustStorePassword"] = (
            "${env:CDC_SQLSERVER_TRUSTSTORE_PASSWORD}"
        )
    hostname = values.get("CDC_SQLSERVER_HOSTNAME_IN_CERTIFICATE", "")
    if hostname:
        if not re.fullmatch(r"[A-Za-z0-9_.*-]+", hostname):
            raise ValueError("Invalid SQL certificate hostname.")
        result["driver.hostNameInCertificate"] = hostname
    agent = values.get("CDC_SQLSERVER_AGENT_STATUS_QUERY", "")
    if agent:
        # A database administrator may expose a signed read-only status UDF.
        # Do not accept arbitrary SQL through a shared configuration field.
        if not re.fullmatch(
            r"SELECT \[#db\]\.\[?[A-Za-z_][A-Za-z0-9_]*\]?"
            r"\.\[?[A-Za-z_][A-Za-z0-9_]*\]?\(\)",
            agent,
            re.IGNORECASE,
        ):
            raise ValueError("Use a DBA-approved scalar status function.")
        result["database.sqlserver.agent.status.query"] = agent
    return result


def register(values, mapping, endpoint):
    """Create once or confirm an identical existing config without writes."""
    opener = build_opener(ProxyHandler({}))
    name = values["CDC_CONNECTOR_NAME"]
    candidate = configuration(values, mapping)
    deadline = time.monotonic() + 180
    while True:
        try:
            with opener.open(endpoint + "/connectors", timeout=5) as reply:
                if reply.status != 200:
                    raise RuntimeError("Kafka Connect discovery was rejected.")
                existing = json.load(reply)
            if not isinstance(existing, list) or not all(
                isinstance(item, str) for item in existing
            ):
                raise ValueError("Invalid Kafka Connect connector list.")
            break
        except HTTPError:
            # Authentication and HTTP failures need review, not a create retry.
            raise RuntimeError(
                "Kafka Connect discovery was rejected."
            ) from None
        except OSError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Kafka Connect did not become ready.")
            time.sleep(2)
    if name in existing:
        with opener.open(
            endpoint + "/connectors/" + name + "/config", timeout=5
        ) as reply:
            if reply.status != 200:
                raise RuntimeError("Connector configuration was unavailable.")
            current = json.load(reply)
        if not isinstance(current, dict):
            raise ValueError("Invalid existing connector configuration.")
        # Connect may add its own name property. Only that exact value may be
        # removed; every other key and value must match without coercion.
        if "name" in current:
            if current["name"] != name:
                raise ValueError("Existing connector name did not match.")
            current = {
                key: value for key, value in current.items() if key != "name"
            }
        if current != candidate:
            raise ValueError(
                "Connector configuration differs; review it before changing."
            )
        print(
            json.dumps(
                {"registered": True, "unchanged": True, "tables": len(mapping)}
            )
        )
        return
    payload = {"name": name, "config": candidate}
    request = Request(
        endpoint + "/connectors",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=30) as response:
        if response.status != 201:
            raise RuntimeError("Connector registration was not accepted.")
    print(json.dumps({"registered": True, "tables": len(mapping)}))


def main():
    """Register without displaying credentials or private database names."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file")
    parser.add_argument("--table-map", default="/run/cdc/table-map.json")
    parser.add_argument("--endpoint", default="http://cdc-connect:8083")
    args = parser.parse_args()
    values = settings(read_env(args.env_file) if args.env_file else os.environ)
    if args.endpoint not in {
        "http://cdc-connect:8083",
        "http://127.0.0.1:" + values["CDC_CONNECT_PORT"],
        "http://localhost:" + values["CDC_CONNECT_PORT"],
    }:
        raise ValueError(
            "Use the internal or loopback Kafka Connect endpoint."
        )
    register(values, mappings(args.table_map), args.endpoint)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, HTTPError, OSError):
        print("CDC connector registration failed; inspect private status.")
        sys.exit(1)
