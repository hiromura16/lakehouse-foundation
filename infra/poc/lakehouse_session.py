"""Give notebooks explicit, inspectable Trino and Spark catalog connections."""

import os

GOLD_TABLES = {"factory_daily_production", "sales_fulfillment"}


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


def spark_session():
    """Create Spark local[2] with the same Iceberg runtime as compute."""
    from pyspark.sql import SparkSession

    prefix = "spark.sql.catalog.lakehouse"
    options = {
        "spark.sql.extensions": (
            "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions"
        ),
        "spark.sql.shuffle.partitions": "2",
        "spark.ui.enabled": "false",
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
    builder = SparkSession.builder.master("local[2]").appName(
        "lakehouse-notebook"
    )
    for key, value in options.items():
        builder = builder.config(key, value)
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    return spark
