"""Send a fixed file ID to the internal worker, without SQL credentials."""

import re
from hashlib import sha256

import httpx
from job_manager import OperationFailure

CONTRACT_IDS = {
    "erp_products",
    "erp_work_orders",
    "erp_sales_orders",
    "erp_shipments",
    "mes_events",
    "mes_downtime",
}


def public_result(value):
    """Filter completion metadata without private helper fields."""
    if not isinstance(value, dict):
        raise ValueError("Invalid SQL batch result.")
    batch_id, observed_at = value.get("batch_id"), value.get("observed_at")
    if (
        not isinstance(batch_id, str)
        or not isinstance(observed_at, str)
        or not re.fullmatch(r"[0-9a-f]{64}", batch_id)
        or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z", observed_at
        )
    ):
        raise ValueError("Invalid SQL batch identity.")
    apply_id = sha256((batch_id + "\n" + observed_at).encode()).hexdigest()
    counts = value.get("counts")
    if (
        value.get("apply_id") != apply_id
        or value.get("completed") is not True
        or type(value.get("skipped")) is not bool
        or not isinstance(counts, dict)
        or set(counts) != CONTRACT_IDS
        or any(type(n) is not int or n < 0 for n in counts.values())
        or type(value.get("sql_statements")) is not int
        or not 0 <= value["sql_statements"] <= 1000
    ):
        raise ValueError("Invalid SQL batch completion.")
    result = {
        key: value[key]
        for key in (
            "batch_id",
            "apply_id",
            "observed_at",
            "completed",
            "skipped",
            "counts",
            "sql_statements",
        )
    }
    # Only the source-side adapter can confirm its separate SQL transaction.
    # Worker acknowledgements naturally omit this field and default to false.
    checkpointed = value.get("checkpointed", False)
    if type(checkpointed) is not bool:
        raise ValueError("Invalid checkpoint status.")
    result["checkpointed"] = checkpointed
    for key in ("changed_counts", "current_counts"):
        counts = value.get(key)
        if counts is not None:
            if (
                not isinstance(counts, dict)
                or set(counts) != CONTRACT_IDS
                or any(type(n) is not int or n < 0 for n in counts.values())
            ):
                raise ValueError("Invalid optional row counts.")
            result[key] = counts
    return result


def send_batch(token, batch_id, observed_at):
    """Validate completion before allowing a source-side checkpoint.

    A timeout is an unknown outcome: the worker may still be running. The
    caller leaves checkpoints unchanged and must check/replay explicitly.
    Arbitrary URLs, redirects, file paths and DB settings are not accepted.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", batch_id) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z", observed_at
    ):
        raise OperationFailure(
            "SQL_BATCH_INVALID", "batchの形式を確認します。"
        )
    try:
        with httpx.Client(
            timeout=240, trust_env=False, follow_redirects=False
        ) as client:
            response = client.post(
                "http://worker:8000/run",
                headers={
                    "Authorization": "Bearer " + token,
                },
                json={
                    "action": "sql-load",
                    "batch_id": batch_id,
                    "observed_at": observed_at,
                },
            )
        if response.status_code != 200:
            raise ValueError("Worker rejected batch.")
        value = response.json()
        if (
            not isinstance(value, dict)
            or value.get("batch_id") != batch_id
            or value.get("completed") is not True
            or value.get("observed_at") != observed_at
            or type(value.get("skipped")) is not bool
        ):
            raise ValueError("Worker completion mismatch.")
        return public_result(value)
    except httpx.TimeoutException:
        raise OperationFailure(
            "SQL_WORKER_RESULT_UNKNOWN",
            "workerが継続中の可能性があります。状態と結果を確認します。",
        ) from None
    except (httpx.HTTPError, ValueError, TypeError):
        raise OperationFailure(
            "SQL_WORKER_FAILED",
            "workerの状態とbatch結果を確認します。",
        ) from None
