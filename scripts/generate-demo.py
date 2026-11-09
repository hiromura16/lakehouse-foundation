#!/usr/bin/env python3
"""Generate synthetic MES/ERP source records.

Optionally insert raw strings into a new, isolated SQL schema. This tool
never connects to or starts a lakehouse, catalog, or deployment service.
"""

import argparse
import csv
import importlib.util
import io
import ipaddress
import json
import os
import random
import re
import secrets
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "medallion/sources.json"
SCENARIO_SPEC = importlib.util.spec_from_file_location(
    "synthetic_source_scenarios", ROOT / "scripts/source_scenarios.py"
)
SCENARIOS = importlib.util.module_from_spec(SCENARIO_SPEC)
SCENARIO_SPEC.loader.exec_module(SCENARIOS)


def contracts():
    """Load the checked-in six-source column and filename contract."""
    return json.loads(CONTRACT.read_text())["sources"]


def timestamp(day, hour=8, minute=0):
    """Format a deterministic UTC timestamp without consulting the clock."""
    return (
        datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        .replace(hour=hour, minute=minute)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_data(seed=42, orders=20, days=7, scenario="edge"):
    """Return six reproducible raw-string source datasets without I/O."""
    if not 1 <= orders <= 100000 or not 1 <= days <= 366:
        raise ValueError("orders must be 1..100000 and days 1..366.")
    if scenario not in {"clean", "edge"}:
        raise ValueError("Unknown scenario.")
    rng = random.Random(seed)
    start = date(2026, 1, 5)
    data = {source["table"]: [] for source in contracts()}
    for i in range(1, 5):
        data["erp_products"].append(
            dict(
                product_id=f"P{i:03}",
                product_name=f"Demo component {i}",
                product_family="Assembly" if i % 2 else "Machining",
                unit="piece",
                standard_cycle_seconds=str(20 + 5 * i),
                updated_at=timestamp(start - timedelta(days=1)),
            )
        )
    for i in range(orders):
        day = start + timedelta(days=i % days)
        product = f"P{1 + i % 4:03}"
        factory = f"F{1 + i % 2:02}"
        wo = f"WO{i + 1:05}"
        so = f"SO{i + 1:05}"
        planned = rng.randint(80, 160)
        reject = rng.randint(0, 7)
        good = max(0, planned - rng.randint(0, 15) - reject)
        ordered = rng.randint(40, 100)
        data["erp_work_orders"].append(
            dict(
                work_order_id=wo,
                factory_id=factory,
                product_id=product,
                planned_date=day.isoformat(),
                planned_qty=str(planned),
                updated_at=timestamp(day, 6),
            )
        )
        data["erp_sales_orders"].append(
            dict(
                sales_order_id=so,
                factory_id=factory,
                product_id=product,
                order_date=(day - timedelta(days=2)).isoformat(),
                promised_date=(day + timedelta(days=1)).isoformat(),
                ordered_qty=str(ordered),
                unit_price=f"{rng.randint(1000, 9999) / 100:.2f}",
                updated_at=timestamp(day, 7),
            )
        )
        # Two production records and two partial shipments exercise aggregation
        # without join multiplication.
        first = good // 2
        for part, quantity in enumerate([first, good - first], 1):
            data["mes_events"].append(
                dict(
                    event_id=f"EV{i + 1:05}-{part}",
                    work_order_id=wo,
                    event_time=timestamp(day, 9 + part),
                    good_qty=str(quantity),
                    reject_qty=str(reject if part == 2 else 0),
                    updated_at=timestamp(day, 10 + part),
                )
            )
        shipped = (
            ordered if i % 3 == 0 else max(1, ordered - rng.randint(1, 10))
        )
        first_shipment = shipped // 2
        for part, quantity in enumerate(
            [first_shipment, shipped - first_shipment], 1
        ):
            shipped_day = day + timedelta(
                days=2 if i % 5 == 0 and part == 2 else 1
            )
            data["erp_shipments"].append(
                dict(
                    shipment_id=f"SH{i + 1:05}-{part}",
                    sales_order_id=so,
                    shipped_at=timestamp(shipped_day, 13 + part),
                    shipped_qty=str(quantity),
                    updated_at=timestamp(shipped_day, 14 + part),
                )
            )
        data["mes_downtime"].append(
            dict(
                downtime_id=f"DT{i + 1:05}",
                work_order_id=wo,
                start_time=timestamp(day, 12),
                end_time=timestamp(day, 12, rng.randint(2, 20)),
                reason_code=["CHANGEOVER", "MATERIAL_WAIT", "MINOR_STOP"][
                    i % 3
                ],
                updated_at=timestamp(day, 13),
            )
        )
    if scenario == "edge":
        # Exact duplicate, then a later revision. The production quantity must
        # count once at its latest value.
        data["mes_events"].append(dict(data["mes_events"][0]))
        revision = dict(data["mes_events"][0])
        revision["good_qty"] = str(int(revision["good_qty"]) + 3)
        revision["updated_at"] = timestamp(
            start + timedelta(days=days + 2), 10
        )
        data["mes_events"].append(revision)
        # Old event_time with recent updated_at represents a late-arriving
        # valid observation.
        data["mes_events"].append(
            dict(
                event_id="EV-LATE",
                work_order_id="WO00001",
                event_time=timestamp(start, 7),
                good_qty="4",
                reject_qty="1",
                updated_at=timestamp(start + timedelta(days=days + 3)),
            )
        )
        bad_event = dict(
            data["mes_events"][0],
            event_id="EV-BAD-TYPE",
            good_qty="not-a-number",
        )
        data["mes_events"].append(bad_event)
        data["mes_events"].append(
            dict(data["mes_events"][0], event_id="EV-NEGATIVE", good_qty="-7")
        )
        data["mes_events"].append(
            dict(
                data["mes_events"][0],
                event_id="EV-UNKNOWN-WO",
                work_order_id="WO-UNKNOWN",
            )
        )
        data["erp_work_orders"].append(
            dict(
                data["erp_work_orders"][0],
                work_order_id="WO-UNKNOWN-PRODUCT",
                product_id="P-UNKNOWN",
            )
        )
        data["erp_shipments"].append(
            dict(
                data["erp_shipments"][0],
                shipment_id="SH-NEGATIVE",
                shipped_qty="-5",
            )
        )
        data["erp_shipments"].append(
            dict(
                data["erp_shipments"][0],
                shipment_id="SH-UNKNOWN-SO",
                sales_order_id="SO-UNKNOWN",
            )
        )
        data["erp_sales_orders"].append(
            dict(
                data["erp_sales_orders"][0],
                sales_order_id="SO-BAD-PRICE",
                unit_price="invalid",
            )
        )
        data["mes_downtime"].append(
            dict(
                data["mes_downtime"][0],
                downtime_id="DT-REVERSED",
                end_time=timestamp(start, 11),
            )
        )
    # Keep a fixed on-disk order and enforce the raw-string contract
    # independently of serialization.
    for source in contracts():
        columns = source["columns"]
        for row in data[source["table"]]:
            if set(row) != set(columns) or not all(
                isinstance(row[column], str) for column in columns
            ):
                raise ValueError("Generated source contract mismatch.")
    return data


def encoded_sources(data):
    """Serialize CSV and JSON Lines without converting raw string values."""
    result = {}
    for source in contracts():
        rows = data[source["table"]]
        if source["path"].endswith(".csv"):
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(
                stream, fieldnames=source["columns"], lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(rows)
            payload = stream.getvalue().encode("utf-8")
        else:
            payload = "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                + "\n"
                for row in rows
            ).encode("utf-8")
        result[source["path"]] = payload
    return result


def private_demo_settings(path):
    """Read private demo credentials and reject company/system databases."""
    path = Path(path).expanduser()
    if (
        path.is_symlink()
        or not path.is_file()
        or path.resolve().is_relative_to(ROOT)
    ):
        raise ValueError(
            "Use a regular private env file outside the repository."
        )
    if path.stat().st_mode & 0o077:
        raise ValueError(
            "The SQL demo env file must have mode 0600 or stricter."
        )
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
                f"Invalid private env assignment at line {number}."
            )
        if any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError(
                "Private SQL demo settings must be single-line ASCII."
            )
        values[key] = value
    for key in ["HOST", "PORT", "DATABASE", "USER", "PASSWORD"]:
        if not values.get("DEMO_SQLSERVER_" + key):
            raise ValueError("Missing DEMO_SQLSERVER_" + key + ".")
    database = values["DEMO_SQLSERVER_DATABASE"]
    if database != database.strip() or database.casefold() in {
        "master",
        "model",
        "msdb",
        "tempdb",
    }:
        raise ValueError(
            "Choose a dedicated existing demo database, not a system database."
        )
    source_database = values.get("SOURCE_SQLSERVER_DATABASE", "").strip()
    if source_database and database.casefold() == source_database.casefold():
        raise ValueError(
            "The company source database cannot be a demo destination."
        )
    if (
        not values["DEMO_SQLSERVER_PORT"].isdigit()
        or not 1 <= int(values["DEMO_SQLSERVER_PORT"]) <= 65535
    ):
        raise ValueError("Invalid demo SQL port.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", values["DEMO_SQLSERVER_HOST"]):
        raise ValueError(
            "Use a DNS name or IPv4 address for the demo SQL host."
        )
    certificate_hostname(values)
    return values


def certificate_hostname(values):
    """Validate an optional expected certificate name, never a TLS bypass."""
    name = values.get("DEMO_SQLSERVER_HOSTNAME_IN_CERTIFICATE", "")
    if not isinstance(name, str) or (
        name and not re.fullmatch(r"[A-Za-z0-9_.-]{1,253}", name)
    ):
        raise ValueError("Invalid expected demo SQL certificate hostname.")
    return name


def write_sql(data, values, allow_insecure_test=False):
    """Insert raw records into a new schema in a dedicated demo database."""
    host = values["DEMO_SQLSERVER_HOST"]
    if allow_insecure_test:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.casefold() == "localhost"
        if not loopback:
            raise ValueError(
                "The test-only certificate exception is limited to loopback."
            )
    ca_file = values.get("DEMO_SQLSERVER_CA_FILE", "system") or "system"
    if ca_file != "system" and (
        not Path(ca_file).is_file() or any(c in ca_file for c in "\r\n[]")
    ):
        raise ValueError(
            "DEMO_SQLSERVER_CA_FILE must identify an existing PEM CA file."
        )
    expected_hostname = certificate_hostname(values)
    schema = "lh_demo_" + secrets.token_hex(8)
    # This process-local config enforces encryption and, by default,
    # certificate/hostname validation.
    with tempfile.TemporaryDirectory(prefix="source-demo-tls-") as directory:
        config = Path(directory) / "freetds.conf"
        trust = ""
        if not allow_insecure_test:
            trust = (
                "    ca file = " + ca_file + "\n"
                "    check certificate hostname = yes\n"
            )
            if expected_hostname:
                # Network aliases may differ from the trusted certificate name.
                trust += (
                    "    certificate hostname = " + expected_hostname + "\n"
                )
        config.write_text(
            "[global]\n    encryption = require\n    tds version = 7.4\n"
            + trust
            + "[lh_demo_target]\n    host = "
            + host
            + "\n    port = "
            + values["DEMO_SQLSERVER_PORT"]
            + "\n"
        )
        config.chmod(0o600)
        old_config = os.environ.get("FREETDSCONF")
        old_legacy_config = os.environ.get("FREETDS")
        os.environ["FREETDSCONF"] = str(config)
        os.environ["FREETDS"] = str(config)
        try:
            try:
                import pymssql
            except ImportError:
                raise RuntimeError(
                    "SQL demo needs requirements-demo.txt "
                    "in a virtual environment."
                ) from None
            connection = pymssql.connect(
                server="lh_demo_target",
                port=values["DEMO_SQLSERVER_PORT"],
                user=values["DEMO_SQLSERVER_USER"],
                password=values["DEMO_SQLSERVER_PASSWORD"],
                database=values["DEMO_SQLSERVER_DATABASE"],
                encryption="require",
                login_timeout=15,
                timeout=60,
                autocommit=False,
                charset="UTF-8",
                appname="MES-ERP synthetic source generator",
            )
            try:
                cursor = connection.cursor()
                cursor.execute("SELECT DB_NAME()")
                actual_database = cursor.fetchone()[0]
                if (
                    actual_database.casefold()
                    != values["DEMO_SQLSERVER_DATABASE"].casefold()
                ):
                    raise RuntimeError(
                        "Connection resolved to an unexpected database."
                    )
                cursor.execute("SELECT SCHEMA_ID(%s)", (schema,))
                if cursor.fetchone()[0] is not None:
                    raise RuntimeError(
                        "Generated schema already exists; no changes made."
                    )
                cursor.execute("SET XACT_ABORT ON")
                cursor.execute("CREATE SCHEMA [" + schema + "]")
                for source in contracts():
                    table = "[" + schema + "].[" + source["table"] + "]"
                    columns = source["columns"]
                    cursor.execute(
                        "CREATE TABLE "
                        + table
                        + " ("
                        + ",".join(
                            "[" + col + "] nvarchar(max) NOT NULL"
                            for col in columns
                        )
                        + ")"
                    )
                    placeholders = ",".join(["%s"] * len(columns))
                    names = ",".join("[" + col + "]" for col in columns)
                    cursor.executemany(
                        "INSERT INTO "
                        + table
                        + " ("
                        + names
                        + ") VALUES ("
                        + placeholders
                        + ")",
                        [
                            tuple(row[col] for col in columns)
                            for row in data[source["table"]]
                        ],
                    )
                    cursor.execute("SELECT count(*) FROM " + table)
                    if cursor.fetchone()[0] != len(data[source["table"]]):
                        raise RuntimeError(
                            "SQL demo inserted row count mismatch."
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        finally:
            for name, old in [
                ("FREETDSCONF", old_config),
                ("FREETDS", old_legacy_config),
            ]:
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old
    return schema


def output_directory(path):
    """Validate an external output location without touching its contents."""
    path = Path(path).expanduser().absolute()
    if path.resolve().is_relative_to(ROOT):
        raise ValueError(
            "The output directory must be outside the source repository."
        )
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Output paths must not use symlinks.")
    if path.exists() and not path.is_dir():
        raise ValueError("Output must be a directory.")
    for part in ["csv", "json"]:
        child = path / part
        if child.is_symlink() or (child.exists() and not child.is_dir()):
            raise ValueError(
                "Source subdirectories must be ordinary directories."
            )
    return path


def publish_file(path, payload, replace=False):
    """Publish a complete file atomically and refuse accidental replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + "." + secrets.token_hex(6) + ".part")
    try:
        with staging.open("xb") as stream:
            stream.write(payload)
        if replace:
            os.replace(staging, path)
        else:
            os.link(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def main(argv=None):
    """Validate the requested source destination and publish synthetic rows."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--orders", type=int, default=20)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--scenario", choices=["clean", "edge"], default="edge"
    )
    parser.add_argument(
        "--destination",
        choices=["landing", "sqlserver", "both"],
        default="landing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Replace landing files and manifest only. "
            "Existing SQL schemas are never reused."
        ),
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--allow-sql-write", action="store_true")
    parser.add_argument("--anomaly", action="append", default=[])
    parser.add_argument("--anomaly-rate", type=int, default=10)
    parser.add_argument("--allow-file-faults", action="store_true")
    parser.add_argument("--images", action="store_true")
    parser.add_argument("--list-scenarios", action="store_true")
    parser.add_argument(
        "--test-trust-server-certificate",
        action="store_true",
        help="Loopback-only test exception. Encryption remains required.",
    )
    args = parser.parse_args(argv)
    if args.list_scenarios:
        print(
            json.dumps(
                {
                    "version": 1,
                    "scenarios": SCENARIOS.catalog(),
                    "presets": SCENARIOS.presets(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.output is None:
        parser.error("--output is required unless --list-scenarios is used.")
    anomalies = SCENARIOS.validate_selection(
        args.anomaly,
        args.destination,
        args.images,
        args.allow_file_faults,
        args.anomaly_rate,
    )
    if args.images and args.destination == "sqlserver":
        raise ValueError("Synthetic images require a landing destination.")
    output = output_directory(args.output)
    sql = args.destination in {"sqlserver", "both"}
    if sql and (not args.allow_sql_write or args.env_file is None):
        raise ValueError(
            "SQL destination requires a private --env-file "
            "and explicit --allow-sql-write."
        )
    if not sql and args.test_trust_server_certificate:
        raise ValueError(
            "The SQL certificate test flag only applies to SQL destinations."
        )
    values = private_demo_settings(args.env_file) if sql else None
    data = build_data(args.seed, args.orders, args.days, args.scenario)
    changes = []
    if anomalies:
        data, changes = SCENARIOS.apply_rows(
            data, anomalies, args.anomaly_rate, args.seed
        )
    payloads = encoded_sources(data)
    if args.images:
        images_dir = output / "images"
        if images_dir.is_symlink() or (
            images_dir.exists() and not images_dir.is_dir()
        ):
            raise ValueError("Image output must be an ordinary directory.")
        payloads["images/inspection.png"] = SCENARIOS.synthetic_png(args.seed)
    payloads, marker_policies, faults = SCENARIOS.apply_files(
        payloads, anomalies
    )
    targets = [output / "manifest.json"]
    if args.destination in {"landing", "both"}:
        targets += [output / filename for filename in payloads]
        targets += [output / (filename + ".ready") for filename in payloads]
    for target in targets:
        if target.is_symlink() or (
            target.exists() and (not args.force or not target.is_file())
        ):
            raise ValueError(
                "Output exists or is unsafe. "
                "Use another directory or explicit --force."
            )
    output.mkdir(parents=True, exist_ok=True)
    schema = (
        write_sql(data, values, args.test_trust_server_certificate)
        if sql
        else None
    )
    if args.destination in {"landing", "both"}:
        for filename, payload in payloads.items():
            publish_file(output / filename, payload, args.force)
            marker = output / (filename + ".ready")
            ready = SCENARIOS.ready_payload(
                payload, marker_policies.get(filename)
            )
            if ready is not None:
                publish_file(marker, ready, args.force)
            elif args.force:
                marker.unlink(missing_ok=True)
    manifest = {
        "format_version": 1,
        "seed": args.seed,
        "scenario": args.scenario,
        "orders": args.orders,
        "days": args.days,
        "destination": args.destination,
        "sql_schema": schema,
        "sources": [
            {
                "table": source["table"],
                "path": source["path"],
                "rows": len(data[source["table"]]),
                "sha256": sha256(payloads[source["path"]]).hexdigest(),
            }
            for source in contracts()
        ],
    }
    if args.images:
        manifest["images"] = [
            {
                "path": "images/inspection.png",
                "sha256": sha256(
                    payloads["images/inspection.png"]
                ).hexdigest(),
            }
        ]
    if anomalies:
        manifest["diagnostics"] = {
            "anomalies": list(anomalies),
            "anomaly_rate": args.anomaly_rate,
            "row_changes": changes,
            "file_faults": faults,
            "intentional_file_faults": bool(faults),
        }
    publish_file(
        output / "manifest.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
        args.force,
    )
    print(
        "Synthetic source data written. No downstream processing was started."
    )
    if schema:
        print("New SQL staging schema: " + schema)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # SQL driver errors may contain server/account details. Do not print
        # them.
        print(
            "Source generation failed: " + str(error)
            if isinstance(error, ValueError)
            else (
                "Source generation failed. Check private configuration, "
                "dependencies, permissions and TLS trust."
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
