"""Check only the authenticated consumer control listener."""

import os
from urllib.request import ProxyHandler, Request, build_opener


def main():
    """Exit silently; HTTP liveness does not prove ingestion passed."""
    request = Request(
        "http://127.0.0.1:8001/status",
        headers={
            "Authorization": "Bearer " + os.environ.get("WORKER_TOKEN", ""),
        },
    )
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=3) as reply:
            return 0 if reply.status == 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
