FROM docker.io/library/python:3.14-slim@sha256:ac25e5afbfdb4a94ff99cc24b0345719210133144481480bcba6085efa36a341
ARG CDC_UID=10001
ARG CDC_GID=10001
WORKDIR /opt/lakehouse
COPY infra/lakehouse/requirements.lock ./requirements.lock
COPY infra/cdc/requirements.lock ./cdc-requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && pip install --no-cache-dir --require-hashes -r cdc-requirements.lock
COPY scripts/admin.py ./admin.py
COPY medallion ./medallion
COPY infra/cdc/*.py ./cdc/
ENV PYTHONPATH=/opt/lakehouse
# State is a dedicated durable volume, never the snapshot landing directory.
RUN mkdir /state && chown "${CDC_UID}:${CDC_GID}" /state
USER ${CDC_UID}:${CDC_GID}
CMD ["python", "-u", "/opt/lakehouse/cdc/consumer.py"]
