#!/usr/bin/env python3
"""Provision an isolated synthetic CDC database or apply fixed source DML.

This helper never starts Kafka, Debezium, a worker, or any downstream job.
Normal company source databases cannot pass the demo ownership checks.
"""

import argparse
import importlib.util
import json
import os
import re
import secrets
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "infra/cdc"))
from config import CONTRACTS, IDENTIFIER, read_env  # noqa: E402

CONNECTION_LOCK = threading.Lock()
DEMO_PREFIX = "lh_cdc_demo_"


def generator():
    """Reuse synthetic records without invoking the source generator CLI."""
    specification = importlib.util.spec_from_file_location(
        "cdc_demo_generator", ROOT / "scripts/generate-demo.py"
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def validate_admin(values, trust_local_test):
    """Validate separate demo provisioning credentials and explicit TLS
    trust.
    """
    for key in ["HOST", "USER", "PASSWORD"]:
        if not values.get("DEMO_SQLSERVER_" + key):
            raise ValueError("Complete separate DEMO_SQLSERVER settings.")
    host = values["DEMO_SQLSERVER_HOST"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        raise ValueError("Invalid demo SQL host.")
    port = values.get("DEMO_SQLSERVER_PORT", "1433")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("Invalid demo SQL port.")
    if trust_local_test and host not in {"localhost", "127.0.0.1"}:
        raise ValueError("The test certificate exception requires loopback.")
    return host, port


@contextmanager
def connect(values, database, trust_local_test=False):
    """Require encrypted FreeTDS sessions with normal CA and hostname
    checks.
    """
    host, port = validate_admin(values, trust_local_test)
    if database != "master" and not re.fullmatch(
        DEMO_PREFIX + r"[0-9a-f]{16}", database
    ):
        raise ValueError("Only a new owned CDC demo database is writable.")
    ca_file = values.get("DEMO_SQLSERVER_CA_FILE", "system") or "system"
    if ca_file != "system" and (
        not Path(ca_file).is_file()
        or any(char in ca_file for char in "\r\n[]")
    ):
        raise ValueError("Provide an existing demo CA PEM file.")
    certificate_name = values.get("DEMO_SQLSERVER_HOSTNAME_IN_CERTIFICATE", "")
    if certificate_name and not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,253}", certificate_name
    ):
        raise ValueError("Invalid demo certificate hostname.")
    with (
        CONNECTION_LOCK,
        tempfile.TemporaryDirectory(prefix="cdc-demo-tls-") as directory,
    ):
        config = Path(directory) / "freetds.conf"
        trust = (
            ""
            if trust_local_test
            else (
                "    ca file = " + ca_file + "\n"
                "    check certificate hostname = yes\n"
                + (
                    "    certificate hostname = " + certificate_name + "\n"
                    if certificate_name
                    else ""
                )
            )
        )
        config.write_text(
            "[global]\n    encryption = require\n    tds version = 7.4\n"
            + trust
            + "[cdc_demo]\n    host = "
            + host
            + "\n    port = "
            + port
            + "\n"
        )
        config.chmod(0o600)
        previous = {
            key: os.environ.get(key) for key in ["FREETDS", "FREETDSCONF"]
        }
        os.environ.update(FREETDS=str(config), FREETDSCONF=str(config))
        connection = None
        try:
            import pymssql

            connection = pymssql.connect(
                server="cdc_demo",
                port=port,
                user=values["DEMO_SQLSERVER_USER"],
                password=values["DEMO_SQLSERVER_PASSWORD"],
                database=database,
                encryption="require",
                autocommit=True,
                login_timeout=15,
                timeout=60,
                charset="UTF-8",
                appname="Owned synthetic CDC source preparation",
            )
            yield connection
        finally:
            if connection is not None:
                connection.close()
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def quote(identifier):
    """Quote only simple fixed or generated SQL identifiers."""
    if not IDENTIFIER.fullmatch(identifier):
        raise ValueError("Invalid demo SQL identifier.")
    return "[" + identifier + "]"


def private_output(path, text):
    """Create a private external output without overwriting prior settings."""
    path = Path(path).expanduser().absolute()
    if path.resolve().is_relative_to(ROOT) or any(
        part.is_symlink() for part in (path, *path.parents)
    ):
        raise ValueError(
            "Keep demo configuration outside Git without symlinks."
        )
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("Use an owner-private output directory.")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(text)


def create_demo(values, output, trust_local_test=False):
    """Create one new DB, primary-key tables, capture role, and CDC
    instances.
    """
    validate_admin(values, trust_local_test)
    output = Path(output).expanduser().absolute()
    if output.exists() or (output.parent / "table-map.json").exists():
        raise ValueError("Use a fresh private directory for demo outputs.")
    database = DEMO_PREFIX + secrets.token_hex(8)
    token = secrets.token_hex(32)
    reader = "lh_cdc_reader_" + secrets.token_hex(8)
    password = secrets.token_hex(32) + "Aa1!"
    private = {
        "CDC_SQLSERVER_HOST": values["DEMO_SQLSERVER_HOST"],
        "CDC_SQLSERVER_PORT": values.get("DEMO_SQLSERVER_PORT", "1433"),
        "CDC_SQLSERVER_DATABASE": database,
        "CDC_SQLSERVER_USER": reader,
        "CDC_SQLSERVER_PASSWORD": password,
        "CDC_DEMO_OWNERSHIP_TOKEN": token,
        "CDC_TEST_TRUST_SERVER_CERTIFICATE": str(trust_local_test).lower(),
    }
    # Save recovery information before creating any server object. Failure
    # Keep a failed new demo DB for administrator review.
    # Automatic DROP is deliberately unavailable.
    private_output(
        output,
        "\n".join(key + "=" + value for key, value in private.items()) + "\n",
    )
    with connect(values, "master", trust_local_test) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT DB_ID(%s)", (database,))
        if cursor.fetchone()[0] is not None:
            raise RuntimeError("Generated demo database unexpectedly exists.")
        cursor.execute("CREATE DATABASE " + quote(database))
        # The new login receives no instance-wide permission or server role.
        cursor.execute(
            "CREATE LOGIN " + quote(reader) + " WITH PASSWORD = %s, "
            "DEFAULT_DATABASE = " + quote(database) + ", CHECK_POLICY = ON",
            (password,),
        )
    data = generator().build_data(seed=42, orders=20, days=7, scenario="clean")
    with connect(values, database, trust_local_test) as connection:
        cursor = connection.cursor()
        cursor.execute(
            "CREATE TABLE dbo.lh_cdc_demo_ownership "
            "(token nvarchar(64) NOT NULL PRIMARY KEY)"
        )
        cursor.execute(
            "INSERT INTO dbo.lh_cdc_demo_ownership VALUES (%s)", (token,)
        )
        cursor.execute("EXEC sys.sp_cdc_enable_db")
        cursor.execute("CREATE ROLE [lh_cdc_reader]")
        cursor.execute(
            "CREATE USER " + quote(reader) + " FOR LOGIN " + quote(reader)
        )
        cursor.execute(
            "ALTER ROLE [lh_cdc_reader] ADD MEMBER " + quote(reader)
        )
        for metadata in [
            "captured_columns",
            "change_tables",
            "lsn_time_mapping",
        ]:
            cursor.execute(
                "GRANT SELECT ON [cdc]."
                + quote(metadata)
                + " TO [lh_cdc_reader]"
            )
        cursor.execute("GRANT VIEW DATABASE STATE TO " + quote(reader))
        cursor.execute(
            "GRANT VIEW DATABASE PERFORMANCE STATE TO " + quote(reader)
        )
        for contract in CONTRACTS:
            table = quote("dbo") + "." + quote(contract["table"])
            columns = contract["columns"]
            definitions = [
                quote(columns[0]) + " nvarchar(128) NOT NULL PRIMARY KEY"
            ]
            definitions += [
                quote(column) + " nvarchar(4000) NOT NULL"
                for column in columns[1:]
            ]
            cursor.execute(
                "CREATE TABLE " + table + " (" + ", ".join(definitions) + ")"
            )
            cursor.execute("GRANT SELECT ON " + table + " TO [lh_cdc_reader]")
            cursor.execute(
                "EXEC sys.sp_cdc_enable_table @source_schema = N'dbo', "
                "@source_name = %s, @role_name = N'lh_cdc_reader', "
                "@supports_net_changes = 0",
                (contract["table"],),
            )
            cursor.execute(
                "GRANT SELECT ON [cdc]."
                + quote("dbo_" + contract["table"] + "_CT")
                + " TO [lh_cdc_reader]"
            )
            names = ", ".join(quote(column) for column in columns)
            placeholders = ", ".join("%s" for _ in columns)
            cursor.executemany(
                "INSERT INTO "
                + table
                + " ("
                + names
                + ") VALUES ("
                + placeholders
                + ")",
                [
                    [row[column] for column in columns]
                    for row in data[contract["table"]]
                ],
            )
        # Fresh database CDC needs no independent capture. The first enable
        # creates shared jobs normally; NULL drop-pending is healthy only when
        # its source still exists and is tracked. Never repair metadata/jobs.
        cursor.execute(
            "IF EXISTS (SELECT 1 FROM cdc.change_tables ct "
            "LEFT JOIN sys.tables src ON src.object_id=ct.source_object_id "
            "LEFT JOIN sys.tables changed ON changed.object_id=ct.object_id "
            "WHERE src.object_id IS NULL OR src.is_tracked_by_cdc<>1 "
            "OR changed.object_id IS NULL "
            "OR (ct.has_drop_pending IS NOT NULL "
            "AND ct.has_drop_pending<>0)) "
            "THROW 51130, 'Review synthetic CDC source health.', 1; "
            "IF (SELECT COUNT(*) FROM msdb.dbo.cdc_jobs "
            "WHERE database_id=DB_ID() AND job_type IN "
            "(N'capture',N'cleanup'))<>2 "
            "THROW 51132, 'Review shared synthetic CDC jobs.', 1;"
        )
    mapping = {
        "tables": [
            {
                "target_table": item["table"],
                "source_schema": "dbo",
                "source_table": item["table"],
                "columns": {column: column for column in item["columns"]},
            }
            for item in CONTRACTS
        ]
    }
    private_output(
        Path(output).parent / "table-map.json",
        json.dumps(mapping, indent=2) + "\n",
    )
    return {"created": True, "tables": 6, "scenario": "clean"}


def scenario_rows():
    """Define six linked source-only records for repeatable CDC scenarios."""
    stamp = "2026-02-01T08:00:00+00:00"
    return {
        "erp_products": [
            "CDC-P",
            "CDC demo part",
            "DEMO",
            "piece",
            "30",
            stamp,
        ],
        "erp_work_orders": [
            "CDC-WO",
            "CDC-F",
            "CDC-P",
            "2026-02-01",
            "20",
            stamp,
        ],
        "erp_sales_orders": [
            "CDC-SO",
            "CDC-F",
            "CDC-P",
            "2026-02-01",
            "2026-02-02",
            "10",
            "5.00",
            stamp,
        ],
        "erp_shipments": [
            "CDC-SH",
            "CDC-SO",
            "2026-02-03T08:00:00+00:00",
            "3",
            stamp,
        ],
        "mes_events": ["CDC-E", "CDC-WO", stamp, "10", "1", stamp],
        "mes_downtime": [
            "CDC-D",
            "CDC-WO",
            stamp,
            "2026-02-01T08:05:00+00:00",
            "DEMO",
            stamp,
        ],
    }


def apply_scenario(values, action, trust_local_test=False):
    """Apply only fixed INSERT, UPDATE, or DELETE to an owned dedicated DB."""
    if action not in {"insert", "update", "delete"}:
        raise ValueError("Choose insert, update, or delete.")
    database = values.get("CDC_SQLSERVER_DATABASE", "")
    if (
        not re.fullmatch(DEMO_PREFIX + r"[0-9a-f]{16}", database)
        or database.casefold()
        == values.get("SOURCE_SQLSERVER_DATABASE", "").casefold()
        or not re.fullmatch(
            r"[0-9a-f]{64}", values.get("CDC_DEMO_OWNERSHIP_TOKEN", "")
        )
    ):
        raise ValueError(
            "Only the separately provisioned owned demo DB is writable."
        )
    rows = scenario_rows()
    with connect(values, database, trust_local_test) as connection:
        connection.autocommit(False)
        cursor = connection.cursor()
        cursor.execute("SELECT token FROM dbo.lh_cdc_demo_ownership")
        found = cursor.fetchall()
        if found != [(values["CDC_DEMO_OWNERSHIP_TOKEN"],)]:
            raise ValueError("Demo ownership marker did not match.")
        ordered = (
            CONTRACTS if action != "delete" else list(reversed(CONTRACTS))
        )
        try:
            for contract in ordered:
                target = contract["table"]
                columns = contract["columns"]
                row = rows[target]
                table = "[dbo]." + quote(target)
                if action == "insert":
                    cursor.execute(
                        "INSERT INTO "
                        + table
                        + " ("
                        + ", ".join(quote(column) for column in columns)
                        + ") VALUES ("
                        + ", ".join("%s" for _ in columns)
                        + ")",
                        tuple(row),
                    )
                elif action == "delete":
                    cursor.execute(
                        "DELETE FROM "
                        + table
                        + " WHERE "
                        + quote(columns[0])
                        + " = %s",
                        (row[0],),
                    )
                else:
                    changed = list(row)
                    changed[-1] = "2026-02-04T08:00:00+00:00"
                    if target == "mes_events":
                        changed[3] = "14"
                    elif target == "erp_shipments":
                        changed[3] = "5"
                    elif target == "erp_work_orders":
                        changed[4] = "25"
                    elif target == "erp_sales_orders":
                        changed[5] = "12"
                    elif target == "erp_products":
                        changed[1] = "CDC demo part revised"
                    cursor.execute(
                        "UPDATE "
                        + table
                        + " SET "
                        + ", ".join(
                            quote(column) + " = %s" for column in columns[1:]
                        )
                        + " WHERE "
                        + quote(columns[0])
                        + " = %s",
                        tuple(changed[1:] + [changed[0]]),
                    )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "Scenario precondition failed; no DML was committed."
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return {"action": action, "changed_rows": 6, "source_only": True}


def main():
    """Require explicit permission flags for new DB preparation or fixed
    DML.
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "専用の合成CDC DBを準備するか、"
            "所有権を確認して固定6行を変更します。"
            "会社の業務DBへは書き込まず、Kafkaや下流jobは起動しません。"
        ),
    )
    parser.add_argument(
        "action",
        choices=["create", "insert", "update", "delete"],
        help="create=専用DBとCDCを準備。insert/update/delete=固定6行を変更",
    )
    parser.add_argument(
        "--env-file",
        required=True,
        help="DEMO接続情報を持つGit外のprivate env。DMLには所有権情報も必要",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "create結果のGit外新規envファイル。0600で保存し、"
            "同じ親dirへtable-map.jsonも生成する"
        ),
    )
    parser.add_argument(
        "--allow-create-demo-database",
        action="store_true",
        help="新しい専用合成DBの作成・初期投入・CDC準備に明示同意する",
    )
    parser.add_argument(
        "--allow-demo-dml",
        action="store_true",
        help="準備済み専用DBの所有権を照合した固定INSERT/UPDATE/DELETEに同意する",
    )
    parser.add_argument(
        "--test-trust-server-certificate",
        action="store_true",
        help="loopback試験専用の証明書検証例外。暗号化は維持。会社接続には使わない",
    )
    args = parser.parse_args()
    values = read_env(args.env_file)
    if args.action == "create":
        if not args.allow_create_demo_database or args.output is None:
            parser.error(
                "Creation requires --allow-create-demo-database and --output."
            )
        result = create_demo(
            values, args.output, args.test_trust_server_certificate
        )
    else:
        if not args.allow_demo_dml:
            parser.error("Fixed DML requires --allow-demo-dml.")
        result = apply_scenario(
            values, args.action, args.test_trust_server_certificate
        )
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"failed": True, "error_type": type(error).__name__}))
        sys.exit(1)
