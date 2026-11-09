# Spark 4.1 is the newest line with a published Iceberg 1.12 runtime.
FROM apache/spark:4.1.4-scala2.13-java21-python3-ubuntu@sha256:847f91f8bb2f28d632064dfe632d423020f8fd60b27c7704c0e5bf5b0c6432c7

COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
ARG SPARK_UID=10001
ARG SPARK_GID=10001
USER root
# Optional public corporate CA; no credential or private key enters the image.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    UV_SYSTEM_CERTS=true
COPY infra/proxy/install_ca.py /usr/local/share/lakehouse/install_ca.py
RUN --mount=type=secret,id=corporate_ca,required=false \
    python3 /usr/local/share/lakehouse/install_ca.py
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Both artifacts are locked to the same Iceberg release. BuildKit's reserved
# proxy arguments apply to these downloads without entering image ENV layers.
RUN set -eu; \
    central=https://repo.maven.apache.org/maven2/org/apache/iceberg; \
    runtime=iceberg-spark-runtime-4.1_2.13; \
    bundle=iceberg-aws-bundle; \
    curl --fail --show-error --location --retry 3 \
      "$central/$runtime/1.12.0/$runtime-1.12.0.jar" \
      --output "/opt/spark/jars/$runtime-1.12.0.jar"; \
    printf '%s  %s\n' \
      ac1e2cb00970309f0c6bf241fabf334aef373c1f3e3b443181d429d5f2c0903ace47df9cfc1d55c95807a096cf405ae0a8868831b9e9eb5696dfdb6380587f38 \
      "/opt/spark/jars/$runtime-1.12.0.jar" | sha512sum --check -; \
    curl --fail --show-error --location --retry 3 \
      "$central/$bundle/1.12.0/$bundle-1.12.0.jar" \
      --output "/opt/spark/jars/$bundle-1.12.0.jar"; \
    printf '%s  %s\n' \
      b01456f80142c8837004e31329a0f40db8a9d44917a4a694cb7d8d6d3a68cb20c61e6f7788fa1bdcb95c0edf724322b49667d6f9ef60fc9cc46da5c24ce5c7de \
      "/opt/spark/jars/$bundle-1.12.0.jar" | sha512sum --check -

# Match the notebook's Python minor and Arrow dependencies for remote UDFs.
# A passwd entry is required by Hadoop's user identity resolution.
RUN if ! getent group "$SPARK_GID" >/dev/null; then \
      groupadd --gid "$SPARK_GID" spark-runtime; \
    fi \
    && usermod --uid "$SPARK_UID" --gid "$SPARK_GID" spark \
    && mkdir -p /opt/spark/events /opt/spark/work \
    && chown "$SPARK_UID:$SPARK_GID" /opt/spark/events /opt/spark/work
COPY infra/compute/requirements.lock /opt/lakehouse/requirements-spark.lock
RUN uv --system-certs --no-cache venv --python /usr/bin/python3 /opt/spark-venv \
    && uv --system-certs --no-cache pip sync --python /opt/spark-venv/bin/python \
      --require-hashes /opt/lakehouse/requirements-spark.lock \
    && uv pip check --python /opt/spark-venv/bin/python
ENV UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never \
    PYSPARK_PYTHON=/opt/spark-venv/bin/python \
    PYTHONPATH=/opt/spark/python \
    HOME=/opt/spark/work
COPY --chown=spark:root infra/compute/spark_cluster.py /opt/lakehouse/
COPY --chown=spark:root infra/compute/spark_smoke.py /opt/lakehouse/
COPY --chown=spark:root infra/compute/smoke.sql /opt/lakehouse/
WORKDIR /opt/spark/work
USER spark
