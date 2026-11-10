#!/usr/bin/env python3
"""Read mapped SQL Server source tables and publish completed landing files.

Only explicit SELECT statements are issued. This snapshot bridge never creates
or changes source objects, enables CDC, or starts downstream processing.
"""

import argparse
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
SYSTEM_DATABASES = {"master", "model", "msdb", "tempdb"}
MAX_ROWS = 100000


def source_io():
    """Load format and atomic-file helpers without running the demo CLI."""
    specification = importlib.util.spec_from_file_location(
        "landing_source_io", ROOT / "scripts/generate-demo.py"
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def private_file(path):
    """Require an external, regular, owner-private configuration file."""
    path = Path(path).expanduser().absolute()
    if (
        path.resolve().is_relative_to(ROOT)
        or not path.is_file()
        or any(part.is_symlink() for part in (path, *path.parents))
    ):
        raise ValueError("Use regular private configuration outside Git.")
    if path.stat().st_mode & 0o077:
        raise ValueError(
            "Private configuration must have mode 0600 or stricter."
        )
    return path


def read_settings(path):
    """Read source credentials literally, without evaluating shell syntax."""
    values = {}
    for line in private_file(path).read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or key in values
            or any(
                ord(character) < 32 or ord(character) > 126
                for character in value
            )
        ):
            raise ValueError("Invalid private configuration assignment.")
        values[key] = value
    for key in ("HOST", "DATABASE", "USER", "PASSWORD"):
        if not values.get("SOURCE_SQLSERVER_" + key):
            raise ValueError("Complete the private SOURCE_SQLSERVER settings.")
    values.setdefault("SOURCE_SQLSERVER_PORT", "1433")
    port = values["SOURCE_SQLSERVER_PORT"]
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("Invalid source SQL port.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", values["SOURCE_SQLSERVER_HOST"]):
        raise ValueError("Use a DNS name or IPv4 source host.")
    database = values["SOURCE_SQLSERVER_DATABASE"]
    if database != database.strip() or database.casefold() in SYSTEM_DATABASES:
        raise ValueError("Select an application source database.")
    ca_file = values.get("SOURCE_SQLSERVER_CA_FILE", "system") or "system"
    if ca_file != "system" and (
        not Path(ca_file).is_file()
        or any(character in ca_file for character in "\r\n[]")
    ):
        raise ValueError("The source CA setting must be system or a PEM file.")
    values["SOURCE_SQLSERVER_CA_FILE"] = ca_file
    try:
        ZoneInfo(values.get("SOURCE_SQLSERVER_TIMEZONE", "UTC") or "UTC")
    except (ValueError, KeyError):
        raise ValueError("Invalid source timestamp timezone.") from None
    return values


def reject_duplicate_keys(pairs):
    """Reject duplicate JSON keys in a private mapping."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key in the private table mapping.")
        result[key] = value
    return result


def read_mapping(path, contracts):
    """Validate six one-to-one column maps against the checked-in contract."""
    mapping = json.loads(
        private_file(path).read_text(), object_pairs_hook=reject_duplicate_keys
    )
    if not isinstance(mapping, dict) or set(mapping) != {"tables"}:
        raise ValueError("Table mapping must contain a tables array.")
    tables = mapping["tables"]
    if not isinstance(tables, list) or len(tables) != len(contracts):
        raise ValueError("Map all six declared source tables exactly once.")
    declared = {source["table"]: source for source in contracts}
    selected = {}
    for table in tables:
        if not isinstance(table, dict) or set(table) != {
            "target_table",
            "source_schema",
            "source_table",
            "columns",
        }:
            raise ValueError("Invalid private table mapping entry.")
        target = table["target_table"]
        if (
            not isinstance(target, str)
            or target not in declared
            or target in selected
        ):
            raise ValueError("Unknown or duplicate target in table mapping.")
        for key in ("source_schema", "source_table"):
            if not isinstance(table[key], str) or not IDENTIFIER.fullmatch(
                table[key]
            ):
                raise ValueError(
                    "Use plain SQL identifiers in the private map."
                )
        columns = table["columns"]
        if (
            not isinstance(columns, dict)
            or not all(
                isinstance(key, str)
                and IDENTIFIER.fullmatch(key)
                and isinstance(value, str)
                for key, value in columns.items()
            )
            or len(columns) != len(declared[target]["columns"])
            or set(columns.values()) != set(declared[target]["columns"])
        ):
            raise ValueError(
                "Each source column must map to one declared column."
            )
        selected[target] = table
    return selected


def quoted(identifier):
    """Quote an already validated, non-expression SQL Server identifier."""
    if not IDENTIFIER.fullmatch(identifier):
        raise ValueError("Invalid SQL identifier.")
    return "[" + identifier + "]"


def canonical_value(value, source_zone):
    """Serialize typed source scalars as deterministic Bronze string values."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=source_zone)
        return (
            value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite numeric source value.")
        return format(value, "f")
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite numeric source value.")
    if isinstance(value, (str, int, float)):
        return str(value)
    raise ValueError("Unsupported source scalar type.")


@contextmanager
def tls_configuration(values, trust_local_test=False):
    """Set process-local FreeTDS trust without changing host configuration."""
    host = values["SOURCE_SQLSERVER_HOST"]
    if trust_local_test:
        import ipaddress

        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.casefold() == "localhost"
        if not loopback:
            raise ValueError("The certificate test exception needs loopback.")
    with tempfile.TemporaryDirectory(
        prefix="source-extract-tls-"
    ) as directory:
        config = Path(directory) / "freetds.conf"
        trust = ""
        if not trust_local_test:
            trust = (
                "    ca file = " + values["SOURCE_SQLSERVER_CA_FILE"] + "\n"
                "    check certificate hostname = yes\n"
            )
        config.write_text(
            "[global]\n    encryption = require\n    tds version = 7.4\n"
            + trust
            + "[lh_source_target]\n    host = "
            + host
            + "\n"
            + "    port = "
            + values["SOURCE_SQLSERVER_PORT"]
            + "\n"
        )
        config.chmod(0o600)
        previous = {
            key: os.environ.get(key) for key in ("FREETDS", "FREETDSCONF")
        }
        os.environ.update(FREETDS=str(config), FREETDSCONF=str(config))
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def extract_tables(
    values, mapping, contracts, max_rows, trust_local_test=False
):
    """Fetch and validate all tables before publishing any files."""
    if not 1 <= max_rows <= MAX_ROWS:
        raise ValueError("max-rows must be between 1 and 100000.")
    source_zone = ZoneInfo(
        values.get("SOURCE_SQLSERVER_TIMEZONE", "UTC") or "UTC"
    )
    datasets = {}
    with tls_configuration(values, trust_local_test):
        try:
            import pymssql
        except ImportError:
            raise RuntimeError(
                "Install optional requirements-demo.txt."
            ) from None
        connection = pymssql.connect(
            server="lh_source_target",
            port=values["SOURCE_SQLSERVER_PORT"],
            database=values["SOURCE_SQLSERVER_DATABASE"],
            user=values["SOURCE_SQLSERVER_USER"],
            password=values["SOURCE_SQLSERVER_PASSWORD"],
            encryption="require",
            read_only=True,
            autocommit=True,
            login_timeout=15,
            timeout=60,
            charset="UTF-8",
            appname="MES-ERP read-only landing snapshot",
        )
        try:
            cursor = connection.cursor()
            for source in contracts:
                table = mapping[source["table"]]
                # Map company columns onto the fixed Bronze column order.
                inverse = {
                    target: original
                    for original, target in table["columns"].items()
                }
                selected = [inverse[column] for column in source["columns"]]
                statement = (
                    "SELECT TOP ("
                    + str(max_rows + 1)
                    + ") "
                    + ", ".join(quoted(column) for column in selected)
                    + " FROM "
                    + quoted(table["source_schema"])
                    + "."
                    + quoted(table["source_table"])
                )
                cursor.execute(statement)
                records = []
                while True:
                    batch = cursor.fetchmany(
                        min(1000, max_rows + 1 - len(records))
                    )
                    if not batch:
                        break
                    records.extend(batch)
                    if len(records) > max_rows:
                        raise ValueError("A source table exceeds --max-rows.")
                rows = []
                for record in records:
                    if len(record) != len(source["columns"]):
                        raise ValueError("Unexpected source result shape.")
                    rows.append(
                        {
                            column: canonical_value(value, source_zone)
                            for column, value in zip(source["columns"], record)
                        }
                    )
                # SQL row order is unspecified. Canonical sorting makes
                # unchanged snapshots byte-identical without a SQL sort.
                rows.sort(
                    key=lambda row: tuple(
                        row[column] for column in source["columns"]
                    )
                )
                datasets[source["table"]] = rows
        finally:
            connection.close()
    return datasets


def main(argv=None):
    """Fetch a read-only snapshot and publish six completed source files."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "許可された6表/列をSELECTし、CSV/JSON・ready・manifestを保存します。"
            "DB変更、CDC有効化、取り込み・変換は行いません。"
            "6表のSELECTは同一時点transactionではありません。"
        ),
    )
    parser.add_argument(
        "--env-file",
        required=True,
        type=Path,
        help="SOURCE用read-only資格情報とTLS設定を持つGit外のprivate env",
    )
    parser.add_argument(
        "--table-map",
        required=True,
        type=Path,
        help="実表・列を6ソース契約へ対応付けるGit外のJSON。任意SQLは指定不可",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Git外のlanding絶対パス。全6表の取得・変換成功後に完成素材を公開",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=MAX_ROWS,
        help="1表の上限行数。1〜100000（既定: 100000）。超過は切捨てず拒否",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="出力済み素材・ready・抽出manifestの意図した置換を許可する",
    )
    parser.add_argument(
        "--test-trust-server-certificate",
        action="store_true",
        help=(
            "loopback試験専用の証明書検証例外。"
            "暗号化は維持し、会社接続には使わない"
        ),
    )
    args = parser.parse_args(argv)
    helper = source_io()
    contracts = helper.contracts()
    values = read_settings(args.env_file)
    mapping = read_mapping(args.table_map, contracts)
    output = helper.output_directory(args.output)
    filenames = [source["path"] for source in contracts]
    targets = [output / filename for filename in filenames]
    targets += [output / (filename + ".ready") for filename in filenames]
    targets.append(output / "source-extract.json")
    for target in targets:
        if target.is_symlink() or (
            target.exists() and (not args.force or not target.is_file())
        ):
            raise ValueError(
                "Output exists. Choose another directory or --force."
            )
    datasets = extract_tables(
        values,
        mapping,
        contracts,
        args.max_rows,
        args.test_trust_server_certificate,
    )
    payloads = helper.encoded_sources(datasets)
    # No file is published until all six queries and value conversions succeed.
    for filename, payload in payloads.items():
        helper.publish_file(output / filename, payload, args.force)
        helper.publish_file(
            output / (filename + ".ready"),
            (sha256(payload).hexdigest() + "\n").encode(),
            args.force,
        )
    manifest = {
        "format_version": 1,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "max_rows": args.max_rows,
        "sources": [
            {
                "table": source["table"],
                "path": source["path"],
                "rows": len(datasets[source["table"]]),
                "sha256": sha256(payloads[source["path"]]).hexdigest(),
            }
            for source in contracts
        ],
    }
    helper.publish_file(
        output / "source-extract.json",
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
        args.force,
    )
    print("Read-only source snapshot published. No processing was started.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Driver/configuration errors can contain internal source identifiers.
        print(
            "Source extraction failed. Check private mapping, limits, "
            "permissions and TLS trust.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
