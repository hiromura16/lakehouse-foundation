# Use the completed official Vector binary. No compiler or extra downloads.
FROM docker.io/timberio/vector:0.61.0-debian@sha256:f6c23b5a7e16fb2838d33619ef7acf4bad2289a7cf22bc90efc2371f520c4ea2
USER root
ARG WORKER_UID=10001
ARG WORKER_GID=10001
# Initialize NEW named-volume copy-up metadata at build time only.
# Existing host paths/volume contents are never repaired by this image.
RUN mkdir -p /var/lib/vector \
    && chown "${WORKER_UID}:${WORKER_GID}" /var/lib/vector \
    && chmod 0700 /var/lib/vector
COPY infra/monitoring/vector /etc/vector
USER ${WORKER_UID}:${WORKER_GID}
WORKDIR /etc/vector
ENTRYPOINT ["/bin/sh", "/etc/vector/entrypoint.sh"]
