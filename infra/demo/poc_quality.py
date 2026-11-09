"""Read bounded quality counts using one fixed internal Trino statement."""

import re
import time
from urllib.parse import urlsplit

import httpx
from poc_catalog import SOURCE_DETAILS


class QualityUnavailable(RuntimeError):
    """Keep SQL, transport details and result pages outside public errors."""


def statement() -> str:
    """Build a fixed six-row query from tracked contract table identifiers."""
    return " UNION ALL ".join(
        "SELECT '" + table + "' AS dataset, "
        "(SELECT count(*) FROM iceberg.bronze." + table + ") AS bronze_rows, "
        "(SELECT count(*) FROM iceberg.silver." + table + ") AS silver_rows, "
        "(SELECT count(*) FROM iceberg.quarantine."
        + table
        + ") AS quarantine_rows"
        for table in SOURCE_DETAILS
    )


def next_page(value: str) -> str:
    """Accept Trino result/cancel URLs only on the fixed internal origin."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "trino"
        or parsed.port != 8080
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
        or not re.fullmatch(
            r"/v1/statement/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", parsed.path
        )
    ):
        raise QualityUnavailable("Invalid internal result reference.")
    return value


def quality_counts() -> list[dict]:
    """Bound polling, output size and runtime; cancel unfinished reads."""
    deadline = time.monotonic() + 10
    pending = None
    rows = []
    with httpx.Client(
        timeout=5,
        trust_env=False,
        follow_redirects=False,
        headers={"X-Trino-User": "lakehouse-portal-reader"},
    ) as client:
        try:
            response = client.post(
                "http://trino:8080/v1/statement", content=statement()
            )
            while True:
                if (
                    response.status_code != 200
                    or len(response.content) > 65536
                ):
                    raise QualityUnavailable("Quality response unavailable.")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise QualityUnavailable("Invalid quality response.")
                pending = (
                    next_page(payload["nextUri"])
                    if payload.get("nextUri")
                    else None
                )
                if payload.get("error"):
                    raise QualityUnavailable("Quality tables unavailable.")
                rows.extend(payload.get("data", []))
                if len(rows) > 6:
                    raise QualityUnavailable("Quality result limit exceeded.")
                if pending is None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise QualityUnavailable("Quality read deadline reached.")
                response = client.get(pending, timeout=min(5, remaining))
            if len(rows) != 6:
                raise QualityUnavailable("Quality tables incomplete.")
            result = []
            for row in rows:
                if (
                    not isinstance(row, list)
                    or len(row) != 4
                    or row[0] not in SOURCE_DETAILS
                    or any(
                        type(value) is not int or value < 0
                        for value in row[1:]
                    )
                ):
                    raise QualityUnavailable("Invalid quality counts.")
                result.append(
                    dict(
                        zip(
                            [
                                "dataset",
                                "bronze_rows",
                                "silver_rows",
                                "quarantine_rows",
                            ],
                            row,
                        )
                    )
                )
            if len({item["dataset"] for item in result}) != 6:
                raise QualityUnavailable("Duplicate quality counts.")
            return result
        except Exception:
            if pending is not None:
                try:
                    client.delete(pending, timeout=2)
                except httpx.HTTPError:
                    pass
            raise QualityUnavailable("Quality read unavailable.") from None
