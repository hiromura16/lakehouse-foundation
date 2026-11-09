"""Give notebooks explicit Trino, Spark cluster and Iceberg connections."""

import os
from urllib.parse import quote, urlsplit

GOLD_TABLES = {"factory_daily_production", "sales_fulfillment"}
EVENT_LOG_DIR = "file:///opt/spark/events"
DRIVER_PYTHON = "/opt/notebook-venv/bin/python"
EXECUTOR_PYTHON = "/opt/spark-venv/bin/python"


def read_gold(table, limit=100):
    """Read one declared Gold table with bounded Trino result size."""
    from trino.dbapi import connect

    if table not in GOLD_TABLES or not isinstance(limit, int):
        raise ValueError("Choose a declared Gold table and integer limit.")
    if not 1 <= limit <= 10000:
        raise ValueError("Use a row limit from 1 to 10000.")
    with connect(
        host="trino",
        port=8080,
        user="notebook",
        catalog="iceberg",
        schema="gold",
        request_timeout=60,
    ) as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM " + table + " LIMIT " + str(limit))
        return [column[0] for column in cursor.description], cursor.fetchall()


def ui_url(values, key, default):
    """Build a loopback GUI link without accepting an arbitrary URL."""
    port = values.get(key, default)
    if not port.isdigit() or not 1024 <= int(port) <= 65535:
        raise ValueError("Invalid Spark UI port setting.")
    return "http://localhost:" + port


def execution_settings(values=None):
    """Select cluster only with compute enabled; never fail over silently."""
    values = os.environ if values is None else values
    profiles = set(filter(None, values.get("COMPOSE_PROFILES", "").split(",")))
    remote = "compute" in profiles
    master = "local[2]"
    if remote:
        master = values.get("SPARK_MASTER_URL", "spark://spark-master:7077")
        target = urlsplit(master)
        try:
            valid = (
                target.scheme == "spark"
                and target.hostname
                and target.port is not None
                and 1 <= target.port <= 65535
                and target.username is None
                and target.password is None
                and target.path in {"", "/"}
                and not target.query
                and not target.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Use an explicit spark://host:port master URL.")
    return {
        "mode": "standalone" if remote else "local",
        "master": master,
        "executor_python": EXECUTOR_PYTHON if remote else DRIVER_PYTHON,
        "master_ui": (
            ui_url(values, "SPARK_MASTER_UI_PORT", "8082") if remote else None
        ),
        "history_ui": (
            ui_url(values, "SPARK_HISTORY_UI_PORT", "18080")
            if remote
            else None
        ),
        "worker_ui": (
            ui_url(values, "SPARK_WORKER_UI_PORT", "8085") if remote else None
        ),
    }


def session_info(spark):
    """Return the actual master, application and user-facing history links."""
    selected = execution_settings()
    context = spark.sparkContext
    identifier = context.applicationId
    remote = selected["mode"] == "standalone"
    application_ui = None
    if remote:
        application_ui = (
            selected["master_ui"]
            + "/proxy/"
            + quote(identifier, safe="")
            + "/"
        )
    return {
        "mode": selected["mode"],
        "master": context.master,
        "application_id": identifier,
        "master_ui": selected["master_ui"],
        "worker_ui": selected["worker_ui"],
        "application_ui": application_ui,
        "history_ui": selected["history_ui"],
        "event_log_dir": EVENT_LOG_DIR,
    }


def show_session(spark):
    """Display cluster identity and links directly in notebook output."""
    info = session_info(spark)
    if info["mode"] == "local":
        print("compute profile is disabled: Spark uses local[2] in Jupyter.")
        print("Event logs remain available when History is enabled later.")
    print("Spark master:", info["master"])
    print("Application:", info["application_id"])
    links = [
        (name, info[name])
        for name in ["master_ui", "worker_ui", "application_ui", "history_ui"]
        if info[name]
    ]
    try:
        from IPython.display import Markdown, display
    except ImportError:
        for name, address in links:
            print(name + ":", address)
    else:
        if links:
            display(
                Markdown(
                    " · ".join(
                        "[" + name + "](" + address + ")"
                        for name, address in links
                    )
                )
            )
    return info


def spark_session():
    """Start the selected mode and reject an existing session mismatch."""
    from pyspark.sql import SparkSession

    selected = execution_settings()
    # PySpark serializes SparkContext.pythonExec from this environment value.
    # Setting only spark.pyspark.python does not update Python RDD workers.
    os.environ["PYSPARK_PYTHON"] = selected["executor_python"]
    os.environ["PYSPARK_DRIVER_PYTHON"] = DRIVER_PYTHON
    prefix = "spark.sql.catalog.lakehouse"
    options = {
        "spark.sql.extensions": (
            "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions"
        ),
        "spark.sql.shuffle.partitions": "2",
        "spark.ui.enabled": "true",
        "spark.ui.port": "4040",
        "spark.eventLog.enabled": "true",
        "spark.eventLog.dir": EVENT_LOG_DIR,
        "spark.pyspark.driver.python": DRIVER_PYTHON,
        "spark.pyspark.python": selected["executor_python"],
        "spark.redaction.regex": (
            "(?i)secret|password|token|access.key|access-key"
        ),
        prefix: "org.apache.iceberg.spark.SparkCatalog",
        prefix + ".catalog-impl": "org.apache.iceberg.rest.RESTCatalog",
        prefix + ".uri": "http://catalog:8181/catalog",
        prefix + ".warehouse": os.environ["WAREHOUSE_NAME"],
        prefix + ".token": "dummy",
        prefix + ".io-impl": "org.apache.iceberg.aws.s3.S3FileIO",
        prefix + ".s3.endpoint": "http://storage:8333",
        prefix + ".s3.path-style-access": "true",
        prefix + ".s3.access-key-id": os.environ["S3_ACCESS_KEY"],
        prefix + ".s3.secret-access-key": os.environ["S3_SECRET_KEY"],
        prefix + ".client.region": "us-east-1",
        prefix + ".rest-metrics-reporting-enabled": "false",
    }
    if selected["mode"] == "standalone":
        options.update(
            {
                "spark.driver.host": "jupyter",
                "spark.driver.bindAddress": "0.0.0.0",
                "spark.driver.port": "39000",
                "spark.blockManager.port": "39001",
                "spark.ui.reverseProxy": "true",
                "spark.ui.reverseProxyUrl": selected["master_ui"],
                "spark.cores.max": "2",
                "spark.executor.cores": "2",
                "spark.executor.memory": "1g",
            }
        )
    builder = SparkSession.builder.master(selected["master"]).appName(
        "lakehouse-notebook"
    )
    for key, value in options.items():
        builder = builder.config(key, value)
    try:
        spark = builder.getOrCreate()
    except Exception:
        raise RuntimeError(
            "Spark did not start. Check the selected Master/Worker and kernel "
            "logs. No automatic local fallback was attempted."
        ) from None
    if spark.sparkContext.master != selected["master"]:
        raise RuntimeError(
            "An existing Spark session uses another master. Stop that session "
            "or restart its kernel before changing execution mode."
        )
    spark.sparkContext.setLogLevel("WARN")
    show_session(spark)
    return spark
