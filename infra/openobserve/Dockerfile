# Vendor binaries only: no Rust build, package install or runtime download.
FROM docker.io/library/busybox:1.37.0-uclibc@sha256:7aa3d3c53cc1ed823c6a5967ebaac87700a5b0766af45ff599dbca3506c8b072 AS probe
FROM public.ecr.aws/zinclabs/openobserve:v1.1.2@sha256:4a154849432f75c4fb024de32fe34163c02406c905dab22bf8d213d2e3386392
ARG WORKER_UID=10001
ARG WORKER_GID=10001
# The copied /bin/busybox is a static uClibc ELF on amd64 and arm64.
# /openobserve and all its vendor shared libraries stay in their original base.
COPY --from=probe /bin/busybox /bin/busybox
# Metadata applies only to new volume copy-up; never repair mounted live data.
RUN ["/bin/busybox", "sh", "-ec", "test \"$WORKER_UID\" -gt 0; /bin/busybox mkdir -p /data; /bin/busybox chown \"$WORKER_UID:$WORKER_GID\" /data; /bin/busybox chmod 0700 /data"]
COPY infra/openobserve/entrypoint.sh /etc/lakehouse-openobserve/entrypoint.sh
COPY infra/openobserve/health.sh /etc/lakehouse-openobserve/health.sh
USER ${WORKER_UID}:${WORKER_GID}
ENTRYPOINT ["/bin/busybox", "sh", "/etc/lakehouse-openobserve/entrypoint.sh"]
