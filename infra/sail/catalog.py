"""Bounded internal-only REST observations for the synthetic Sail lab.

There is no user-supplied endpoint or SQL. Redirects and inherited HTTP
proxies are disabled. Error bodies/URLs are never surfaced. Namespace
creation is the only REST mutation and requires the runner's write consent.
"""

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from infra.sail.contract import (
    ENGINE_NAMESPACES,
    MAX_ROWS,
    OWNER,
    LabFailure,
    schema_for,
    target,
    typed,
)

CATALOG = "http://catalog:8181/catalog"
TRINO = "http://trino:8080"
MAX_BODY = 1024 * 1024
DEADLINE_SECONDS = 30


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward credentials or follow a server-provided new origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def internal_opener():
    """Use only fixed Compose endpoints without inherited proxy variables."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect()
    )


def json_request(opener, request, deadline, *, allow_missing=False):
    """Bound body size and total time, checking between unbuffered reads."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LabFailure("CATALOG_UNAVAILABLE")
    try:
        with opener.open(request, timeout=min(3, remaining)) as response:
            if not 200 <= response.status < 300:
                raise LabFailure("CATALOG_UNAVAILABLE")
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise LabFailure("CATALOG_UNAVAILABLE")
                chunk = response.read1(min(8192, MAX_BODY + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BODY:
                    raise LabFailure("CATALOG_RESPONSE_INVALID")
            content = b"".join(chunks)
        payload = json.loads(content) if content else {}
        if not isinstance(payload, dict):
            raise LabFailure("CATALOG_RESPONSE_INVALID")
        return payload
    except urllib.error.HTTPError as error:
        if allow_missing and error.code == 404:
            return None
        raise LabFailure("CATALOG_UNAVAILABLE") from None
    except (
        OSError,
        ValueError,
        TimeoutError,
        urllib.error.URLError,
        http.client.HTTPException,
    ):
        raise LabFailure("CATALOG_UNAVAILABLE") from None


class Catalog:
    """Resolve the server's warehouse prefix and protect lab ownership.

    Lakekeeper's REST config returns a warehouse prefix. It is validated as
    path segments, never interpreted as a URL. Actual catalog authorization
    remains the operator's responsibility; this lab follows the repository's
    existing unauthenticated internal PoC catalog, without changing its auth.
    """

    def __init__(self, warehouse=None, opener=None):
        self.opener = opener or internal_opener()
        warehouse = (
            warehouse
            if warehouse is not None
            else os.environ.get("WAREHOUSE_NAME", "")
        )
        if not isinstance(warehouse, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", warehouse
        ):
            raise LabFailure("CATALOG_RESPONSE_INVALID")
        url = (
            CATALOG
            + "/v1/config?"
            + urllib.parse.urlencode({"warehouse": warehouse})
        )
        config = self.request(url)
        defaults, overrides = (
            config.get("defaults", {}),
            config.get("overrides", {}),
        )
        if not isinstance(defaults, dict) or not isinstance(overrides, dict):
            raise LabFailure("CATALOG_RESPONSE_INVALID")
        prefix = {**defaults, **overrides}.get("prefix", "")
        if (
            not isinstance(prefix, str)
            or len(prefix) > 128
            or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix)
        ):
            raise LabFailure("CATALOG_RESPONSE_INVALID")
        self.base = CATALOG + "/v1/" + prefix

    def request(self, url, *, data=None, missing=False):
        """Call one fixed catalog origin with normalized diagnostics."""
        if not url.startswith(CATALOG + "/v1/"):
            raise LabFailure("CATALOG_RESPONSE_INVALID")
        request = urllib.request.Request(
            url,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
        return json_request(
            self.opener,
            request,
            time.monotonic() + DEADLINE_SECONDS,
            allow_missing=missing,
        )

    def namespace(self, engine, *, approve=False):
        """Create only a missing fixed namespace, or verify its owner marker.

        Existing unmarked namespaces are rejected. No automatic adoption,
        property repair, drop, deletion or namespace rename is offered.
        """
        target(engine, "factory_daily_production")
        name = ENGINE_NAMESPACES[engine]
        url = self.base + "/namespaces/" + name
        state = self.request(url, missing=True)
        if state is None:
            if not approve:
                raise LabFailure("WRITE_APPROVAL_REQUIRED")
            self.request(
                self.base + "/namespaces",
                data={"namespace": [name], "properties": {"lab.owner": OWNER}},
            )
            state = self.request(url)
        if state.get("properties", {}).get("lab.owner") != OWNER:
            raise LabFailure("CATALOG_OWNERSHIP_MISMATCH")
        return state

    def table(self, engine, table, *, missing=False):
        """Load an allowlisted table and reject foreign/unsupported data."""
        target(engine, table)
        url = (
            self.base
            + "/namespaces/"
            + ENGINE_NAMESPACES[engine]
            + "/tables/"
            + table
        )
        response = self.request(url, missing=missing)
        if response is None:
            return None
        metadata = response.get("metadata")
        if not isinstance(metadata, dict):
            raise LabFailure("CATALOG_RESPONSE_INVALID")
        if metadata.get("properties", {}).get("lab.owner") != OWNER:
            raise LabFailure("CATALOG_OWNERSHIP_MISMATCH")
        if metadata.get("format-version") != 2:
            raise LabFailure("TABLE_FORMAT_UNSUPPORTED")
        current = metadata.get("current-schema-id")
        schemas = metadata.get("schemas", [])
        schema = next(
            (item for item in schemas if item.get("schema-id") == current),
            None,
        )
        if not isinstance(schema, dict):
            raise LabFailure("CATALOG_RESPONSE_INVALID")
        expected = list(schema_for(table))
        actual = []
        nullable = []
        for field in schema.get("fields", []):
            kind = field.get("type")
            if (
                not isinstance(kind, str)
                or type(field.get("required")) is not bool
            ):
                raise LabFailure("SCHEMA_MISMATCH")
            kind = kind.replace(" ", "").replace("long", "bigint")
            actual.append((field.get("name"), kind))
            nullable.append(not field["required"])
        if actual != expected:
            raise LabFailure("SCHEMA_MISMATCH")
        snapshots = metadata.get("snapshots", [])
        snapshot_id = metadata.get("current-snapshot-id")
        if (
            type(snapshot_id) is not int
            or snapshot_id < 0
            or not isinstance(snapshots, list)
            or not any(
                isinstance(item, dict)
                and type(item.get("snapshot-id")) is int
                and item["snapshot-id"] == snapshot_id
                for item in snapshots
            )
        ):
            raise LabFailure("CATALOG_RESPONSE_INVALID")
        return {
            "format_version": 2,
            "schema": [list(item) for item in actual],
            "nullable": nullable,
            "snapshot_id": str(snapshot_id),
            "snapshot_count": len(snapshots),
        }


def trino_page_uri(value):
    """Approve continuation/cancellation paths only on the fixed origin."""
    if not isinstance(value, str) or len(value) > 2048:
        raise LabFailure("TRINO_READ_FAILED")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.netloc != "trino:8080"
        or not parsed.path.startswith("/v1/statement/")
        or parsed.username
        or parsed.password
        or parsed.fragment
        or ".." in parsed.path
        or "%" in parsed.path
    ):
        raise LabFailure("TRINO_READ_FAILED")
    return value


def trino_rows(engine, table, *, opener=None):
    """Cross-read every business column with finite pages/rows/time bounds.

    Columns are transported as text to avoid JSON floating-point conversion
    of DECIMAL. Their Iceberg types are checked separately by Catalog.table.
    A partial query is cancelled best effort; cancellation failure stays a
    failed run and is never presented as a successful cleanup.
    """
    destination = target(engine, table, "iceberg")
    schema = schema_for(table)
    columns = ", ".join(
        f"CAST({name} AS varchar) AS {name}" for name, _ in schema
    )
    statement = f"SELECT {columns} FROM {destination} LIMIT {MAX_ROWS + 1}"
    opener = opener or internal_opener()
    deadline = time.monotonic() + DEADLINE_SECONDS
    request = urllib.request.Request(
        TRINO + "/v1/statement",
        data=statement.encode(),
        headers={
            "X-Trino-User": "synthetic-lab",
            "Content-Type": "text/plain",
        },
    )
    rows, next_uri = [], None
    try:
        names = None
        for _ in range(100):
            result = json_request(opener, request, deadline)
            # Save only an approved continuation before inspecting rows/schema.
            # Even a malformed first page can leave a live server-side query.
            next_uri = None
            candidate = result.get("nextUri")
            if candidate:
                next_uri = trino_page_uri(candidate)
            if "error" in result:
                raise LabFailure("TRINO_READ_FAILED")
            if "columns" in result:
                names = [column.get("name") for column in result["columns"]]
                if names != [name for name, _ in schema]:
                    raise LabFailure("SCHEMA_MISMATCH")
            data = result.get("data", [])
            if not isinstance(data, list):
                raise LabFailure("TRINO_READ_FAILED")
            for row in data:
                if (
                    names is None
                    or not isinstance(row, list)
                    or len(row) != len(schema)
                ):
                    raise LabFailure("SCHEMA_MISMATCH")
                rows.append(
                    {
                        name: typed(value, kind)
                        for (name, kind), value in zip(schema, row)
                    }
                )
                if len(rows) > MAX_ROWS:
                    raise LabFailure("TRINO_READ_FAILED")
            if not next_uri:
                if names is None:
                    raise LabFailure("SCHEMA_MISMATCH")
                return rows
            request = urllib.request.Request(next_uri)
        raise LabFailure("TRINO_READ_FAILED")
    except (LabFailure, OSError, ValueError, KeyError, TypeError):
        if next_uri:
            try:
                cancel = urllib.request.Request(
                    trino_page_uri(next_uri), method="DELETE"
                )
                with opener.open(cancel, timeout=2) as response:
                    if not 200 <= response.status < 300:
                        raise LabFailure("TRINO_READ_FAILED")
            except (
                LabFailure,
                OSError,
                ValueError,
                urllib.error.URLError,
                http.client.HTTPException,
            ):
                pass
        raise LabFailure("TRINO_READ_FAILED") from None
