"""Create explicit synthetic vector or manual lineage examples through APIs.

vector uses the private Qdrant write key to create a four-dimensional demo
collection with three points, leaving any existing collection untouched.
lineage submits a stable demo run ID to Marquez with the current event time.
Neither action creates embeddings, runs medallion SQL, or proves that a real
pipeline executed. Normal service startup does not invoke this script.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import NAMESPACE_URL, uuid5

COLLECTION = "lh_demo_numeric_vectors"
OPENER = build_opener(ProxyHandler({}))


def request(method, url, body=None, headers=None, accepted=(200,)):
    """Call fixed internal endpoints without logging secret headers."""
    if not url.startswith(("http://qdrant:6333/", "http://marquez:5000/")):
        raise ValueError("Use an internal PoC tool endpoint.")
    value = json.dumps(body).encode() if body is not None else None
    message = Request(
        url,
        data=value,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with OPENER.open(message, timeout=30) as response:
            if response.status not in accepted:
                raise RuntimeError("The PoC API rejected the request.")
            payload = response.read()
            return json.loads(payload) if payload else None
    except HTTPError as error:
        if error.code == 404 and 404 in accepted:
            return None
        raise RuntimeError("The PoC API request failed.") from None


def seed_vector(values):
    """Create three numeric vectors only when the demo collection is absent.

    An existing collection is returned as-is, without checking or repairing
    its points. Operators should inspect its contents after a partial prior
    run instead of assuming this seed restored a complete demonstration.
    """
    key = values.get("QDRANT_API_KEY", "")
    if len(key) < 16:
        raise ValueError("Configure a private Qdrant write key.")
    endpoint = "http://qdrant:6333/collections/" + COLLECTION
    headers = {"api-key": key}
    existing = request("GET", endpoint, headers=headers, accepted=(200, 404))
    if existing is not None:
        return {"tool": "qdrant", "created": False, "existing": True}
    request(
        "PUT",
        endpoint,
        {"vectors": {"size": 4, "distance": "Cosine"}},
        headers,
    )
    request(
        "PUT",
        endpoint + "/points?wait=true",
        {
            "points": [
                {
                    "id": 1,
                    "vector": [1.0, 0.0, 0.0, 0.0],
                    "payload": {
                        "label": "synthetic production A",
                        "source": "demo",
                    },
                },
                {
                    "id": 2,
                    "vector": [0.9, 0.1, 0.0, 0.0],
                    "payload": {
                        "label": "synthetic production B",
                        "source": "demo",
                    },
                },
                {
                    "id": 3,
                    "vector": [0.0, 0.0, 1.0, 0.0],
                    "payload": {
                        "label": "synthetic maintenance",
                        "source": "demo",
                    },
                },
            ]
        },
        headers,
    )
    return {"tool": "qdrant", "created": True, "points": 3, "dimensions": 4}


def lineage_event():
    """Describe a manual demo graph without claiming a real pipeline ran."""
    producer = "urn:lakehouse-foundation:poc-demo"
    return {
        "eventType": "COMPLETE",
        "eventTime": datetime.now(timezone.utc).isoformat(),
        "producer": producer,
        "schemaURL": (
            "https://openlineage.io/spec/2-0-2/OpenLineage.json"
            "#/$defs/RunEvent"
        ),
        "run": {"runId": str(uuid5(NAMESPACE_URL, producer))},
        "job": {"namespace": "lakehouse-poc", "name": "manual-demo-lineage"},
        "inputs": [
            {"namespace": "iceberg", "name": "bronze.mes_events"},
            {"namespace": "iceberg", "name": "bronze.erp_work_orders"},
        ],
        "outputs": [
            {"namespace": "iceberg", "name": "gold.factory_daily_production"}
        ],
    }


def seed_lineage():
    """Send one stable demo run ID to the native OpenLineage endpoint."""
    request(
        "POST",
        "http://marquez:5000/api/v1/lineage",
        lineage_event(),
        accepted=(200, 201, 202),
    )
    return {"tool": "marquez", "demo_job": True, "inputs": 2, "outputs": 1}


def main():
    """Require an explicit action; never seed tools from ordinary startup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["vector", "lineage"],
        help=(
            "vectorは合成3点のcollection作成、lineageは手動デモ系統の登録。"
            "どちらも内部APIへの明示的な書込みです"
        ),
    )
    args = parser.parse_args()
    result = (
        seed_vector(os.environ) if args.action == "vector" else seed_lineage()
    )
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"failed": True, "error_type": type(error).__name__}))
        sys.exit(1)
