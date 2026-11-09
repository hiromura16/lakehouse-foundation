"""Manually trigger the core worker without storing a bearer token in Git."""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from airflow.exceptions import AirflowException
from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG, BaseHook


def run_medallion():
    """Resolve the private HTTP connection only when this task executes."""
    connection = BaseHook.get_connection("lakehouse_worker")
    host = connection.host or "worker"
    if "://" not in host:
        host = (connection.schema or "http") + "://" + host
    address = urllib.parse.urlsplit(host)
    if (
        address.scheme not in {"http", "https"}
        or not address.hostname
        or address.username
        or address.password
        or address.path not in {"", "/"}
        or address.query
        or address.fragment
    ):
        raise AirflowException("Use an HTTP host without credentials or path.")
    port = connection.port or address.port or 8000
    authority = address.hostname
    if ":" in authority:
        authority = "[" + authority + "]"
    endpoint = address.scheme + "://" + authority + ":" + str(port) + "/run"
    token = connection.password
    if not token:
        raise AirflowException("Set the worker token in Connection Password.")
    request = urllib.request.Request(
        endpoint,
        data=json.dumps({"action": "medallion"}).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise AirflowException("Worker response exceeded size limit.")
            payload = json.loads(body)
    except urllib.error.HTTPError as error:
        raise AirflowException(
            "Worker rejected the request: HTTP " + str(error.code)
        ) from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise AirflowException(
            "Worker transport or response failed."
        ) from None
    if not isinstance(payload, dict) or "error" in payload:
        raise AirflowException(
            "Worker did not return a successful job result."
        )
    # Keep source paths and response details out of task logs and XCom.
    return {"status": "succeeded"}


with DAG(
    dag_id="lakehouse_medallion",
    description="Manually run the core ingestion and medallion SQL job.",
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    tags=["lakehouse", "manual"],
) as dag:
    PythonOperator(
        task_id="run_medallion",
        python_callable=run_medallion,
        execution_timeout=timedelta(minutes=10),
        retries=0,
        do_xcom_push=False,
        show_return_value_in_logs=False,
    )
