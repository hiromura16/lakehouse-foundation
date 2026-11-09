"""Publish isolated synthetic sources without starting downstream services."""

import fcntl
import importlib.util
import io
import json
import os
import re
import struct
import tempfile
import zlib
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "demo_source_generator", ROOT / "scripts/generate-demo.py"
)
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)
ID_COLUMNS = {
    "product_id",
    "work_order_id",
    "sales_order_id",
    "shipment_id",
    "event_id",
    "downtime_id",
}
SQL_KEYS = {
    "DEMO_SQLSERVER_" + suffix
    for suffix in [
        "HOST",
        "PORT",
        "DATABASE",
        "USER",
        "PASSWORD",
        "CA_FILE",
        "HOSTNAME_IN_CERTIFICATE",
    ]
} | {"SOURCE_SQLSERVER_DATABASE"}


def validate_options(run_id, batch, seed, orders, days, scenario, destination):
    """Bound source size and require an unambiguous incremental filename."""
    if not isinstance(run_id, str) or not re.fullmatch(
        r"[0-9a-f]{12}", run_id
    ):
        raise ValueError(
            "run_id must contain 12 lowercase hexadecimal digits."
        )
    for name, value, lower, upper in [
        ("batch", batch, 1, 9999),
        ("seed", seed, 0, 2147483647),
        ("orders", orders, 1, 1000),
        ("days", days, 1, 366),
    ]:
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{name} must be an integer in {lower}..{upper}.")
    if scenario not in {"clean", "edge"}:
        raise ValueError("scenario must be clean or edge.")
    if destination not in {"landing", "sqlserver", "both"}:
        raise ValueError("Unknown source destination.")


def namespaced_data(run_id, batch, seed, orders, days, scenario):
    """Keep references and revisions while isolating every generated batch."""
    data = GENERATOR.build_data(seed, orders, days, scenario)
    namespace = f"G{run_id}B{batch:04d}-"
    # Preserve historical business dates and relative revision ordering.
    # A distinct key namespace prevents cross-run or cross-batch conflicts.
    offset = timedelta(seconds=batch - 1)
    for rows in data.values():
        for row in rows:
            for key in ID_COLUMNS & row.keys():
                row[key] = namespace + row[key]
            stamp = datetime.fromisoformat(row["updated_at"])
            row["updated_at"] = (
                (stamp + offset).isoformat().replace("+00:00", "Z")
            )
    return data


def synthetic_png(seed, size=96):
    """Render a deterministic artificial inspection pattern as RGB PNG."""

    def chunk(kind, payload):
        """Add the length and CRC required by the PNG chunk format."""
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return (
            struct.pack("!I", len(payload))
            + kind
            + payload
            + struct.pack("!I", checksum)
        )

    pixels = io.BytesIO()
    for y in range(size):
        pixels.write(b"\x00")
        for x in range(size):
            tile = (x // 12 + y // 12 + seed) % 2
            pixels.write(bytes((40 + 140 * tile, 90 + seed % 80, 180)))
    header = struct.pack("!2I5B", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(pixels.getvalue()))
        + chunk(b"IEND", b"")
    )


def sql_settings(settings):
    """Reuse the CLI's private settings validator without exposing values."""
    values = {
        key: str(value)
        for key, value in (
            settings if settings is not None else os.environ
        ).items()
        if key in SQL_KEYS
    }
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("SQL settings must be single-line values.")
    with tempfile.TemporaryDirectory(prefix="demo-source-settings-") as name:
        path = Path(name) / "site.env"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(
                "\n".join(key + "=" + value for key, value in values.items())
                + "\n"
            )
        return GENERATOR.private_demo_settings(path)


def publish_source(path, payload):
    """Publish immutable bytes first, then the atomic SHA-256 ready marker."""
    GENERATOR.publish_file(path, payload)
    digest = sha256(payload).hexdigest()
    GENERATOR.publish_file(
        path.with_name(path.name + ".ready"), (digest + "\n").encode()
    )
    return digest


def generate_batch(
    output,
    *,
    run_id,
    batch=1,
    seed=42,
    orders=20,
    days=7,
    scenario="edge",
    destination="landing",
    images=False,
    allow_sql_write=False,
    settings=None,
):
    """Create one source-only batch; return metadata containing no secrets."""
    validate_options(run_id, batch, seed, orders, days, scenario, destination)
    if type(images) is not bool or type(allow_sql_write) is not bool:
        raise ValueError("images and allow_sql_write must be booleans.")
    sql = destination in {"sqlserver", "both"}
    if sql and not allow_sql_write:
        raise ValueError("Explicit SQL write consent is required.")
    if destination == "sqlserver" and images:
        raise ValueError("Synthetic images require a landing destination.")
    # Validate credentials before creating output files or opening SQL.
    values = sql_settings(settings) if sql else None
    output = GENERATOR.output_directory(Path(output))
    for name in ["csv", "json", "images", "manifests"]:
        child = output / name
        if child.is_symlink() or (child.exists() and not child.is_dir()):
            raise ValueError("Source output paths must be real directories.")
    output.mkdir(parents=True, exist_ok=True)
    lock_path = output / ".demo-gui.lock"
    descriptor = os.open(
        lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another source batch is running.") from None
        return _publish_batch(
            output,
            run_id,
            batch,
            seed,
            orders,
            days,
            scenario,
            destination,
            images,
            values,
        )
    finally:
        os.close(descriptor)


def _publish_batch(
    output,
    run_id,
    batch,
    seed,
    orders,
    days,
    scenario,
    destination,
    images,
    values,
):
    """Publish after acquiring the cross-session source writer lock."""
    suffix = f"__{run_id}_{batch:04d}"
    manifest_path = output / "manifests" / ("demo" + suffix + ".json")
    if manifest_path.exists() or manifest_path.is_symlink():
        raise ValueError("This batch has already been published.")
    data = namespaced_data(run_id, batch, seed, orders, days, scenario)
    payloads = GENERATOR.encoded_sources(data)
    sources = []
    targets = []
    for source in GENERATOR.contracts():
        original = Path(source["path"])
        relative = original.with_name(original.stem + suffix + original.suffix)
        payload = payloads[source["path"]]
        sources.append(
            {
                "table": source["table"],
                "path": relative.as_posix(),
                "rows": len(data[source["table"]]),
                "sha256": sha256(payload).hexdigest(),
            }
        )
        targets.append((output / relative, payload))
    image_sources = []
    if images:
        relative = Path("images") / ("inspection" + suffix + ".png")
        payload = synthetic_png(seed + batch)
        image_sources.append(
            {
                "path": relative.as_posix(),
                "sha256": sha256(payload).hexdigest(),
            }
        )
        targets.append((output / relative, payload))
    # Reject reused filenames before any SQL writes. No overwrite is offered.
    if destination != "sqlserver" and any(
        path.exists() or path.with_name(path.name + ".ready").exists()
        for path, _ in targets
    ):
        raise ValueError("Batch filenames already exist; choose a new run.")
    schema = GENERATOR.write_sql(data, values) if values is not None else None
    if destination != "sqlserver":
        for path, payload in targets:
            publish_source(path, payload)
    result = {
        "format_version": 1,
        "run_id": run_id,
        "batch": batch,
        "seed": seed,
        "orders": orders,
        "days": days,
        "scenario": scenario,
        "destination": destination,
        "sources": sources,
        "images": image_sources,
        "sql_schema": schema,
        "manifest": manifest_path.relative_to(output).as_posix(),
    }
    GENERATOR.publish_file(
        manifest_path, (json.dumps(result, indent=2) + "\n").encode()
    )
    return result
