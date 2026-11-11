# Official PySail binary-wheel PoC deployment, without a Rust toolchain.
FROM docker.io/library/python:3.14-slim@sha256:ac25e5afbfdb4a94ff99cc24b0345719210133144481480bcba6085efa36a341
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
ARG WORKER_UID=10001
ARG WORKER_GID=10001
ENV UV_SYSTEM_CERTS=true UV_PYTHON_DOWNLOADS=never \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/lakehouse
COPY infra/proxy/install_ca.py /opt/lakehouse/install_ca.py
RUN --mount=type=secret,id=corporate_ca,required=false \
    python3 /opt/lakehouse/install_ca.py
COPY infra/sail/requirements-server.lock /opt/lakehouse/requirements.lock
RUN uv --no-cache venv --python /usr/local/bin/python /opt/sail-venv \
    && uv --system-certs --no-cache pip sync --python /opt/sail-venv/bin/python \
      --require-hashes --only-binary :all: /opt/lakehouse/requirements.lock \
    && uv pip check --python /opt/sail-venv/bin/python \
    && groupadd --gid "$WORKER_GID" sail-runtime \
    && useradd --uid "$WORKER_UID" --gid "$WORKER_GID" \
      --no-create-home --home-dir /tmp sail-runtime
COPY infra/sail/server.py /opt/lakehouse/infra/sail/server.py
ENV UV_OFFLINE=1 HOME=/tmp
USER ${WORKER_UID}:${WORKER_GID}
WORKDIR /tmp
CMD ["/opt/sail-venv/bin/python", "/opt/lakehouse/infra/sail/server.py"]
