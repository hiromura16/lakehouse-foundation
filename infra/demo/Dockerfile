# Python 3.14 matches the worker's tested base and Streamlit's supported range.
FROM docker.io/library/python:3.14-slim@sha256:ac25e5afbfdb4a94ff99cc24b0345719210133144481480bcba6085efa36a341
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /opt/lakehouse
COPY infra/demo/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY scripts ./scripts
COPY medallion ./medallion
COPY infra/demo ./infra/demo
COPY infra/cdc/config.py ./infra/cdc/config.py
# Serve Swagger from this image; runtime browsers need no public CDN.
RUN python infra/demo/fetch_swagger.py
USER 10001:10001
CMD ["streamlit", "run", "infra/demo/app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false"]
