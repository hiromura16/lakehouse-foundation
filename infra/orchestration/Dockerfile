# Keep the Airflow package and Python ABI consistent with its constraints.
FROM apache/airflow:3.4.0-python3.12@sha256:818f94affc5bd7ce0be0cc04f2ace09aa8382e65190a73a08881229ff32700cb
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
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
ENV UV_CACHE_DIR=/tmp/airflow-uv-cache UV_PYTHON_DOWNLOADS=never

USER airflow
# Verify the release constraints before installing the two explicit providers.
COPY --chown=airflow:root infra/orchestration/fetch_constraints.py /tmp/
RUN python /tmp/fetch_constraints.py
# Airflow 3.4 already owns this venv; keep its Python, PATH and entrypoint.
# Use the fixed installer explicitly, ahead of any vendor uv on PATH.
RUN /usr/local/bin/uv --no-cache pip install \
    --python /home/airflow/.local/bin/python \
    'apache-airflow==3.4.0' \
    'apache-airflow-providers-standard==1.21.0' \
    'apache-airflow-providers-http==6.3.0' \
    --constraints /tmp/airflow-constraints.txt \
    && /usr/local/bin/uv --no-cache pip check \
       --python /home/airflow/.local/bin/python
ENV UV_OFFLINE=1
COPY --chown=airflow:root infra/orchestration/dags/ /opt/lakehouse/dags/
