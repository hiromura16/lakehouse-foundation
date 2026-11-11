#!/bin/sh
# Refuse stale volume permissions rather than repairing operator-owned data.
set -eu
expected="$(/bin/busybox id -u):$(/bin/busybox id -g):700"
actual="$(/bin/busybox stat -c '%u:%g:%a' /data 2>/dev/null || true)"
if [ "$(/bin/busybox id -u)" = 0 ] || [ -L /data ] || \
   [ ! -d /data ] || [ ! -w /data ] || [ "$actual" != "$expected" ]; then
  printf '%s\n' 'OpenObserve lab state is not prepared for the runtime user.' >&2
  exit 78
fi
if [ -z "${ZO_ROOT_USER_EMAIL:-}" ] || [ -z "${ZO_ROOT_USER_PASSWORD:-}" ]; then
  printf '%s\n' 'OpenObserve lab bootstrap credentials are not configured.' >&2
  exit 78
fi
# Bootstrap credentials must never double as the Vector ingestion credential.
# The token itself is not passed to this service or printed by this wrapper.
exec /openobserve
