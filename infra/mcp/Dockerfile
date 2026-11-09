FROM docker.io/library/python:3.14-slim@sha256:ac25e5afbfdb4a94ff99cc24b0345719210133144481480bcba6085efa36a341
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_PYTHON_DOWNLOADS=never \
    UV_SYSTEM_CERTS=true \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /opt/lakehouse
COPY infra/proxy/install_ca.py ./install_ca.py
RUN --mount=type=secret,id=corporate_ca \
    python install_ca.py --ca-file /run/secrets/corporate_ca
COPY infra/mcp/requirements.lock ./requirements.lock
RUN uv --no-cache venv --python /usr/local/bin/python /opt/venv \
    && uv --no-cache pip sync --python /opt/venv/bin/python \
       --require-hashes requirements.lock
ENV UV_OFFLINE=1
COPY infra/mcp/server.py ./server.py
USER 10001:10001
CMD ["python", "server.py"]
