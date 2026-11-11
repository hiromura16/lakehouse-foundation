# Disposable source test only. No Rust build, package install or host mounts.
# Cross-image binary/library compatibility is a runtime gate, not assumed.
FROM docker.io/timberio/vector:0.61.0-debian@sha256:f6c23b5a7e16fb2838d33619ef7acf4bad2289a7cf22bc90efc2371f520c4ea2 AS vector
FROM docker.io/grafana/loki:3.7.9@sha256:0db216b630ae1f348a1584c67f3cead8b4c1fc839169a39501db664744158e28 AS loki
FROM docker.io/library/python:3.14-slim@sha256:ac25e5afbfdb4a94ff99cc24b0345719210133144481480bcba6085efa36a341
COPY --from=vector /usr/bin/vector /usr/bin/vector
COPY --from=loki /usr/bin/loki /usr/bin/loki
COPY infra/monitoring/vector /opt/vector
COPY infra/monitoring/loki/loki.yaml /opt/loki.yaml
USER 10001:10001
WORKDIR /opt/vector
ENTRYPOINT ["python3", "/opt/vector/tests/integration/probe.py"]
