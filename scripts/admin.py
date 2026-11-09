"""Run bounded landing imports and SQL medallion jobs on one node."""

import argparse
import csv
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import pyarrow as pa
import requests
from botocore.config import Config
from botocore.exceptions import ClientError
from PIL import Image
from pyiceberg.catalog import load_catalog
from pyiceberg.expressions import EqualTo

ROOT = Path("/opt/lakehouse")
HTTP = requests.Session()
S3 = boto3.client(
    "s3",
    endpoint_url="http://storage:8333",
    region_name="us-east-1",
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    config=Config(
        s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=30
    ),
)
LOCK = threading.Lock()
STATE = {
    "ready": False,
    "blocked": False,
    "successful_runs": 0,
    "failed_runs": 0,
    "last_success": 0,
    "imported_files": 0,
}
CONTRACT = json.loads((ROOT / "medallion/sources.json").read_text())["sources"]
MAX_BYTES = 16 * 1024 * 1024


def request(method, url, payload=None, accepted=(200, 201, 204)):
    """Call a catalog endpoint without including sensitive bodies in errors."""
    response = HTTP.request(method, url, json=payload, timeout=30)
    if response.status_code not in accepted:
        raise RuntimeError(
            "Service returned HTTP " + str(response.status_code)
        )
    return response.json() if response.content else None


def catalog():
    """Open the shared Iceberg REST catalog using private S3 credentials."""
    return load_catalog(
        "lakehouse",
        type="rest",
        uri="http://catalog:8181/catalog",
        warehouse=os.environ["WAREHOUSE_NAME"],
        **{
            # This single-team foundation uses the supplied storage keys.
            # Avoid silently switching to catalog-side remote signing.
            "header.X-Iceberg-Access-Delegation": "client-managed",
            "py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO",
            "s3.endpoint": "http://storage:8333",
            "s3.region": "us-east-1",
            "s3.access-key-id": os.environ["S3_ACCESS_KEY"],
            "s3.secret-access-key": os.environ["S3_SECRET_KEY"],
            "s3.force-virtual-addressing": "false",
        },
    )


def bootstrap():
    """Create missing buckets, a warehouse, and empty source tables."""
    for bucket in [
        os.environ["S3_BUCKET_RAW"],
        os.environ["S3_BUCKET_WAREHOUSE"],
    ]:
        try:
            S3.head_bucket(Bucket=bucket)
        except ClientError as error:
            if str(error.response["Error"]["Code"]) not in {
                "404",
                "NoSuchBucket",
                "NotFound",
            }:
                raise
            S3.create_bucket(Bucket=bucket)
    info = request("GET", "http://catalog:8181/management/v1/info")
    if not info.get("bootstrapped"):
        request(
            "POST",
            "http://catalog:8181/management/v1/bootstrap",
            {"accept-terms-of-use": True},
        )
    warehouses = request(
        "GET", "http://catalog:8181/management/v1/warehouse"
    ).get("warehouses", [])
    matching = [
        r
        for r in warehouses
        if r.get("name", r.get("warehouse-name"))
        == os.environ["WAREHOUSE_NAME"]
    ]
    if not matching:
        request(
            "POST",
            "http://catalog:8181/management/v1/warehouse",
            {
                "warehouse-name": os.environ["WAREHOUSE_NAME"],
                "project-id": "00000000-0000-0000-0000-000000000000",
                "storage-profile": {
                    "type": "s3",
                    "bucket": os.environ["S3_BUCKET_WAREHOUSE"],
                    "key-prefix": "iceberg",
                    "endpoint": "http://storage:8333",
                    "region": "us-east-1",
                    "path-style-access": True,
                    "flavor": "s3-compat",
                    "sts-enabled": False,
                },
                "storage-credential": {
                    "type": "s3",
                    "credential-type": "access-key",
                    "access-key-id": os.environ["S3_ACCESS_KEY"],
                    "secret-access-key": os.environ["S3_SECRET_KEY"],
                },
            },
        )
    else:
        profile = matching[0].get("storage-profile", {})
        if (
            profile.get("bucket") != os.environ["S3_BUCKET_WAREHOUSE"]
            or str(profile.get("endpoint", "")).rstrip("/")
            != "http://storage:8333"
        ):
            raise RuntimeError(
                "Existing warehouse uses a different storage profile; "
                "refusing to reuse it."
            )
    ensure_tables()


def schema(prefix, layer):
    """Separate batch, persistent CDC, and temporary verification layers."""
    if prefix not in {"", "cdc_"} and not re.fullmatch(
        r"verify_[0-9a-f]+_", prefix
    ):
        raise ValueError("Invalid data namespace prefix.")
    return prefix + layer


def ensure_tables(prefix=""):
    """Create only declared source tables, preserving their existing data."""
    cat = catalog()
    for layer in ["bronze", "silver", "gold", "quarantine"]:
        namespace = schema(prefix, layer)
        if not cat.namespace_exists(namespace):
            cat.create_namespace(namespace)
    for source in CONTRACT:
        identifier = schema(prefix, "bronze") + "." + source["table"]
        if not cat.table_exists(identifier):
            fields = source["columns"] + [
                "_source_file",
                "_source_hash",
                "_ingested_at",
            ]
            cat.create_table(
                identifier,
                schema=pa.schema([(name, pa.string()) for name in fields]),
                properties={"format-version": "2"},
            )
    for table, columns in [
        ("files", ["path", "sha256", "s3_uri", "mime", "bytes"]),
        (
            "images",
            ["path", "sha256", "s3_uri", "mime", "bytes", "width", "height"],
        ),
    ]:
        identifier = schema(prefix, "bronze") + "." + table
        if not cat.table_exists(identifier):
            cat.create_table(
                identifier,
                schema=pa.schema([(name, pa.string()) for name in columns]),
                properties={"format-version": "2"},
            )


def overwrite(table, rows, column, value):
    """Replace one source file atomically so interrupted jobs can replay."""
    arrow = pa.Table.from_pylist(rows, schema=table.schema().as_arrow())
    table.overwrite(arrow, overwrite_filter=EqualTo(column, value))


def completion_hash(marker):
    """Read one bounded SHA-256 marker, including its required newline."""
    if marker.stat().st_size > 66:
        raise ValueError("Invalid completion marker.")
    # A file can grow after stat, so cap the read independently as well.
    with marker.open("rb") as stream:
        content = stream.read(67)
    if not re.fullmatch(rb"[a-f0-9]{64}\r?\n", content):
        raise ValueError("Invalid completion marker.")
    return content[:64].decode("ascii")


def resolve_source(relative):
    """Match declared files and bounded GUI microbatch names explicitly."""
    for source in CONTRACT:
        canonical = source["path"]
        if relative == canonical:
            return source
        stem, extension = canonical.rsplit(".", 1)
        pattern = (
            re.escape(stem)
            + r"__[a-f0-9]{12}_(?!0000)[0-9]{4}\."
            + re.escape(extension)
        )
        if re.fullmatch(pattern, relative):
            return source
    return None


def ingest(directory=Path("/landing"), prefix=""):
    """Import hash-marked files while preserving raw objects and provenance."""
    ensure_tables(prefix)
    cat = catalog()
    files = cat.load_table(schema(prefix, "bronze") + ".files")
    imported = skipped = 0
    for marker in sorted(directory.rglob("*.ready")):
        path = marker.with_name(marker.name[:-6])
        if marker.is_symlink() or path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Landing file exceeds the bounded import limit.")
        expected = completion_hash(marker)
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        digest = sha256(data).hexdigest()
        if digest != expected or (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(
                "Landing file changed or does not match its completion marker."
            )
        previous = (
            files.scan(row_filter=EqualTo("path", relative))
            .to_arrow()
            .to_pylist()
        )
        # The manifest is updated last, after each source table commits.
        # A completed hash skips work; an interrupted import replays safely.
        if previous and previous[0]["sha256"] == digest:
            skipped += 1
            continue
        source = resolve_source(relative)
        rows = []
        if source:
            if path.suffix == ".csv":
                rows = list(csv.DictReader(StringIO(data.decode("utf-8-sig"))))
            else:
                rows = [
                    json.loads(line)
                    for line in data.decode("utf-8-sig").splitlines()
                    if line.strip()
                ]
            if len(rows) > 100000 or not all(
                isinstance(row, dict) and set(row) == set(source["columns"])
                for row in rows
            ):
                raise ValueError(
                    "Input does not match its declared source "
                    "columns/row limit."
                )
            stamp = datetime.now(timezone.utc).isoformat()
            rows = [
                {
                    **{
                        key: None if value is None else str(value)
                        for key, value in row.items()
                    },
                    "_source_file": relative,
                    "_source_hash": digest,
                    "_ingested_at": stamp,
                }
                for row in rows
            ]
        bucket = os.environ["S3_BUCKET_RAW"]
        key = (
            ("verify/" + prefix if prefix else "raw/")
            + digest
            + "/"
            + sha256(relative.encode()).hexdigest()
        )
        uri = "s3://" + bucket + "/" + key
        mime = "application/octet-stream"
        image = None
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            with Image.open(BytesIO(data)) as decoded:
                decoded.verify()
            with Image.open(BytesIO(data)) as decoded:
                image = (decoded.width, decoded.height)
                mime = Image.MIME.get(decoded.format, mime)
        # Hash-based object keys preserve raw bytes across file revisions.
        S3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=mime)
        if source:
            table = cat.load_table(
                schema(prefix, "bronze") + "." + source["table"]
            )
            overwrite(table, rows, "_source_file", relative)
        meta = {
            "path": relative,
            "sha256": digest,
            "s3_uri": uri,
            "mime": mime,
            "bytes": str(len(data)),
        }
        if image:
            table = cat.load_table(schema(prefix, "bronze") + ".images")
            overwrite(
                table,
                [{**meta, "width": str(image[0]), "height": str(image[1])}],
                "path",
                relative,
            )
        # Publish completion only after all file-specific writes succeed.
        # Each table commit is atomic; these writes are not one transaction.
        overwrite(files, [meta], "path", relative)
        imported += 1
    STATE["imported_files"] += imported
    return {"imported": imported, "skipped": skipped}


class WorkerBlockedError(RuntimeError):
    """Require operator review after a query could not be cancelled."""


def internal_query_uri(uri):
    """Accept result and cancellation URLs only on the internal Trino host."""
    try:
        target = urlsplit(uri)
        return (
            target.scheme == "http"
            and target.hostname == "trino"
            and target.port == 8080
            and target.username is None
            and target.password is None
            and not target.fragment
        )
    except (TypeError, ValueError):
        return False


def cancel_query(next_uri):
    """Confirm cancellation without forwarding service or network errors."""
    if not next_uri or not internal_query_uri(next_uri):
        return False
    try:
        response = HTTP.delete(next_uri, timeout=5, allow_redirects=False)
    except Exception:
        return False
    return response.status_code in {204, 404}


def query(sql):
    """Finish a Trino query or cancel it before another job can start."""
    # The HTTP protocol expects one statement without a CLI terminator.
    sql = sql.strip().removesuffix(";").rstrip()
    pending_uri = None
    terminal = False
    try:
        response = HTTP.post(
            "http://trino:8080/v1/statement",
            data=sql.encode(),
            headers={"X-Trino-User": "lakehouse-jobs"},
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise RuntimeError("Trino request failed.")
        value = response.json()
        rows = value.get("data", [])
        deadline = time.monotonic() + 180
        while True:
            if value.get("error"):
                # Trino has terminated the query; no cancellation is needed.
                terminal = True
                raise RuntimeError("Trino query failed.")
            # HTTP 200 acknowledges a response, not a finished query.
            next_uri = value.get("nextUri")
            if not next_uri:
                terminal = True
                return rows
            if not internal_query_uri(next_uri):
                raise RuntimeError("Unexpected query result URL.")
            pending_uri = next_uri
            if time.monotonic() > deadline:
                raise TimeoutError("Query deadline exceeded.")
            time.sleep(0.1)
            response = HTTP.get(pending_uri, timeout=30, allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError("Trino request failed.")
            value = response.json()
            rows.extend(value.get("data", []))
    except Exception:
        # Even an initial POST failure can leave server-side work running.
        # Keep the process blocked when cancellation cannot be confirmed.
        if not terminal and not cancel_query(pending_uri):
            STATE["blocked"] = True
        # HTTP exceptions may contain SQL, addresses, or private responses.
        raise RuntimeError("Trino query failed.") from None


def medallion(prefix=""):
    """Execute readable, versioned SQL in dependency order."""
    ensure_tables(prefix)
    count = 0
    # Filename order enforces Silver/Quarantine/Gold dependencies. Each
    # CREATE OR REPLACE commits one table; the full run is not atomic.
    for path in sorted((ROOT / "medallion").glob("*.sql")):
        statements = "\n".join(
            line
            for line in path.read_text().splitlines()
            if not line.lstrip().startswith("--")
        )
        for statement in statements.split(";"):
            if not statement.strip():
                continue
            for layer in ["bronze", "silver", "gold", "quarantine"]:
                statement = statement.replace(
                    "iceberg." + layer + ".",
                    "iceberg." + schema(prefix, layer) + ".",
                )
            query(statement)
            count += 1
    return {"sql_statements": count}


def verify():
    """Check factory fixtures in isolated namespaces, then remove them."""
    # Every verification run owns its namespaces and raw-object prefix.
    # Production Bronze/Silver/Gold tables are never cleanup targets.
    prefix = "verify_" + secrets.token_hex(5) + "_"
    cat = catalog()
    result = {"checks": []}
    try:
        with tempfile.TemporaryDirectory(
            prefix="lakehouse-fixtures-"
        ) as directory:
            command = [
                sys.executable,
                str(ROOT / "scripts" / "generate-demo.py"),
                "--output",
                directory,
                "--seed",
                "42",
                "--orders",
                "20",
                "--days",
                "7",
                "--scenario",
                "edge",
            ]
            subprocess.run(command, check=True)
            # Image fixtures exercise original-byte preservation separately
            # from the MES/ERP generator, which only creates source records.
            image_path = Path(directory) / "images" / "inspection.png"
            image_path.parent.mkdir()
            Image.new("RGB", (4, 4), color="gray").save(image_path)
            image_hash = sha256(image_path.read_bytes()).hexdigest()
            image_path.with_name(image_path.name + ".ready").write_text(
                image_hash + "\n"
            )
            first = ingest(Path(directory), prefix)
            medallion(prefix)
            image_table = cat.load_table(schema(prefix, "bronze") + ".images")
            image_record = image_table.scan().to_arrow().to_pylist()[0]
            image_uri = urlsplit(image_record["s3_uri"])
            original = S3.get_object(
                Bucket=image_uri.netloc,
                Key=image_uri.path.lstrip("/"),
            )["Body"].read()
            if sha256(original).hexdigest() != image_hash:
                raise AssertionError("Stored image bytes do not match.")
            result["checks"].append("image original bytes and hash preserved")
            checks = json.loads(
                (ROOT / "medallion/expected-demo.json").read_text()
            )["checks"]
            for check in checks:
                # Checks are versioned SQL files or inline factory fixtures.
                sql = (
                    check.get("sql")
                    or (ROOT / "medallion" / check["sql_file"]).read_text()
                )
                for layer in ["bronze", "silver", "gold", "quarantine"]:
                    sql = sql.replace(
                        "iceberg." + layer + ".",
                        "iceberg." + schema(prefix, layer) + ".",
                    )
                actual = query(sql)
                if actual != check["expected"]:
                    raise AssertionError(
                        "MES/ERP check failed: " + check["name"]
                    )
                result["checks"].append(check["name"])
            second = ingest(Path(directory), prefix)
            if second["imported"]:
                raise AssertionError("Unchanged inputs imported twice.")
            medallion(prefix)
            for check in checks:
                # Checks are versioned SQL files or inline factory fixtures.
                sql = (
                    check.get("sql")
                    or (ROOT / "medallion" / check["sql_file"]).read_text()
                )
                for layer in ["bronze", "silver", "gold", "quarantine"]:
                    sql = sql.replace(
                        "iceberg." + layer + ".",
                        "iceberg." + schema(prefix, layer) + ".",
                    )
                if query(sql) != check["expected"]:
                    raise AssertionError("Medallion replay changed results.")
            result.update(
                first_import=first,
                replay_import=second,
                isolated_namespaces=True,
                ok=True,
            )
    finally:
        # Clean up even after a failed assertion, using only this run's
        # isolated prefix. Never sweep shared operational namespaces.
        # If cancellation failed, leave the isolated data intact because
        # the server-side query may still be using it.
        if not STATE["blocked"]:
            cleanup_verification(cat, prefix)
    return result


def cleanup_verification(cat, prefix):
    """Remove only one completed verification run's tables and raw files."""
    if not re.fullmatch(r"verify_[0-9a-f]+_", prefix):
        raise ValueError("Cleanup requires a temporary verification prefix.")
    for layer in ["gold", "silver", "quarantine", "bronze"]:
        namespace = schema(prefix, layer)
        if cat.namespace_exists(namespace):
            for identifier in cat.list_tables(namespace):
                cat.purge_table(identifier)
            cat.drop_namespace(namespace)
    bucket = os.environ["S3_BUCKET_RAW"]
    for page in S3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix="verify/" + prefix
    ):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            S3.delete_objects(Bucket=bucket, Delete={"Objects": objects})


def run(action):
    """Serialize jobs inside the worker so GUI and CLI cannot race."""
    if not LOCK.acquire(blocking=False):
        raise RuntimeError("Another ingestion/medallion job is running.")
    try:
        # The flag survives lock release and is reset only on worker restart.
        if STATE["blocked"]:
            raise WorkerBlockedError(
                "Confirm Trino query completion, then restart the worker."
            )
        if action == "bootstrap":
            bootstrap()
            result = {"ok": True}
        elif action == "ingest":
            result = ingest()
        elif action == "medallion":
            result = {**ingest(), **medallion()}
        elif action == "verify":
            result = verify()
        else:
            raise ValueError("Unsupported action.")
        STATE["successful_runs"] += 1
        STATE["last_success"] = time.time()
        return result
    except Exception:
        STATE["failed_runs"] += 1
        raise
    finally:
        LOCK.release()


class Handler(BaseHTTPRequestHandler):
    """Expose fixed jobs without accepting arbitrary SQL or shell input."""

    def log_message(self, *_):
        """Suppress HTTP access logs that could expose request details."""
        pass

    def respond(self, status, value, content_type="application/json"):
        """Send a bounded job result or metrics response with its length."""
        body = (
            json.dumps(value).encode()
            if content_type == "application/json"
            else value.encode()
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Expose readiness and aggregate counters without job inputs."""
        if self.path == "/health":
            self.respond(
                200 if STATE["ready"] and not STATE["blocked"] else 503,
                {
                    "ready": STATE["ready"] and not STATE["blocked"],
                    "blocked": STATE["blocked"],
                    "busy": LOCK.locked(),
                },
            )
        elif self.path == "/metrics":
            body = (
                "\n".join(
                    "lakehouse_"
                    + key
                    + " "
                    + str(int(value) if isinstance(value, bool) else value)
                    for key, value in STATE.items()
                )
                + "\n"
            )
            self.respond(200, body, "text/plain; version=0.0.4")
        else:
            self.respond(404, {"error": "not found"})

    def do_POST(self):
        """Authenticate a fixed action and execute it under the job lock."""
        if self.path != "/run":
            return self.respond(404, {"error": "not found"})
        expected = "Bearer " + os.environ["WORKER_TOKEN"]
        if not secrets.compare_digest(
            self.headers.get("Authorization", ""), expected
        ):
            return self.respond(401, {"error": "authentication required"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                return self.respond(400, {"error": "invalid body size"})
            payload = json.loads(self.rfile.read(length))
            action = payload.get("action")
            if action not in {"ingest", "medallion", "verify"}:
                return self.respond(400, {"error": "unknown action"})
            self.respond(200, run(action))
        except WorkerBlockedError:
            self.respond(
                409,
                {
                    "error": "WorkerBlockedError",
                    "message": (
                        "Confirm Trino query completion, "
                        "then restart the worker."
                    ),
                },
            )
        except Exception as error:
            # Only application-owned error codes are safe to return.
            # Driver details can contain private source values or endpoints.
            message = "Job failed; check source contract and service health."
            controlled = ("Trino query failed:", "MES/ERP check failed:")
            if str(error).startswith(controlled):
                message = str(error)
            # Keep diagnostic locations while excluding values and SQL text.
            frames = [
                {"function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(error.__traceback__)
                if Path(frame.filename).parent == ROOT
            ]
            print(
                json.dumps(
                    {
                        "event": "job.failed",
                        "error": type(error).__name__,
                        "locations": frames,
                    }
                ),
                flush=True,
            )
            self.respond(
                500,
                {
                    "error": type(error).__name__,
                    "message": message,
                },
            )


def main():
    """Start the job service or submit a command to its serialized API."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["serve", "bootstrap", "ingest", "medallion", "verify"],
    )
    args = parser.parse_args()
    if args.action == "serve":
        deadline = time.monotonic() + 180
        while True:
            try:
                bootstrap()
                break
            except Exception as error:
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "Bootstrap readiness deadline."
                    ) from None
                print(
                    "Waiting for core services: " + type(error).__name__,
                    flush=True,
                )
                time.sleep(3)
        STATE["ready"] = True
        ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
    elif args.action == "bootstrap":
        print(json.dumps(run(args.action), ensure_ascii=False), flush=True)
    else:
        # The CLI uses the same process-level lock as n8n's HTTP requests.
        response = HTTP.post(
            "http://127.0.0.1:8000/run",
            headers={"Authorization": "Bearer " + os.environ["WORKER_TOKEN"]},
            json={"action": args.action},
            timeout=300,
        )
        if response.status_code != 200:
            raise SystemExit(
                "Worker job failed; inspect service health "
                "and source contracts."
            )
        print(json.dumps(response.json(), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
