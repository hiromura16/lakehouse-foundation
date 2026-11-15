# The manager is a separate, unprivileged management plane. It shares only
# reviewed public code and frontend build tools with the PoCs it controls.
FROM docker.io/library/python:3.14-slim@sha256:ac25e5afbfdb4a94ff99cc24b0345719210133144481480bcba6085efa36a341 AS python-base
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    UV_SYSTEM_CERTS=true
COPY infra/proxy/install_ca.py /usr/local/share/lakehouse/install_ca.py
RUN --mount=type=secret,id=corporate_ca,required=false \
    python3 /usr/local/share/lakehouse/install_ca.py

FROM docker.io/library/node:24.22.3-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54 AS web-build
COPY --from=python-base /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt \
    NPM_CONFIG_UPDATE_NOTIFIER=false
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python-base AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH" \
    UV_CACHE_DIR=/tmp/uv-cache UV_PYTHON_DOWNLOADS=never
WORKDIR /opt/lakehouse
# Reuse the hash-locked Python toolchain; runtime performs no installation.
COPY infra/demo/requirements.lock ./requirements.lock
RUN uv --no-cache venv --python /usr/local/bin/python /opt/venv \
    && uv --no-cache pip sync --python /opt/venv/bin/python \
       --require-hashes requirements.lock
COPY infra/management ./infra/management
COPY infra/demo/portal_auth.py ./infra/demo/portal_auth.py
COPY infra/demo/fetch_swagger.py ./infra/demo/fetch_swagger.py
COPY --from=web-build /build/dist ./frontend/dist
RUN /opt/venv/bin/python infra/demo/fetch_swagger.py \
    --output /opt/lakehouse/infra/management/static
ENV UV_OFFLINE=1
USER 10001:10001
CMD ["python", "-m", "uvicorn", "infra.management.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
