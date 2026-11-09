# Python 3.14 matches the worker's tested base and Streamlit's supported range.
FROM docker.io/library/python:3.14-slim@sha256:ac25e5afbfdb4a94ff99cc24b0345719210133144481480bcba6085efa36a341
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
# Optional public corporate CA; no credential or private key enters the image.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    UV_SYSTEM_CERTS=true
COPY infra/proxy/install_ca.py /usr/local/share/lakehouse/install_ca.py
RUN --mount=type=secret,id=corporate_ca,required=false \
    python3 /usr/local/share/lakehouse/install_ca.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /opt/lakehouse
COPY infra/demo/requirements.lock ./requirements.lock
RUN uv --no-cache venv --python /usr/local/bin/python /opt/venv \
    && uv --no-cache pip sync --python /opt/venv/bin/python \
       --require-hashes requirements.lock
COPY scripts ./scripts
COPY medallion ./medallion
COPY docs ./docs
COPY infra/demo ./infra/demo
COPY infra/cdc/config.py ./infra/cdc/config.py
# Serve Swagger from this image; runtime browsers need no public CDN.
RUN /opt/venv/bin/python infra/demo/fetch_swagger.py
# No package or interpreter resolution occurs when the services start.
ENV UV_OFFLINE=1
USER 10001:10001
CMD ["streamlit", "run", "infra/demo/app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false"]
