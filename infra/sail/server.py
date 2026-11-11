"""Start only the official Sail binary with this lab's fixed internal peers.

The optional corporate CA is installed at image build through the existing
secret mechanism. No proxy/credential is printed, embedded in argv or stored
in generated files. No JVM Spark catalog options are passed to native Sail.
"""

import json
import os
import re
import socket
import sys


def environment(values):
    """Translate existing private S3/warehouse keys to documented Sail keys."""
    warehouse = values.get("WAREHOUSE_NAME", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", warehouse):
        raise ValueError("SAIL_CONFIGURATION_INVALID")
    if not values.get("S3_ACCESS_KEY") or not values.get("S3_SECRET_KEY"):
        raise ValueError("SAIL_CONFIGURATION_INVALID")
    result = dict(values)
    result.update(
        {
            "SAIL_CATALOG__LIST": (
                '[{type="iceberg-rest", name="lakehouse", '
                'uri="http://catalog:8181/catalog", warehouse='
                + json.dumps(warehouse)
                + "}]"
            ),
            "SAIL_CATALOG__DEFAULT_CATALOG": "lakehouse",
            "AWS_ACCESS_KEY_ID": values["S3_ACCESS_KEY"],
            "AWS_SECRET_ACCESS_KEY": values["S3_SECRET_KEY"],
            "AWS_ENDPOINT": "http://storage:8333",
            "AWS_REGION": "us-east-1",
            "AWS_VIRTUAL_HOSTED_STYLE_REQUEST": "false",
            "RUST_LOG": "info",
        }
    )
    return result


def main(argv=None):
    """Probe local TCP liveness or replace this process with the server."""
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["health"]:
        try:
            with socket.create_connection(("127.0.0.1", 50051), timeout=2):
                return 0
        except OSError:
            return 1
    if argv:
        print("SAIL_COMMAND_REJECTED", file=sys.stderr)
        return 2
    try:
        values = environment(os.environ)
        os.execvpe(
            "/opt/sail-venv/bin/sail",
            ["sail", "spark", "server", "--ip", "0.0.0.0", "--port", "50051"],
            values,
        )
    except (ValueError, OSError):
        print("SAIL_START_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
