FROM docker.io/library/python:3.14-slim@sha256:ac25e5afbfdb4a94ff99cc24b0345719210133144481480bcba6085efa36a341
WORKDIR /opt/lakehouse
COPY infra/lakehouse/requirements.txt ./requirements.txt
COPY infra/lakehouse/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY scripts/admin.py ./admin.py
COPY scripts/generate-demo.py ./scripts/generate-demo.py
COPY medallion ./medallion
COPY samples ./samples
USER 10001:10001
CMD ["python", "-u", "/opt/lakehouse/admin.py", "serve"]
