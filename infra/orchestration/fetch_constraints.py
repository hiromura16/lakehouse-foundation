"""Fetch and verify the immutable contents of Airflow release constraints."""

import hashlib
import urllib.request
from pathlib import Path


def main():
    """Write only the known release constraints, failing on content drift."""
    url = (
        "https://raw.githubusercontent.com/apache/airflow/"
        "constraints-3.4.0/constraints-3.12.txt"
    )
    expected = (
        "3cecb4fe4a11d69bd772aef18434825bbed9b344703302ca0a55d50e30a33eda"
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        content = response.read()
    if hashlib.sha256(content).hexdigest() != expected:
        raise SystemExit("Airflow release constraints checksum mismatch.")
    Path("/tmp/airflow-constraints.txt").write_bytes(content)


if __name__ == "__main__":
    main()
